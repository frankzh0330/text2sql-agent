"""Resolution trace：每个结果来自哪一步逻辑，一行一个字段

从最终响应（extraction_json / explain / resolved_intent）构建，不重复执行解析：

  extract   metric=['revenue'] group_by=['region'] time=['last 7 days']
  turn      new_query (no_previous_state)
  metric    'revenue' → revenue · exact · alias 'revenue' · 100
  table     (not given) → orders · inferred_from_metric revenue (declared view)
  group_by  'region' → users.region · exact_collision_distance_resolved · 100
  time      'last 7 days' → last_n_days n=7
  joins     orders→users ✓
  result    success

用途：
- 每次请求以 INFO 级别写入日志（`[trace <session_id>] ...`）
- 响应里放在 explain["trace"]
- Telegram 回复默认不带；环境变量 TRACE_IN_REPLY=true 时附在回复末尾
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

_EXTRACTION_FIELDS = (
    ("table", "table_extractions"),
    ("metric", "metric_extractions"),
    ("column", "column_extractions"),
    ("group_by", "group_by_extractions"),
    ("filter", "filter_extractions"),
    ("time", "time_extractions"),
    ("window", "window_extractions"),
    ("order", "order_extractions"),
)


def trace_in_reply_enabled() -> bool:
    return os.getenv("TRACE_IN_REPLY", "false").lower() == "true"


def _texts(items: Any) -> List[str]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            out.append(it.get("text", ""))
        else:
            out.append(getattr(it, "text", str(it)))
    return [t for t in out if t]


def _field_line(name: str, fe: Dict[str, Any]) -> str:
    """table / metric：'原话' → 结果 · method · alias · score"""
    src = f"'{fe['input']}'" if fe.get("input") else "(not given)"
    value = fe.get("value") or "—"
    parts = [f"{name:<9} {src} → {value}", fe.get("method", "?")]
    if fe.get("alias"):
        parts.append(f"alias '{fe['alias']}'")
    if fe.get("score") is not None and fe.get("method") not in ("no_extractions",):
        parts.append(f"{float(fe['score']):.0f}")
    hits = (fe.get("user_preference_bias") or {}).get("top_preference_hits") or []
    if hits:
        parts.append("user preference: " + ", ".join(f"{h['value']} +{h['bias']}" for h in hits))
    if (fe.get("cross_encoder_rerank") or {}).get("applied"):
        parts.append("LLM rerank applied")
    return " · ".join(parts)


def _table_inference_line(inf: Dict[str, Any], final_tables: List[str]) -> str:
    method = inf.get("method", "?")
    value = inf.get("table") or (final_tables or ["—"])[0]
    if method == "explicit":
        return ""  # 已由 table 行说明
    if method == "inferred_from_metric":
        how = "declared view" if inf.get("source") == "declared_table" else f"expr {inf.get('expr', '')}"
        return f"{'table':<9} (not given) → {value} · inferred_from_metric {inf.get('metric')} ({how})"
    if method == "inferred_from_columns":
        return f"{'table':<9} (not given) → {value} · inferred_from_columns votes={inf.get('votes')}"
    return f"{'table':<9} (not given) → — · {method}"


def _column_line(e: Dict[str, Any]) -> str:
    role = e.get("role", "column")
    parts = [f"{role:<9} '{e.get('text', '')}' → {e.get('column') or '—'}", e.get("method", "?")]
    if e.get("alias"):
        parts.append(f"alias '{e['alias']}'")
    if e.get("score") is not None:
        parts.append(f"{float(e['score']):.0f}")
    if e.get("confirm_candidates"):
        parts.append("ask user: " + ", ".join(c["value"] for c in e["confirm_candidates"][:5]))
    if e.get("dropped"):
        parts.append("dropped")
    return " · ".join(parts)


def _filter_line(e: Dict[str, Any]) -> str:
    vn = e.get("value_normalized")
    value = vn["to"] if vn else e.get("value")  # 写进 SQL 的是规范化后的值
    target = f"{e.get('column')} {e.get('op')} {value}" if e.get("column") else "—"
    parts = [f"{'filter':<9} '{e.get('column_text', e.get('text', ''))}' → {target}", e.get("method", "?")]
    if e.get("alias"):
        parts.append(f"alias '{e['alias']}'")
    if vn:
        parts.append(f"value '{vn['from']}'→'{vn['to']}' ({vn['method']})")
    if e.get("dropped"):
        parts.append("dropped")
    return " · ".join(parts)


def _time_line(t: Dict[str, Any], resolved: Dict[str, Any]) -> str:
    tr = resolved.get("time_range") or {}
    if tr:
        value = f"{tr.get('type', '?')} n={tr.get('n', '?')}"
    elif t.get("pattern"):
        value = f"{t['pattern']} n={t.get('days', '?')}"
    elif t.get("method") == "fallback":
        value = f"last_n_days n={t.get('default', '?')}"
    else:
        value = "—"
    if t.get("method") == "fallback":
        return f"{'time':<9} (not given) → {value} · default"
    if t.get("method") == "followup_hint":
        return f"{'time':<9} follow-up hint → {value}"
    src = f"'{t['input']}'" if t.get("input") else "?"
    return f"{'time':<9} {src} → {value} · {t.get('method', '?')}"


def _join_line(j: Dict[str, Any]) -> str:
    steps = [f"{s['left']}→{s['right']} ✓" for s in j.get("steps", [])]
    missing = [f"{j.get('base_table')}→{t} ✗ no direct join" for t in j.get("missing_join", [])]
    body = " · ".join(steps + missing)
    return f"{'joins':<9} {body}" if body else ""


def _turn_line(explain: Dict[str, Any]) -> str:
    turn = explain.get("turn_explain") or {}
    decision = turn.get("decision") or explain.get("turn_decision") or {}
    mode = turn.get("mode") or decision.get("mode")
    if not mode:
        return ""
    line = f"{'turn':<9} {mode}"
    if decision.get("reason"):
        line += f" ({decision['reason']})"
    if turn.get("applied_patch_fields"):
        line += f" · patched {turn['applied_patch_fields']}"
    if turn.get("inherited_fields"):
        line += f" · inherited {turn['inherited_fields']}"
    if turn.get("confirmed_fields"):
        line += f" · user confirmed {turn['confirmed_fields']}"
    return line


def build_trace(result: Dict[str, Any]) -> List[str]:
    """从响应 dict 构建 trace 行（任何字段缺失都跳过，不抛异常）"""
    lines: List[str] = []
    explain = result.get("explain") or {}
    rex = explain.get("resolver_explain") or {}
    resolved = result.get("resolved_intent") or {}

    extraction = result.get("extraction_json") or {}
    ext_parts = [f"{label}={_texts(extraction.get(key))}"
                 for label, key in _EXTRACTION_FIELDS if _texts(extraction.get(key))]
    lines.append(f"{'extract':<9} " + (" ".join(ext_parts) if ext_parts else "(nothing)"))

    turn = _turn_line(explain)
    if turn:
        lines.append(turn)

    for name in ("metric", "table"):
        fe = rex.get(name)
        if fe and fe.get("method") != "no_extractions":
            lines.append(_field_line(name, fe))
    inf = rex.get("table_inference")
    if inf:
        line = _table_inference_line(inf, resolved.get("tables") or [])
        if line:
            lines.append(line)

    columns_parse = rex.get("columns_parse") or {}
    for e in columns_parse.get("columns", []) or rex.get("columns", []) or []:
        lines.append(_column_line(e))
    for e in columns_parse.get("filters", []) or rex.get("filters_parse", []) or []:
        lines.append(_filter_line(e))

    t = rex.get("time")
    if t:
        lines.append(_time_line(t, resolved))

    gen = rex.get("sql_generation") or explain.get("sql_generation") or {}
    j = gen.get("join_explain") or {}
    if j:
        jl = _join_line(j)
        if jl:
            lines.append(jl)

    status = result.get("status", "success")
    outcome = f"{'result':<9} {status}"
    if status == "needs_confirmation" and result.get("candidates"):
        outcome += " · ask " + "; ".join(
            f"{field}: " + ", ".join(c["value"] for c in cands[:5])
            for field, cands in result["candidates"].items())
    elif status == "early_exit" and result.get("message"):
        outcome += f" · {result['message'][:120]}"
    if gen.get("generation_failed"):
        outcome += f" · generation failed: {str(gen['generation_failed'])[:120]}"
    lines.append(outcome)
    return lines
