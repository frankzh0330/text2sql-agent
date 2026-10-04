"""resolution trace：构建规则 + Telegram 附加开关"""
from __future__ import annotations

from gateway.telegram_gateway import TelegramGateway
from service.resolution_trace import build_trace

SUCCESS = {
    "status": "success",
    "sql": "SELECT 1",
    "extraction_json": {
        "metric_extractions": [{"text": "revenue"}],
        "group_by_extractions": [{"text": "region"}],
        "time_extractions": [{"text": "last 7 days"}],
    },
    "resolved_intent": {"tables": ["orders"], "time_range": {"type": "last_n_days", "n": 7}},
    "explain": {
        "turn_explain": {"mode": "new_query", "decision": {"mode": "new_query", "reason": "no_previous_state"}},
        "resolver_explain": {
            "table": {"input": "", "value": "", "method": "no_extractions", "score": 0.0},
            "metric": {"input": "revenue", "value": "revenue", "method": "exact", "score": 100.0, "alias": "revenue"},
            "table_inference": {"method": "inferred_from_metric", "metric": "revenue",
                                "source": "declared_table", "table": "orders"},
            "columns_parse": {"columns": [{"role": "group_by", "text": "region", "column": "users.region",
                                           "method": "exact_collision_distance_resolved", "score": 100.0}]},
            "time": {"method": "regex_match", "input": "last 7 days", "pattern": "last_n_days", "days": 7},
            "sql_generation": {"join_explain": {"base_table": "orders", "missing_join": [],
                                                "steps": [{"left": "orders", "right": "users"}]}},
        },
    },
}


def test_one_line_per_field_with_provenance():
    trace = build_trace(SUCCESS)
    assert trace[0] == "extract   metric=['revenue'] group_by=['region'] time=['last 7 days']"
    assert "turn      new_query (no_previous_state)" in trace
    assert "metric    'revenue' → revenue · exact · alias 'revenue' · 100" in trace
    assert "table     (not given) → orders · inferred_from_metric revenue (declared view)" in trace
    assert "group_by  'region' → users.region · exact_collision_distance_resolved · 100" in trace
    assert "time      'last 7 days' → last_n_days n=7 · regex_match" in trace
    assert "joins     orders→users ✓" in trace
    assert trace[-1] == "result    success"


def test_confirmation_and_missing_join_are_explained():
    result = {
        "status": "needs_confirmation",
        "candidates": {"join": [{"value": "orders", "score": 100.0}]},
        "explain": {"resolver_explain": {"sql_generation": {"join_explain": {
            "base_table": "payments", "steps": [], "missing_join": ["users"]}}}},
    }
    trace = build_trace(result)
    assert "joins     payments→users ✗ no direct join" in trace
    assert trace[-1] == "result    needs_confirmation · ask join: orders"


def test_missing_fields_never_raise():
    assert build_trace({}) == ["extract   (nothing)", "result    success"]


def test_telegram_reply_omits_trace_by_default(monkeypatch):
    monkeypatch.delenv("TRACE_IN_REPLY", raising=False)
    result = {**SUCCESS, "explain": {**SUCCESS["explain"], "trace": build_trace(SUCCESS)}}
    assert "Trace" not in TelegramGateway(bot_token="x").format_response(result)


def test_telegram_reply_appends_trace_when_enabled(monkeypatch):
    monkeypatch.setenv("TRACE_IN_REPLY", "true")
    trace = build_trace(SUCCESS)
    result = {**SUCCESS, "explain": {**SUCCESS["explain"], "trace": trace}}
    text = TelegramGateway(bot_token="x").format_response(result)
    assert "🔎 Trace" in text and trace[3] in text


def test_telegram_reply_stays_within_message_limit(monkeypatch):
    monkeypatch.setenv("TRACE_IN_REPLY", "true")
    result = {"status": "early_exit", "message": "no", "explain": {"trace": ["x" * 200] * 50}}
    assert len(TelegramGateway(bot_token="x").format_response(result)) <= 4096
