"""app.py HTTP 端点测试（NL2SQL 版）"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from matcher.entity_matcher import MatchResult
from matcher.matcher_service import MatcherService
from service.llm_extractions import Extraction, SQLIntentJson
from service.session_models import QueryState


# ==================== Fake 底层 matcher ====================

def _mr(matched, score, candidates=None):
    """构造 MatchResult，可选 top5 候选"""
    cands = [{"name": v, "score": s} for v, s in (candidates or [])]
    return MatchResult(matched=matched, score=score, candidates=cands)


class FakeMatcher:
    """text → MatchResult 的假匹配器"""

    def __init__(self, results=None, fallback=None):
        self.results = results or {}
        self.fallback = fallback

    def match(self, text):
        if text in self.results:
            return self.results[text]
        if self.fallback is not None:
            return self.fallback
        return MatchResult(matched=None, score=0.0, explain={})


COLUMN_MAP = {
    "地区": ("users.region", 100.0),
    "区域": ("users.region", 100.0),
    "品类": ("products.category", 100.0),
    "渠道": ("orders.channel", 100.0),
    "金额": ("orders.amount", 100.0),
    "会员等级": ("users.vip_level", 100.0),
    "状态": ("orders.status", 100.0),
    # English mentions (schema is English-only now)
    "region": ("users.region", 100.0),
    "area": ("users.region", 100.0),
    "category": ("products.category", 100.0),
    "categories": ("products.category", 100.0),
    "product category": ("products.category", 100.0),
    "channel": ("orders.channel", 100.0),
    "vip level": ("users.vip_level", 100.0),
    "payment type": ("payments.payment_type", 100.0),
}


def _fake_column_matcher(overrides=None):
    results = {}
    for text, (value, score) in COLUMN_MAP.items():
        results[text] = _mr(value, score)
    if overrides:
        results.update(overrides)
    return FakeMatcher(results)


def _make_service(table_matcher=None, metric_matcher=None, column_matcher=None) -> MatcherService:
    """真实 MatcherService（阈值/推断/join 跑真代码）+ 假文本匹配层"""
    svc = MatcherService(catalog_path="catalog")
    if table_matcher is not None:
        svc.table_matcher = table_matcher
    if metric_matcher is not None:
        svc.metric_matcher = metric_matcher
    if column_matcher is not None:
        svc.column_matcher = column_matcher
    return svc


def _high_confidence_service() -> MatcherService:
    return _make_service(
        table_matcher=FakeMatcher({"订单表": _mr("orders", 100.0), "订单": _mr("orders", 100.0)}),
        metric_matcher=FakeMatcher({
            "销售额": _mr("revenue", 100.0),
            "订单量": _mr("order_count", 100.0),
            "客单价": _mr("avg_order_value", 100.0),
            "revenue": _mr("revenue", 100.0),
            "sales": _mr("revenue", 100.0),
            "order count": _mr("order_count", 100.0),
            "average order value": _mr("avg_order_value", 100.0),
        }),
        column_matcher=_fake_column_matcher(),
    )


def _low_confidence_table_service() -> MatcherService:
    return _make_service(
        table_matcher=FakeMatcher({
            "商品表": _mr(None, 55.0, candidates=[("products", 55.0), ("orders", 48.0)]),
        }),
        metric_matcher=FakeMatcher({"销售额": _mr("revenue", 100.0)}),
        column_matcher=_fake_column_matcher(),
    )


# ==================== Fixtures ====================

@pytest.fixture
def client(tmp_path):
    """创建隔离的 TestClient，使用 tmp_path 避免 state 泄漏"""
    import app as app_module
    from memory.storage.memory_file import TaskStorage
    from memory.user_preference_store import UserPreferenceStore
    from service.session_manager import SessionManager
    from service.task_manager import TaskManager

    app_module.session_manager = SessionManager(data_path=str(tmp_path / "data"))
    app_module.task_manager = TaskManager(
        storage=TaskStorage(data_path=str(tmp_path / "data" / "tasks"))
    )
    app_module.user_preference_store = UserPreferenceStore(
        data_path=str(tmp_path / "data" / "user_preferences")
    )
    app_module.orchestrator.session = app_module.session_manager
    app_module.orchestrator.task = app_module.task_manager
    app_module.orchestrator.preferences = app_module.user_preference_store

    app_module._matcher_service = _high_confidence_service()

    with TestClient(app_module.app) as c:
        yield c

    app_module._matcher_service = None


def _mock_generate_sql(sql="SELECT users.region, sum(orders.amount) AS revenue FROM orders"):
    return mock.patch(
        "service.query_orchestrator.generate_sql",
        new_callable=mock.AsyncMock,
        return_value=(sql, {"rounds": [{"round": 1, "ok": True, "errors": []}]}),
    )


# ==================== Tests: Happy Path ====================

class TestNL2SQLHappyPath:
    @staticmethod
    def _seed_previous_state():
        import app as app_module

        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)
        prev_qs = QueryState(
            project_id=55,
            tables=["orders"],
            metrics=["revenue"],
            time_range={"type": "last_n_days", "n": 7},
            group_by=["users.region"],
            filters=[],
            turn_type="new_query",
        )
        app_module.session_manager.update_query_state(ctx.session_id, prev_qs)
        return ctx, prev_qs

    def test_nl2sql_success_with_join_inference(self, client):
        """表+指标+分组（地区列在 users 表）→ 自动 join 推断"""
        intent = SQLIntentJson(
            table_extractions=[Extraction(text="订单表")],
            metric_extractions=[Extraction(text="销售额")],
            group_by_extractions=[Extraction(text="地区")],
            time_extractions=[Extraction(text="近7天")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen_mock:
                resp = client.post("/nl2sql", json={
                    "text": "近7天各地区订单表的销售额",
                    "project_id": 55,
                })

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["sql"].startswith("SELECT")
        assert body["session_id"]

        # resolved_intent：主表/指标/分组/时间
        intent_out = body["resolved_intent"]
        assert intent_out["tables"] == ["orders"]
        assert intent_out["metrics"] == ["revenue"]
        assert intent_out["group_by"] == ["users.region"]
        assert intent_out["time_range"] == {"type": "last_n_days", "n": 7}

        # join 推断进 explain
        resolver_explain = body["explain"]["resolver_explain"]
        assert resolver_explain["sql_generation"]["join_explain"]["steps"], "region 在 users 表上，应推断出 join"
        assert resolver_explain["table"]["method"] == "exact"

        # SQL 生成收到的 intent 包含 join 步骤
        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["base_table"] == "orders"
        assert any(j["right"] == "users" for j in gen_intent["joins"])
        assert gen_intent["time_expr"].startswith("orders.created_at")

    def test_nl2sql_metric_only_infers_table(self, client):
        """不提表名 → 从指标表达式推断主表"""
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="客单价")],
            time_extractions=[Extraction(text="近7天")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql():
                resp = client.post("/nl2sql", json={"text": "近7天客单价是多少", "project_id": 55})

        body = resp.json()
        assert body["status"] == "success"
        assert body["resolved_intent"]["tables"] == ["orders"]
        assert body["explain"]["resolver_explain"]["table_inference"]["method"] == "inferred_from_metric"

    def test_response_carries_resolution_trace(self, client):
        """explain["trace"]：每个字段来自哪一步（主表由指标推断）"""
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="客单价")],
            time_extractions=[Extraction(text="近7天")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql():
                resp = client.post("/nl2sql", json={"text": "近7天客单价是多少", "project_id": 55})

        trace = resp.json()["explain"]["trace"]
        assert trace[0].startswith("extract")
        assert any(l.startswith("table") and "inferred_from_metric" in l and "→ orders" in l for l in trace)
        assert trace[-1].startswith("result") and "success" in trace[-1]

    def test_early_exit_trace_explains_missing_previous_state(self, client):
        """无上一轮状态的 follow-up 早退：trace 说明 no_previous_state"""
        intent = SQLIntentJson(time_extractions=[Extraction(text="yesterday")])

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            resp = client.post("/nl2sql", json={"text": "What about yesterday?", "project_id": 55})

        body = resp.json()
        assert body["status"] == "early_exit"
        assert any("no_previous_state" in l for l in body["explain"]["trace"])

    def test_nl2sql_returns_session_id(self, client):
        intent = SQLIntentJson(metric_extractions=[Extraction(text="销售额")])

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql():
                resp = client.post("/nl2sql", json={"text": "销售额", "project_id": 55})

        assert resp.json()["session_id"] is not None

    def test_nl2sql_uses_existing_session(self, client):
        import app as app_module
        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)
        sid = ctx.session_id

        intent = SQLIntentJson(metric_extractions=[Extraction(text="销售额")])

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql():
                resp = client.post("/nl2sql", json={
                    "text": "销售额",
                    "project_id": 55,
                    "session_id": sid,
                })

        assert resp.json()["session_id"] == sid

    def test_nl2sql_followup_inherits_previous_state(self, client):
        ctx, _ = self._seed_previous_state()

        intent = SQLIntentJson(
            time_extractions=[Extraction(text="昨天")],
        )
        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql():
                resp = client.post("/nl2sql", json={
                    "text": "昨天",
                    "project_id": 55,
                    "session_id": ctx.session_id,
                })

        body = resp.json()
        assert body["status"] == "success"
        intent_out = body["resolved_intent"]
        assert intent_out["tables"] == ["orders"]
        assert intent_out["metrics"] == ["revenue"]
        assert intent_out["time_range"]["n"] == 1
        assert body["explain"]["turn_explain"]["mode"] == "followup_patch"
        assert body["explain"]["turn_explain"]["decision"]["reason"] == "time_only_term"
        assert body["explain"]["turn_explain"]["applied_patch_fields"] == ["time_range"]

    def test_nl2sql_followup_changes_metric_only(self, client):
        ctx, _ = self._seed_previous_state()

        intent = SQLIntentJson(metric_extractions=[Extraction(text="订单量")])

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql():
                resp = client.post("/nl2sql", json={
                    "text": "改成订单量",
                    "project_id": 55,
                    "session_id": ctx.session_id,
                })

        body = resp.json()
        assert body["status"] == "success"
        intent_out = body["resolved_intent"]
        assert intent_out["metrics"] == ["order_count"]
        assert intent_out["tables"] == ["orders"]
        assert body["explain"]["turn_explain"]["explicit_fields"] == ["metrics"]
        assert body["explain"]["turn_explain"]["state_snapshot"]["metrics"] == ["order_count"]

    def test_nl2sql_followup_adds_window_rank(self, client):
        """「每个地区前3」→ window patch（分组排名）"""
        ctx, _ = self._seed_previous_state()

        intent = SQLIntentJson(window_extractions=[Extraction(text="每个地区前3")])

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen_mock:
                resp = client.post("/nl2sql", json={
                    "text": "每个地区前3",
                    "project_id": 55,
                    "session_id": ctx.session_id,
                })

        body = resp.json()
        assert body["status"] == "success"
        intent_out = body["resolved_intent"]
        assert intent_out["window"] == {"group_by": "users.region", "limit": 3}
        assert body["explain"]["turn_explain"]["mode"] == "followup_patch"

        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["window"] == {"group_by": "users.region", "limit": 3}

    def test_nl2sql_window_rank_new_query(self, client):
        """新查询直接带分组排名意图"""
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="销售额")],
            window_extractions=[Extraction(text="每个地区前3")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen_mock:
                resp = client.post("/nl2sql", json={"text": "每个地区销售额前3", "project_id": 55})

        body = resp.json()
        assert body["status"] == "success"
        assert body["resolved_intent"]["window"] == {"group_by": "users.region", "limit": 3}

        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["window"]["limit"] == 3

    def test_nl2sql_global_topn(self, client):
        """「销售额最高的前5」→ order_by TopN"""
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="销售额")],
            order_extractions=[Extraction(text="销售额最高的前5")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen_mock:
                resp = client.post("/nl2sql", json={"text": "销售额最高的前5", "project_id": 55})

        body = resp.json()
        assert body["status"] == "success"
        intent_out = body["resolved_intent"]
        assert intent_out["order_by"]["metric"] == "revenue"
        assert intent_out["order_by"]["direction"] == "DESC"
        assert intent_out["order_by"]["limit"] == 5

        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["order_by"]["metric_expr"] == "sum(orders.amount)"

    def test_nl2sql_applies_user_preference_rerank_after_recall(self, client):
        import app as app_module

        for _ in range(6):
            app_module.user_preference_store.record_selection(
                55, "user_pref", metric="order_count"
            )

        intent = SQLIntentJson(metric_extractions=[Extraction(text="销售额")])
        # 低置信指标：revenue 55 分，order_count 54 分
        svc = _make_service(
            metric_matcher=FakeMatcher({
                "销售额": _mr(None, 55.0, candidates=[("revenue", 55.0), ("order_count", 54.0)]),
            }),
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp = client.post("/nl2sql", json={
                    "text": "销售额情况",
                    "project_id": 55,
                    "user_id": "user_pref",
                })

        body = resp.json()
        assert body["status"] == "needs_confirmation"
        # 偏好 rerank：order_count（6 次使用）加权后排到第一
        assert body["candidates"]["metrics"][0]["value"] == "order_count"
        assert body["explain"]["resolver_explain"]["metric"]["user_preference_bias"]["applied"] is True


# ==================== Tests: Early Exit ====================

class TestNL2SQLEarlyExit:

    def test_nl2sql_early_exit_no_signal(self, client):
        intent = SQLIntentJson()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            resp = client.post("/nl2sql", json={
                "text": "你好",
                "project_id": 55,
            })

        body = resp.json()
        assert body["status"] == "early_exit"
        assert body["message"]
        assert "table" in body["message"].lower() or "metric" in body["message"].lower()

    def test_nl2sql_early_exit_records_message(self, client):
        import app as app_module

        intent = SQLIntentJson()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            resp = client.post("/nl2sql", json={
                "text": "some query",
                "project_id": 55,
            })

        sid = resp.json()["session_id"]
        ctx = app_module.session_manager.get_session(sid)
        assert ctx is not None
        assert any(m.content == "some query" for m in ctx.messages)


# ==================== Tests: Confirmation Flow ====================

class TestNL2SQLConfirmation:
    @staticmethod
    def _seed_previous_state():
        import app as app_module

        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)
        # group_by 用 orders.channel：确认切换主表后（如 products）仍可 join，避免无关的 join 确认
        prev_qs = QueryState(
            project_id=55,
            tables=["orders"],
            metrics=["revenue"],
            time_range={"type": "last_n_days", "n": 7},
            group_by=["orders.channel"],
            filters=[],
            turn_type="new_query",
        )
        app_module.session_manager.update_query_state(ctx.session_id, prev_qs)
        return ctx, prev_qs

    def test_nl2sql_needs_confirmation(self, client):
        import app as app_module

        intent = SQLIntentJson(table_extractions=[Extraction(text="商品表")], metric_extractions=[Extraction(text="销售额")])
        svc = _low_confidence_table_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp = client.post("/nl2sql", json={
                    "text": "商品表的销售额",
                    "project_id": 55,
                })

        body = resp.json()
        assert body["status"] == "needs_confirmation"
        assert body["task_id"] is not None
        assert body["candidates"] is not None
        assert "tables" in body["candidates"]

        # pending task 落在 session 上
        sid = body["session_id"]
        ctx = app_module.session_manager.get_session(sid)
        assert ctx.pending_task_id == body["task_id"]

    def test_nl2sql_confirmation_then_reply(self, client):
        intent = SQLIntentJson(table_extractions=[Extraction(text="商品表")], metric_extractions=[Extraction(text="销售额")])
        svc = _low_confidence_table_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp1 = client.post("/nl2sql", json={
                    "text": "商品表的销售额",
                    "project_id": 55,
                })

        body1 = resp1.json()
        sid = body1["session_id"]
        assert body1["status"] == "needs_confirmation"

        # 用户回复 "1" → 确认 products → 生成 SQL
        with _mock_generate_sql():
            resp2 = client.post("/nl2sql", json={
                "text": "1",
                "project_id": 55,
                "session_id": sid,
            })

        body2 = resp2.json()
        assert body2["status"] == "success"
        assert body2["explain"]["turn_explain"]["mode"] == "confirmation"
        assert body2["explain"]["turn_explain"]["field_sources"]["tables"] == "confirmed"
        assert body2["explain"]["turn_explain"]["confirmed_fields"]["tables"] == "products"
        assert body2["resolved_intent"]["tables"] == ["products"]

    def test_nl2sql_followup_confirmation_then_reply(self, client):
        ctx, _ = self._seed_previous_state()

        intent = SQLIntentJson(table_extractions=[Extraction(text="商品表")])
        svc = _low_confidence_table_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp1 = client.post("/nl2sql", json={
                    "text": "对比商品表",
                    "project_id": 55,
                    "session_id": ctx.session_id,
                })

        body1 = resp1.json()
        assert body1["status"] == "needs_confirmation"
        assert body1["explain"]["turn_explain"]["mode"] == "followup_patch"

        with _mock_generate_sql():
            resp2 = client.post("/nl2sql", json={
                "text": "1",
                "project_id": 55,
                "session_id": ctx.session_id,
            })

        body2 = resp2.json()
        assert body2["status"] == "success"
        assert body2["resolved_intent"]["tables"] == ["products"]
        assert body2["resolved_intent"]["metrics"] == ["revenue"]
        assert body2["explain"]["turn_explain"]["mode"] == "confirmation"
        assert body2["explain"]["turn_explain"]["field_sources"]["tables"] == "confirmed"
        assert "metrics" in body2["explain"]["turn_explain"]["inherited_fields"]
        assert body2["explain"]["turn_explain"]["confirmed_fields"]["tables"] == "products"

    def test_nl2sql_confirmation_after_session_restore(self, client):
        import app as app_module
        from service.session_manager import SessionManager

        intent = SQLIntentJson(table_extractions=[Extraction(text="商品表")], metric_extractions=[Extraction(text="销售额")])
        svc = _low_confidence_table_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp1 = client.post("/nl2sql", json={
                    "text": "商品表的销售额",
                    "project_id": 55,
                })

        body1 = resp1.json()
        sid = body1["session_id"]
        assert body1["status"] == "needs_confirmation"

        # 模拟服务重启：清空内存 session/task，再从同一路径恢复
        original_session_manager = app_module.session_manager
        original_task_manager = app_module.task_manager
        app_module.session_manager = SessionManager(
            data_path=str(original_session_manager.storage.data_path.parent)
        )
        app_module.task_manager = type(original_task_manager)(
            ttl_minutes=30,
            storage=original_task_manager.storage,
        )

        try:
            with _mock_generate_sql():
                resp2 = client.post("/nl2sql", json={
                    "text": "1",
                    "project_id": 55,
                    "session_id": sid,
                })
        finally:
            app_module.session_manager = original_session_manager
            app_module.task_manager = original_task_manager

        body2 = resp2.json()
        assert body2["status"] == "success"


# ==================== Tests: Session Endpoints ====================

class TestSessionEndpoints:

    def test_list_sessions_empty(self, client):
        resp = client.get("/sessions")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_sessions_with_data(self, client):
        import app as app_module
        app_module.session_manager.create_or_get(None, "user_1", 55)

        resp = client.get("/sessions")
        assert resp.json()["count"] == 1

    def test_get_session_found(self, client):
        import app as app_module
        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)

        resp = client.get(f"/sessions/{ctx.session_id}")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == ctx.session_id

    def test_get_session_not_found(self, client):
        resp = client.get("/sessions/nonexistent")
        assert resp.status_code == 404

    def test_delete_session_found(self, client):
        import app as app_module
        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)

        resp = client.delete(f"/sessions/{ctx.session_id}")
        assert resp.status_code == 200

        resp2 = client.get(f"/sessions/{ctx.session_id}")
        assert resp2.status_code == 404

    def test_delete_session_not_found(self, client):
        resp = client.delete("/sessions/nonexistent")
        assert resp.status_code == 404


# ==================== Tests: Cross-Encoder Rerank ====================

class TestCrossEncoderRerank:
    """RERANKER_ENABLED=true 时低置信表名被 cross-encoder 静默终选"""

    def test_low_confidence_auto_accepted_by_reranker(self, client, monkeypatch):
        monkeypatch.setenv("RERANKER_ENABLED", "true")

        from service import reranker as reranker_module

        intent = SQLIntentJson(
            table_extractions=[Extraction(text="商品表")],
            metric_extractions=[Extraction(text="销售额")],
        )
        svc = _low_confidence_table_service()

        llm_rerank = {"scores": [
            {"value": "products", "relevance": 92, "reason": "商品表即产品表"},
            {"value": "orders", "relevance": 35, "reason": "订单表与商品表不同"},
        ]}

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                with mock.patch.object(reranker_module, "_rerank_via_llm", return_value=llm_rerank):
                    with _mock_generate_sql() as gen_mock:
                        resp = client.post("/nl2sql", json={
                            "text": "商品表的销售额",
                            "project_id": 55,
                        })

        body = resp.json()
        assert body["status"] == "success"  # 未触发确认流，被终选采纳
        assert body["resolved_intent"]["tables"] == ["products"]
        assert "cross_encoder" in body["explain"]["resolver_explain"]["table"]["method"]
        rexplain = body["explain"]["resolver_explain"]["table"]["cross_encoder_rerank"]
        assert rexplain["applied"] is True
        assert rexplain["auto_accept"] is True
        assert rexplain["best"] == "products"
        # 生成 SQL 收到的是终选后的主表
        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["base_table"] == "products"

    def test_reranker_disabled_by_default_keeps_confirmation(self, client, monkeypatch):
        monkeypatch.delenv("RERANKER_ENABLED", raising=False)

        from service import reranker as reranker_module

        intent = SQLIntentJson(
            table_extractions=[Extraction(text="商品表")],
            metric_extractions=[Extraction(text="销售额")],
        )
        svc = _low_confidence_table_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                with mock.patch.object(reranker_module, "_rerank_via_llm") as rerank_mock:
                    resp = client.post("/nl2sql", json={
                        "text": "商品表的销售额",
                        "project_id": 55,
                    })

        assert resp.json()["status"] == "needs_confirmation"
        rerank_mock.assert_not_called()  # 默认关闭，LLM 不被调用

    def test_reranker_skipped_outside_confirmation_band(self, client, monkeypatch):
        """已被确定性采纳的结果不再交给 LLM 终选（只在确认带触发）"""
        monkeypatch.setenv("RERANKER_ENABLED", "true")

        from service import reranker as reranker_module

        intent = SQLIntentJson(
            table_extractions=[Extraction(text="商品表")],
            metric_extractions=[Extraction(text="销售额")],
        )
        svc = _make_service(
            table_matcher=FakeMatcher({
                "商品表": _mr("products", 95.0, candidates=[("products", 95.0), ("orders", 60.0)]),
            }),
            metric_matcher=FakeMatcher({"销售额": _mr("revenue", 100.0)}),
            column_matcher=_fake_column_matcher(),
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                with mock.patch.object(reranker_module, "_rerank_via_llm") as rerank_mock:
                    with _mock_generate_sql():
                        resp = client.post("/nl2sql", json={"text": "商品表的销售额", "project_id": 55})

        assert resp.json()["status"] == "success"
        rerank_mock.assert_not_called()


class TestWindowDemoteToOrder:
    """window 文本含方向词（最高/最低）→ 降级为全局 TopN"""

    def test_window_with_direction_word_demotes(self, client):
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="销售额")],
            group_by_extractions=[Extraction(text="品类")],
            window_extractions=[Extraction(text="各品类销售额最高的前5")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen_mock:
                resp = client.post("/nl2sql", json={"text": "各品类销售额最高的前5", "project_id": 55})

        body = resp.json()
        assert body["status"] == "success"
        intent_out = body["resolved_intent"]
        # window 未建立，order_by 建立（指标回退到主指标 revenue）
        assert intent_out["window"] is None
        assert intent_out["order_by"]["metric"] == "revenue"
        assert intent_out["order_by"]["direction"] == "DESC"
        assert intent_out["order_by"]["limit"] == 5

        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["order_by"]["limit"] == 5
        assert gen_intent["window"] is None


# ==================== Tests: graceful failures ====================

class TestGracefulFailures:

    def test_sql_generation_failure_is_early_exit_not_500(self, client):
        """校验/修复循环耗尽时返回可读的 early_exit，而不是抛 500"""
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="销售额")],
            time_extractions=[Extraction(text="近7天")],
        )
        boom = mock.patch(
            "service.query_orchestrator.generate_sql",
            new_callable=mock.AsyncMock,
            side_effect=ValueError("SQL validation failed after 3 rounds: missing_filter: x"),
        )
        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with boom:
                resp = client.post("/nl2sql", json={"text": "近7天销售额", "project_id": 55})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "early_exit"
        assert body["sql"] == ""
        assert "missing_filter" in body["message"]
        assert body["explain"]["sql_generation"]["generation_failed"]

    def test_unresolved_filter_column_asks_instead_of_guessing(self, client):
        """捏造的过滤列（user_type）不得被模糊匹配成某个真实列"""
        from service.llm_extractions import FilterExtraction

        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="销售额")],
            filter_extractions=[FilterExtraction(text="新用户", column="user_type", op="=", value="new")],
        )
        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen:
                resp = client.post("/nl2sql", json={"text": "新用户的销售额", "project_id": 55})

        body = resp.json()
        assert body["status"] == "early_exit"
        assert "新用户" in body["message"]
        gen.assert_not_called()


    def test_extraction_parse_failure_is_early_exit_not_500(self, client):
        """LLM 对寒暄回了非 JSON 文本 → extract 抛 ValueError，不应 500"""
        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock,
                        side_effect=ValueError("LLM SQLIntentJson 输出不合法: Expecting value")):
            resp = client.post("/nl2sql", json={"text": "hello there", "project_id": 55})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "early_exit"
        assert body["sql"] == ""


class TestWindowFullTextRecovery:
    """L1 把窗口短语截断（limit 留在原句）时，用完整查询文本兜底解析"""

    def test_truncated_window_fragment_recovered_from_full_text(self, client):
        # L1 只抽到 "in each region"（没有 top 3），完整查询里有
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="revenue")],
            group_by_extractions=[Extraction(text="categories")],
            window_extractions=[Extraction(text="in each region")],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with _mock_generate_sql() as gen_mock:
                resp = client.post("/nl2sql", json={
                    "text": "Top 3 categories by revenue in each region",
                    "project_id": 55,
                })

        body = resp.json()
        assert body["status"] == "success"
        assert body["resolved_intent"]["window"] == {"group_by": "users.region", "limit": 3}
        # explain 记录了兜底来源
        assert body["explain"]["resolver_explain"]["columns_parse"]["window"]["recovered_from_full_text"] is True

        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["window"] == {"group_by": "users.region", "limit": 3}


class TestColumnCollisionConfirmation:
    """同名列 exact 冲突 → 确认流 → 回复编号后生成 SQL（a01/a02 端到端）"""

    @staticmethod
    def _real_column_service():
        """真实列 EntityMatcher（冲突逻辑在 entity_matcher.py）+ fake 指标层"""
        return _make_service(metric_matcher=FakeMatcher({
            "revenue": _mr("revenue", 100.0),
            "order count": _mr("order_count", 100.0),
        }))

    def test_detail_column_collision_confirms_then_reply(self, client):
        # 无表名无指标：bare "amount" 无法消歧 → 确认
        intent = SQLIntentJson(column_extractions=[Extraction(text="amount")])
        svc = self._real_column_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp1 = client.post("/nl2sql", json={"text": "Show the amount of recent records", "project_id": 55})

        body1 = resp1.json()
        assert body1["status"] == "needs_confirmation"
        assert "detail_column" in body1["candidates"]
        values = [c["value"] for c in body1["candidates"]["detail_column"]]
        assert set(values) == {"orders.amount", "payments.amount"}

        # 回复 1 → 确认所选列，推断基表，生成 SQL
        with _mock_generate_sql() as gen_mock:
            resp2 = client.post("/nl2sql", json={
                "text": "1", "project_id": 55, "session_id": body1["session_id"],
            })
        body2 = resp2.json()
        assert body2["status"] == "success"
        chosen = values[0]
        assert chosen in body2["resolved_intent"]["detail_columns"]
        assert body2["explain"]["turn_explain"]["confirmed_fields"]["detail_column"] == chosen
        gen_intent = gen_mock.call_args.args[1]
        assert gen_intent["detail_columns"] == [chosen]

    def test_group_by_time_collision_confirms(self, client):
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="order count")],
            group_by_extractions=[Extraction(text="time")],
        )
        svc = self._real_column_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                resp = client.post("/nl2sql", json={"text": "Number of orders by time", "project_id": 55})

        body = resp.json()
        assert body["status"] == "needs_confirmation"
        assert "group_by_column" in body["candidates"]
        assert len(body["candidates"]["group_by_column"]) == 3

    def test_region_with_metric_context_auto_resolves(self, client):
        """revenue 语境下 region 由距离消歧，不打断（j01 场景）"""
        intent = SQLIntentJson(
            metric_extractions=[Extraction(text="revenue")],
            group_by_extractions=[Extraction(text="region")],
        )
        svc = self._real_column_service()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=intent):
            with mock.patch("app.get_matcher_service", return_value=svc):
                with _mock_generate_sql():
                    resp = client.post("/nl2sql", json={"text": "Revenue by region", "project_id": 55})

        body = resp.json()
        assert body["status"] == "success"
        assert body["resolved_intent"]["group_by"] == ["users.region"]
        assert body["explain"]["resolver_explain"]["columns_parse"]["columns"][0]["method"] == \
            "exact_collision_distance_resolved"
