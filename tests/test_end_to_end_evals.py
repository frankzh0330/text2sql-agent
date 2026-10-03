"""Data-driven end-to-end evals for the NL2SQL pipeline."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
import yaml
from fastapi.testclient import TestClient

from matcher.entity_matcher import MatchResult
from matcher.matcher_service import MatcherService
from service.llm_extractions import Extraction, FilterExtraction, SQLIntentJson
from service.session_models import QueryState


def _load_cases() -> list:
    """加载 golden cases；带 xfail 字段的 case 以 strict xfail 标记（断言的仍是"正确行为"）

    EVAL_IGNORE_XFAIL=1 时忽略标记，用于查看这些 case 的真实结果。
    """
    path = Path(__file__).parent / "evals" / "nl2sql_cases.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ignore_xfail = os.getenv("EVAL_IGNORE_XFAIL") == "1"
    params = []
    for case in data.get("cases", []):
        marks = []
        if case.get("xfail") and not ignore_xfail:
            marks.append(pytest.mark.xfail(reason=case["xfail"], strict=True))
        params.append(pytest.param(case, id=case["name"], marks=marks))
    return params


# ==================== LLM 抽取 mock ====================

def _make_intent(payload: dict | None) -> SQLIntentJson:
    payload = payload or {}

    def _items(key: str) -> list[Extraction]:
        return [Extraction(text=text) for text in payload.get(key, [])]

    return SQLIntentJson(
        table_extractions=_items("table_extractions"),
        metric_extractions=_items("metric_extractions"),
        column_extractions=_items("column_extractions"),
        filter_extractions=[
            FilterExtraction(
                text=f.get("text", ""),
                column=f.get("column"),
                op=f.get("op", "="),
                value=f.get("value"),
            )
            for f in payload.get("filter_extractions", [])
        ],
        group_by_extractions=_items("group_by_extractions"),
        time_extractions=_items("time_extractions"),
        order_extractions=_items("order_extractions"),
        window_extractions=_items("window_extractions"),
    )


# ==================== matcher mock（真实服务 + 假匹配层） ====================

def _mr(matched, score, candidates=None) -> MatchResult:
    cands = [{"name": v, "score": s} for v, s in (candidates or [])]
    return MatchResult(matched=matched, score=score, candidates=cands)


class FakeMatcher:
    def __init__(self, results=None):
        self.results = results or {}

    def match(self, text):
        if text in self.results:
            return self.results[text]
        return MatchResult(matched=None, score=0.0, explain={})


def _build_service(name: str) -> MatcherService:
    """matcher 场景

    real                 — 真实 MatcherService（召回 + 别名打分），评测 schema 别名 + 匹配逻辑
    low_confidence_table — 真实 service + 假 table matcher，稳定复现确认流（55 分，介于确认带内）
    """
    svc = MatcherService(catalog_path="catalog")

    if name == "real":
        return svc

    if name == "low_confidence_table":
        svc.table_matcher = FakeMatcher({
            "products table": _mr(None, 55.0, candidates=[("products", 55.0), ("orders", 48.0)]),
        })
        return svc

    raise ValueError(f"unknown resolver scenario: {name}")


# ==================== fixtures / runtime ====================

def _rebind_runtime(tmp_data_root: str) -> None:
    import app as app_module
    from memory.storage.memory_file import TaskStorage
    from memory.user_preference_store import UserPreferenceStore
    from service.session_manager import SessionManager
    from service.task_manager import TaskManager

    app_module.session_manager = SessionManager(data_path=tmp_data_root)
    app_module.task_manager = TaskManager(
        storage=TaskStorage(data_path=str(Path(tmp_data_root) / "tasks"))
    )
    app_module.user_preference_store = UserPreferenceStore(
        data_path=str(Path(tmp_data_root) / "user_preferences")
    )
    app_module.orchestrator.session = app_module.session_manager
    app_module.orchestrator.task = app_module.task_manager
    app_module.orchestrator.preferences = app_module.user_preference_store


@pytest.fixture
def eval_client(tmp_path):
    import app as app_module

    _rebind_runtime(str(tmp_path / "data"))
    app_module._matcher_service = _build_service("real")

    with TestClient(app_module.app) as c:
        yield c

    app_module._matcher_service = None


def _write_project_memory(case: dict, data_root: Path) -> None:
    setup = case.get("setup") or {}
    project_memory = setup.get("project_memory")
    if not project_memory:
        return

    project_id = project_memory["project_id"]
    project_dir = data_root / "memory" / f"project_{project_id}"
    project_dir.mkdir(parents=True, exist_ok=True)

    links = []
    for entry in project_memory.get("entries", []):
        filename = entry["filename"]
        (project_dir / filename).write_text(entry["content"], encoding="utf-8")
        links.append(f"- [{filename}]({filename})")

    (project_dir / "MEMORY.md").write_text("\n".join(links) + "\n", encoding="utf-8")


def _seed_setup(case: dict) -> str | None:
    import app as app_module

    setup = case.get("setup") or {}
    data_root = Path(app_module.session_manager.storage.data_path).parent
    _write_project_memory(case, data_root)

    last_query_state = setup.get("last_query_state")
    if not last_query_state:
        return None

    ctx = app_module.session_manager.create_or_get(None, "eval_user", last_query_state["project_id"])
    app_module.session_manager.update_query_state(ctx.session_id, QueryState(**last_query_state))
    return ctx.session_id


def _assert_expectations(body: dict, expect: dict, gen_mock: mock.AsyncMock | None) -> None:
    if "status" in expect:
        assert body["status"] == expect["status"]
    if "status_not" in expect:
        assert body["status"] != expect["status_not"]

    if "turn_mode" in expect:
        assert body["explain"]["turn_explain"]["mode"] == expect["turn_mode"]

    if "message_contains" in expect:
        assert expect["message_contains"] in (body.get("message") or "")

    if "candidates_contains" in expect:
        assert expect["candidates_contains"] in (body.get("candidates") or {})

    intent_expect = expect.get("resolved_intent")
    if not intent_expect:
        return

    resolved = body["resolved_intent"]
    if "tables" in intent_expect:
        assert resolved["tables"] == intent_expect["tables"]
    if "metrics" in intent_expect:
        assert resolved["metrics"] == intent_expect["metrics"]
    if "group_by" in intent_expect:
        assert resolved["group_by"] == intent_expect["group_by"]
    if "group_by_contains" in intent_expect:
        assert intent_expect["group_by_contains"] in resolved["group_by"]
    if "time_n" in intent_expect:
        assert resolved["time_range"]["n"] == intent_expect["time_n"]
    if "time_type" in intent_expect:
        assert resolved["time_range"]["type"] == intent_expect["time_type"]
    if "order_direction" in intent_expect:
        assert resolved["order_by"]["direction"] == intent_expect["order_direction"]
    if "order_limit" in intent_expect:
        assert resolved["order_by"]["limit"] == intent_expect["order_limit"]
    if "window_group" in intent_expect:
        assert resolved.get("window") is not None, (
            f"window intent missing (window_group={intent_expect['window_group']})"
        )
        assert resolved["window"]["group_by"] == intent_expect["window_group"]
    if "window_limit" in intent_expect:
        assert resolved.get("window") is not None, "window intent missing"
        assert resolved["window"]["limit"] == intent_expect["window_limit"]
    if "filter_column" in intent_expect:
        assert resolved["filters"][0]["column"] == intent_expect["filter_column"]
    if "filter_value" in intent_expect:
        assert resolved["filters"][0]["value"] == intent_expect["filter_value"]

    if "time_expr_contains" in expect:
        assert expect["time_expr_contains"] in gen_mock.call_args.args[1]["time_expr"]

    if "sql_intent_contains_join" in expect:
        # SQL 生成入参应包含推断出的 join
        gen_intent = gen_mock.call_args.args[1]
        assert any(
            expect["sql_intent_contains_join"] in (j["right"], j["left"])
            for j in gen_intent["joins"]
        )


@pytest.mark.parametrize("case", _load_cases())
def test_nl2sql_end_to_end_eval_cases(eval_client, case):
    import app as app_module

    data_root = Path(app_module.session_manager.storage.data_path).parent
    session_id = _seed_setup(case)

    for step in case["steps"]:
        if step.get("restart_before"):
            _rebind_runtime(str(data_root))
            app_module._matcher_service = _build_service("real")

        request_body = {
            "text": step["text"],
            "project_id": step.get("project_id", 55),
        }
        if session_id:
            request_body["session_id"] = session_id

        extraction_payload = step.get("extraction")
        expected_memory = step.get("assert_memory_contains")

        async def _mock_extract(query, session_context=None,
                                *, _payload=extraction_payload, _expected_memory=expected_memory):
            if _expected_memory:
                memory_context = (session_context or {}).get("memory_corrections", "")
                assert _expected_memory in memory_context
            return _make_intent(_payload)

        resolver_name = step.get("resolver")

        with mock.patch(
            "service.query_orchestrator.extract_llm_async",
            new_callable=mock.AsyncMock,
            side_effect=_mock_extract,
        ):
            with mock.patch(
                "service.query_orchestrator.generate_sql",
                new_callable=mock.AsyncMock,
                return_value=("SELECT 1", {"rounds": [{"round": 1, "ok": True, "errors": []}]}),
            ) as gen_mock:
                if resolver_name:
                    with mock.patch("app.get_matcher_service", return_value=_build_service(resolver_name)):
                        resp = eval_client.post("/nl2sql", json=request_body)
                else:
                    resp = eval_client.post("/nl2sql", json=request_body)

        body = resp.json()
        _assert_expectations(body, step["expect"], gen_mock if body["status"] == "success" else None)
        session_id = body.get("session_id", session_id)

        # eval 期间确保 session 确实持续复用，避免 accidentally stateless
        if session_id:
            assert app_module.session_manager.get_session(session_id) is not None
