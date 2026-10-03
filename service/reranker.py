"""LLM Cross-Encoder 重排器（受限终选）

对齐参考文档 Semantic Resolution 的"受限 LLM 终选"模式：
- 只对 recall 已产生的候选重排打分，不得发明新值（输出值必须 ⊆ 输入候选）
- 仅在确认带触发（调用方保证）
- relevance / margin 过 matcher/policy.py 的 LLM_ACCEPT_* → 静默采纳（把"要问用户"变成"直接答对"）
- 否则保持确认流，但候选按相关性重排（最优选项排第一）
- 失败容错：LLM 异常/解析失败 → 返回原序，不影响主流程

默认关闭（RERANKER_ENABLED=false）。生产可替换为本地 cross-encoder 模型
（如 bge-reranker-v2-m3），接口不变。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from matcher import policy

logger = logging.getLogger(__name__)

RERANK_SYSTEM_PROMPT = r"""你是检索重排器（cross-encoder 角色）。给定用户查询和一组候选实体，判断每个候选是否是用户所指的对象，打相关性分（0-100）。

规则：
- 只能给给出的候选打分，禁止发明新候选
- 分数只反映"用户这个词最可能指哪个候选"，与 recall 分数无关
- 分数要有区分度：明确是用户所指 → 90+；可能相关 → 50-80；明显不对 → <30
"""

RERANK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "emit_rerank",
        "description": "输出候选重排结果",
        "parameters": {
            "type": "object",
            "properties": {
                "scores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                            "relevance": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["value", "relevance"],
                    },
                },
            },
            "required": ["scores"],
        },
    },
}


def is_reranker_enabled() -> bool:
    return os.getenv("RERANKER_ENABLED", "false").lower() == "true"


def _rerank_via_llm(query: str, field_name: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """同步 LLM 调用（由 rerank_candidates 用 to_thread 包装），便于测试 patch"""
    from service.llm_extractions import (
        _extract_json,
        _get_model_name,
        _supports_tool_calling,
        get_llm_client,
    )

    lines = [
        f"{i}. {c['value']} (recall_score={float(c.get('score', 0)):.0f})"
        for i, c in enumerate(candidates[:8], 1)
    ]
    user_content = (
        f"用户查询: {query}\n"
        f"字段类型: {field_name}\n"
        f"候选:\n" + "\n".join(lines) + "\n\n返回 JSON："
    )
    messages = [
        {"role": "system", "content": RERANK_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    client = get_llm_client()
    use_tool = _supports_tool_calling()
    kwargs: Dict[str, Any] = {
        "model": _get_model_name(),
        "messages": messages,
        "temperature": 0.1,
    }
    if use_tool:
        kwargs["tools"] = [RERANK_TOOL_SCHEMA]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": "emit_rerank"}}

    resp = client.chat.completions.create(**kwargs)
    message = resp.choices[0].message

    if use_tool and message.tool_calls:
        return json.loads(message.tool_calls[0].function.arguments)

    content = _extract_json(message.content or "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("reranker returned invalid JSON: %s", content[:200])
        return None


async def rerank_candidates(
    query: str, field_name: str, candidates: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """对候选做 cross-encoder 重排。

    Returns:
        (reranked_candidates, explain)
        explain.auto_accept=True 时，调用方可安全采纳 top1 而无需确认
    """
    if not query or not candidates or len(candidates) < 2:
        return candidates, {"applied": False, "reason": "insufficient_candidates"}

    try:
        raw = await asyncio.to_thread(_rerank_via_llm, query, field_name, candidates)
    except Exception as e:
        logger.warning("reranker llm failed (fallback to recall order): %s", e)
        return candidates, {"applied": False, "reason": f"llm_error: {e}"}

    if not raw or not raw.get("scores"):
        return candidates, {"applied": False, "reason": "empty_result"}

    # 受限终选：只接受输入候选中存在的值
    known_values = {c["value"] for c in candidates}
    by_value = {c["value"]: dict(c) for c in candidates}
    scored: List[Dict[str, Any]] = []
    seen = set()
    for s in raw["scores"]:
        value, relevance = s.get("value"), s.get("relevance")
        if value in known_values and value not in seen and relevance is not None:
            merged = by_value[value]
            merged["relevance"] = float(relevance)
            merged["relevance_reason"] = s.get("reason", "")
            scored.append(merged)
            seen.add(value)
    # 未被打分的候选补 0，保证集合完整
    for value, c in by_value.items():
        if value not in seen:
            merged = dict(c)
            merged["relevance"] = 0.0
            scored.append(merged)

    scored.sort(key=lambda x: (-x["relevance"], -float(x.get("score", 0))))

    top = scored[0]
    second_relevance = scored[1]["relevance"] if len(scored) > 1 else 0.0
    margin = round(top["relevance"] - second_relevance, 2)
    auto_accept = bool(
        top["relevance"] >= policy.LLM_ACCEPT_RELEVANCE and margin >= policy.LLM_ACCEPT_MARGIN
    )

    explain = {
        "applied": True,
        "backend": "llm_cross_encoder",
        "best": top["value"],
        "relevance": top["relevance"],
        "margin": margin,
        "auto_accept": auto_accept,
        "scores": [
            {"value": s["value"], "relevance": s["relevance"], "reason": s.get("relevance_reason", "")}
            for s in scored[:5]
        ],
    }
    logger.info(
        "Cross-encoder rerank [%s]: best=%s relevance=%.0f margin=%.0f auto_accept=%s",
        field_name, top["value"], top["relevance"], margin, auto_accept,
    )
    return scored, explain
