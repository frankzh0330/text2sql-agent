from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

logger = logging.getLogger(__name__)

# ==================== 缓存机制 ====================
_query_cache: Dict[str, dict] = {}


def _query_hash(text: str) -> str:
    """生成查询的哈希值用于缓存"""
    return hashlib.md5(text.encode()).hexdigest()


def _is_cache_enabled() -> bool:
    """检查是否启用缓存"""
    return os.getenv("ENABLE_LLM_CACHE", "true").lower() == "true"


# ==================== 会话上下文 ====================
def _build_context_str(session_context: Optional[Dict[str, Any]]) -> str:
    """构建动态会话上下文字符串（注入到提示词）

    只保留有价值的动态上下文：
    1. QueryState — 上一轮 SQL 查询意图（上下文继承，最高价值）
    2. 最近用户消息 — 辅助理解
    记忆文件纠正内容在 _build_messages 中单独注入。
    """
    if not session_context:
        return ""

    parts = []

    # QueryState — 最高价值
    if session_context.get("last_query_state"):
        qs = session_context["last_query_state"]
        state_parts = []
        if qs.get("tables"):
            state_parts.append(f"表={','.join(qs['tables'])}")
        if qs.get("metrics"):
            state_parts.append(f"指标={','.join(qs['metrics'])}")
        if qs.get("columns"):
            state_parts.append(f"列={','.join(qs['columns'])}")
        if qs.get("time_range"):
            tr = qs["time_range"]
            state_parts.append(f"时间=近{tr.get('n', '?')}天")
        if qs.get("group_by"):
            state_parts.append(f"分组={','.join(qs['group_by'])}")
        if qs.get("filters"):
            filter_strs = [f"{f.get('column')}{f.get('op')}{f.get('value')}" for f in qs["filters"]]
            state_parts.append(f"过滤={','.join(filter_strs)}")
        if qs.get("window"):
            w = qs["window"]
            state_parts.append(f"窗口排名={w.get('group_by')}前{w.get('limit')}")

        if state_parts:
            parts.append(f"上次查询: {', '.join(state_parts)}")

    # 最近用户消息 — 辅助理解
    recent = session_context.get("recent_queries", [])
    if recent:
        history_lines = [f"Q{i}: {q}" for i, q in enumerate(recent[-3:], 1)]
        parts.append("=== 最近对话 ===\n" + "\n".join(history_lines) + "\n=== 历史结束 ===")

    return "\n".join(parts) if parts else ""


def _is_followup_turn(session_context: Optional[Dict[str, Any]]) -> bool:
    """是否处于更可能是 follow-up 的轮次。"""
    if not session_context:
        return False
    return bool(session_context.get("last_query_state")) and bool(session_context.get("turn_index", 0))


def _build_followup_instruction(session_context: Optional[Dict[str, Any]]) -> str:
    """为多轮 follow-up 场景追加更偏 patch extraction 的指令。"""
    if not _is_followup_turn(session_context):
        return ""

    return (
        "\n\n=== 多轮查询补充规则 ===\n"
        "如果当前问题看起来是在补充、修改或缩写上一轮查询，请优先抽取本轮明确提到的新信息。\n"
        "不要为了补全而重复输出用户本轮没有明确说出的表、指标、列、过滤条件。\n"
        "如果本轮只说了时间、过滤或分组变化，就只抽取这些变化。\n"
        "只有当用户本轮明确提到表或指标时，才填充对应 extractions。\n"
        "=== 规则结束 ===\n"
    )


def _extract_json(content: str) -> str:
    """
    从内容中提取第一个完整的 JSON 对象

    处理 LLM 可能返回的额外数据：
    1. 移除 markdown 代码块标记
    2. 提取第一个完整的 JSON 对象
    3. 忽略 JSON 后的额外内容
    """
    content = content.strip()

    # 移除 markdown 代码块标记
    if content.startswith("```"):
        end_marker = content.find("\n```", 3)
        if end_marker != -1:
            content = content[3:end_marker].strip()
        else:
            lines = content.split("\n", 1)
            if len(lines) > 1:
                content = lines[1].strip()
            if content.startswith("json"):
                content = content[4:].strip()

    if content.startswith("```json"):
        content = content[7:].strip()
    elif content.startswith("```"):
        content = content[3:].strip()

    start = content.find("{")
    if start == -1:
        start = content.find("[")
    if start == -1:
        return content

    if content[start] == "{":
        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(content)):
            char = content[i]

            if escape_next:
                escape_next = False
                continue

            if char == "\\":
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return content[start:i + 1]

    return content[start:]


# ==================== 数据模型 ====================

class Extraction(BaseModel):
    text: str


class FilterExtraction(BaseModel):
    """过滤条件抽取：column/value 为自然语言片段，由 matcher 解析"""
    text: str
    column: Optional[str] = None   # attribute phrase as the user said it, e.g. "membership level" (resolved by the column EntityMatcher)
    op: str = "="                  # = | != | > | < | >= | <= | in | like
    value: Optional[str] = None    # 如 "vip"（LLM 直接给出或从 text 截取）


class SQLIntentJson(BaseModel):
    """Layer 1 LLM 抽取结果 — SQL 查询意图片段（不做任何解析/翻译）"""
    table_extractions: List[Extraction] = Field(default_factory=list)
    metric_extractions: List[Extraction] = Field(default_factory=list)
    column_extractions: List[Extraction] = Field(default_factory=list)
    filter_extractions: List[FilterExtraction] = Field(default_factory=list)
    group_by_extractions: List[Extraction] = Field(default_factory=list)
    time_extractions: List[Extraction] = Field(default_factory=list)
    order_extractions: List[Extraction] = Field(default_factory=list)    # "销售额最高的前3"
    window_extractions: List[Extraction] = Field(default_factory=list)   # "每个地区前3"


# ==================== Prompt ====================

FEW_SHOT_EXAMPLES = r"""
Examples (copy the user's own wording into "text"; never translate):
revenue by region for the last 7 days -> {"metric_extractions":[{"text":"revenue"}],"group_by_extractions":[{"text":"region"}],"time_extractions":[{"text":"last 7 days"}]}
orders from gold members -> {"metric_extractions":[{"text":"orders"}],"filter_extractions":[{"text":"gold members","column":"membership level","op":"=","value":"gold"}]}
revenue paid by credit card -> {"metric_extractions":[{"text":"revenue"}],"filter_extractions":[{"text":"paid by credit card","column":"payment method","op":"=","value":"credit card"}]}
new users in the last 30 days -> {"metric_extractions":[{"text":"new users"}],"time_extractions":[{"text":"last 30 days"}]}
list orders over 100 -> {"table_extractions":[{"text":"orders"}],"filter_extractions":[{"text":"over 100","column":"amount","op":">","value":"100"}]}
top 3 categories by revenue per region -> {"metric_extractions":[{"text":"revenue"}],"group_by_extractions":[{"text":"category"}],"window_extractions":[{"text":"top 3 per region"}]}
top 5 by average order value -> {"metric_extractions":[{"text":"average order value"}],"order_extractions":[{"text":"top 5 by average order value"}]}
"""

_BASE_SYSTEM_PROMPT = r"""You are an "extractor". Your only job: pull the verbatim phrases that express SQL query intent out of the user's question (extractions).
You do NOT generate SQL and you do NOT translate or normalize names: table / metric / column names are resolved downstream by matchers. You only split and classify.

Language rule: the user may write English or Chinese. Copy the user's own words into "text" (and "column"). Never translate, never switch languages, never output a column name the user did not say.

Extraction rules:
- table_extractions: tables the user explicitly mentions ("orders", "users", "products", "payments", "reviews", "sellers")
  - Only extract when a table (or a phrase clearly naming one) is mentioned. Do not guess.

- metric_extractions: business metrics (usually aggregated values)
  - "revenue", "GMV", "sales" -> as written
  - "number of orders", "order count" -> as written
  - "average order value", "AOV", "refund rate", "average rating", "new users" -> as written
  - A metric phrase is a METRIC, never a filter: "new users" is a metric, not user_type = new.

- column_extractions: plain columns (not metrics; used for detail listing or implicit grouping/filtering)
  - "order id", "amount", "status", "region", "category" -> as written

- filter_extractions: filter conditions
  - "orders from gold members" -> {"text":"gold members","column":"membership level","op":"=","value":"gold"}
  - "amount over 100" -> {"text":"amount over 100","column":"amount","op":">","value":"100"}
  - "paid orders" -> {"text":"paid orders","column":"status","op":"=","value":"paid"}
  - op must be one of: = | != | > | < | >= | <= | in | like
  - "column" must be the attribute phrase the user used (e.g. "membership level", "payment method"); do not invent column names such as user_type or is_new.
  - "value" is the value as the user said it (lower-case is fine); value normalization is done downstream.
  - If you cannot name the attribute from the user's words, do not emit a filter.

- group_by_extractions: grouping dimensions
  - "by region", "per channel", "for each category" -> the dimension phrase ("region", "channel", "category")

- time_extractions: time range, verbatim
  - "last 7 days", "yesterday", "this week", "last month", "past 14 days"

- order_extractions: explicit ordering / TopN over the whole result
  - "top 5 by revenue", "highest average order value" -> verbatim

- window_extractions: ranking within each group (top N per X)
  - "top 3 per region", "each category top 5" -> verbatim
  - Distinguish: "top 5 categories by revenue" (global TopN -> order_extractions) vs "top 3 per region" (within group -> window_extractions)

""" + FEW_SHOT_EXAMPLES



# Prompt-based 专用 prompt（含格式指令）
_BASE_SYSTEM_PROMPT_TEXT = _BASE_SYSTEM_PROMPT + r"""
Output requirements:
- No explanatory text
- No Markdown code fences (no ```)
- Output raw JSON only, starting with { and ending with }
"""


# ==================== Tool Schema (Function Calling) ====================

EXTRACT_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_sql_intent",
        "description": "Extract the verbatim SQL-query-intent phrases from the user question (do not translate)",
        "parameters": {
            "type": "object",
            "properties": {
                "table_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Tables mentioned, e.g. orders, users",
                },
                "metric_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Business metrics, e.g. revenue, number of orders, average order value (a metric phrase such as 'new users' is never a filter)",
                },
                "column_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Plain columns, e.g. order id, region, category",
                },
                "filter_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "column": {"type": "string"},
                            "op": {"type": "string", "enum": ["=", "!=", ">", "<", ">=", "<=", "in", "like"]},
                            "value": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                    "description": "Filters, e.g. gold members, amount over 100. 'column' must be the attribute phrase the user used; never invent column names",
                },
                "group_by_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Grouping dimensions, e.g. region, channel, category",
                },
                "time_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Time range, verbatim, e.g. last 7 days, yesterday, this month",
                },
                "order_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Global ordering / TopN, e.g. top 5 by revenue",
                },
                "window_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "Ranking within each group, e.g. top 3 per region",
                },
            },
            "required": [],
        },
    },
}


# ==================== LLM Client ====================

_llm_client: OpenAI | None = None


def get_llm_client() -> OpenAI:
    """获取 OpenAI 兼容客户端（模块级单例，复用连接池）"""
    global _llm_client
    if _llm_client is not None:
        return _llm_client

    backend = os.getenv("LLM_BACKEND", "zhipu").lower()

    if backend == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        _llm_client = OpenAI(
            api_key="ollama",
            base_url=base_url,
        )
    else:  # zhipu (默认)
        api_key = os.getenv("ZHIPU_API_KEY", os.getenv("ZHIPUAI_API_KEY", ""))
        base_url = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
        _llm_client = OpenAI(api_key=api_key, base_url=base_url)

    return _llm_client


def _get_model_name() -> str:
    """获取模型名称"""
    backend = os.getenv("LLM_BACKEND", "zhipu").lower()
    if backend == "ollama":
        return os.getenv("OLLAMA_MODEL", "glm4")
    return os.getenv("ZHIPU_MODEL", "glm-4")


def _supports_tool_calling() -> bool:
    """检查是否启用 function calling

    默认 true，所有 OpenAI 兼容后端均可使用。
    设 TOOL_CALLING_ENABLED=false 可强制走 prompt-based 路径。
    """
    return os.getenv("TOOL_CALLING_ENABLED", "true").lower() == "true"


# ==================== 构建消息 ====================

def _build_messages(query: str, session_context: Optional[Dict[str, Any]],
                    use_tool_calling: bool) -> list[dict[str, str]]:
    """构建 OpenAI 兼容的 messages 列表

    注入结构（按顺序追加到 system prompt）：
    1. 记忆文件内容（纠正/约束）
    2. 动态上下文（QueryState + 最近消息）
    3. follow-up patch extraction 指令
    """
    system_prompt = _BASE_SYSTEM_PROMPT if use_tool_calling else _BASE_SYSTEM_PROMPT_TEXT

    # 层 1：记忆文件内容（纠正/约束）
    if session_context and session_context.get("memory_corrections"):
        corrections = session_context["memory_corrections"]
        system_prompt += (
            f"\n\n=== 已知约束和纠正 ===\n"
            f"{corrections}\n"
            f"=== 约束结束 ===\n"
            f"请遵循上述约束处理用户查询。\n"
        )

    # 层 2：动态上下文（QueryState + 最近消息）
    context_str = _build_context_str(session_context)
    if context_str:
        system_prompt += "\n\n请参考上述上下文理解用户当前问题。\n" + context_str

    # 层 3：follow-up patch extraction 指令
    followup_instruction = _build_followup_instruction(session_context)
    if followup_instruction:
        system_prompt += followup_instruction

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户问题：\n{query}"},
    ]

    if not use_tool_calling:
        messages[-1]["content"] += "\n\n返回 JSON："

    return messages


# ==================== 提取逻辑 ====================

def _parse_result(data: dict) -> SQLIntentJson:
    """解析并验证提取结果"""
    return SQLIntentJson.model_validate(data)


def extract_llm(query: str, session_context: Optional[Dict[str, Any]] = None) -> SQLIntentJson:
    """LLM 抽取 SQL 查询意图（支持会话上下文）

    Args:
        query: 用户查询文本
        session_context: 会话上下文，包含历史对话和已解析状态
    """
    # 检查缓存（暂不支持带上下文的缓存）
    if _is_cache_enabled() and not session_context:
        cache_key = _query_hash(query)
        if cache_key in _query_cache:
            logger.info(f"extract_llm: cache hit for query: {query}")
            return SQLIntentJson(**_query_cache[cache_key])

    client = get_llm_client()
    model = _get_model_name()
    use_tool_calling = _supports_tool_calling()
    messages = _build_messages(query, session_context, use_tool_calling)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    logger.debug(f"LLM extract: model={model}, tool_calling={use_tool_calling}, query={query[:80]}")

    if use_tool_calling:
        kwargs["tools"] = [EXTRACT_INTENT_TOOL]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": "extract_sql_intent"}}

    resp = client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    message = choice.message

    # Function Calling 路径：从 tool_calls 提取结构化参数
    if use_tool_calling and message.tool_calls:
        args = json.loads(message.tool_calls[0].function.arguments)
        logger.debug(f"LLM extract (tool_call): {json.dumps(args, ensure_ascii=False)[:200]}")
        result = _parse_result(args)
    else:
        # Prompt-based 路径（fallback 或 Ollama）
        content = message.content if hasattr(message, "content") else str(message)
        content = _extract_json(content)
        logger.debug(f"LLM extract (prompt): raw={content[:200]}")
        try:
            data = json.loads(content)
            result = _parse_result(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(f"LLM extract parse failed: {e}, raw={content[:300]}")
            raise ValueError(f"LLM SQLIntentJson 输出不合法: {e}\nRaw:\n{content}")

    # 保存到缓存（仅无上下文时）
    if _is_cache_enabled() and not session_context:
        _query_cache[cache_key] = result.model_dump()

    return result


async def extract_llm_async(query: str, session_context: Optional[Dict[str, Any]] = None) -> SQLIntentJson:
    """extract_llm 的异步包装，通过 asyncio.to_thread 避免阻塞事件循环"""
    return await asyncio.to_thread(extract_llm, query, session_context)
