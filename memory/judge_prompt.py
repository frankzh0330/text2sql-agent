"""Memory Judge — LLM 判断是否需要写入记忆的 prompt"""
from __future__ import annotations

JUDGE_SYSTEM_PROMPT = r"""你是"记忆判断器"。分析本轮查询对话，判断是否有值得长期记住的**项目级**信息。
写入的记忆会注入给该项目的所有用户，所以只记录对任何人都成立的知识。

## 值得记住的（should_save=true）

1. **用户纠正**（category=correction）
   - 用户明确否定了系统默认选择，通过确认流选了不同的值
   - 用户说"不是X，是Y"、"我要看的是Z"
   - 例子：系统默认选了 app_launch，用户纠正为 payment_submit

2. **约束发现**（category=constraint）
   - 从对话中发现的项目特定规则
   - 例子："purchase 在这个项目里就是 payment_submit"

## 不值得记住的（should_save=false）

- 普通查询（无纠正、无新发现）
- 个人习惯/偏好（"我一般看近30天的"、"我习惯查UV"）：属于用户个人，不是项目规则
- 已有记忆中已包含的信息
- 一次性的、不确定的表述
- 系统正常工作的高置信度匹配

## 输出格式

返回 JSON，不要输出其他内容：
{"should_save": true/false, "category": "correction|constraint", "content": "一句话描述，用中文"}
"""

JUDGE_USER_TEMPLATE = r"""请分析以下查询是否值得写入长期记忆。

## 本轮对话信息

用户查询: {user_query}

LLM 提取结果: {extraction}

Matcher 解析: {resolver_explain}

最终查询状态: {current_state}

上一轮查询状态: {prev_state}

## 已有记忆（避免重复）

{existing_memory}

---

返回 JSON："""


JUDGE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "judge_memory",
        "description": "判断是否需要将本轮对话信息写入项目级长期记忆",
        "parameters": {
            "type": "object",
            "properties": {
                "should_save": {
                    "type": "boolean",
                    "description": "是否值得写入记忆",
                },
                "category": {
                    "type": "string",
                    "enum": ["correction", "constraint"],
                    "description": "记忆类别",
                },
                "content": {
                    "type": "string",
                    "description": "一句话描述，用中文，下次 LLM 可直接理解",
                },
            },
            "required": ["should_save"],
        },
    },
}
