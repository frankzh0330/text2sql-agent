#!/usr/bin/env python3
"""
Text2SQL Agent MCP 服务器

提供三个调试命令：
- /token <query> - 测试分词
- /recall <query> - 测试召回（逐个 retriever）
- /rerank <query> - 测试完整匹配（召回 + 别名打分）
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from common.text_utils import normalize, tokenize_mixed
from matcher.entity_matcher import EntityMatcher, recall_tokens
from matcher.schema_loader import load_sql_schema


# 全局 matcher 实例（table / metric / column）
_matchers: dict[str, EntityMatcher] = {}


def _init_matchers():
    """初始化所有 matcher"""
    if not _matchers:
        schema = load_sql_schema("catalog")
        _matchers["table"] = EntityMatcher(schema.tables)
        _matchers["metric"] = EntityMatcher(schema.metrics)
        _matchers["column"] = EntityMatcher(schema.columns)


def _get_matcher(matcher_type: str) -> EntityMatcher:
    """根据类型获取 matcher"""
    _init_matchers()
    if matcher_type not in _matchers:
        raise ValueError(f"Unknown matcher_type: {matcher_type}")
    return _matchers[matcher_type]


# 创建 MCP 服务器
mcp = FastMCP("Text2SQL Agent")


@mcp.tool()
async def token(query: str) -> dict:
    """
    测试分词功能

    Returns:
        {
            "original": str,          # 原始查询
            "normalized": str,        # 规范化后的查询（exact 别名匹配用）
            "tokens": list[str],      # 分词结果
            "recall_tokens": list[str],  # 召回用 token（去停用词 + 复数归一）
        }
    """
    return {
        "original": query,
        "normalized": normalize(query),
        "tokens": tokenize_mixed(query),
        "recall_tokens": recall_tokens(query),
    }


@mcp.tool()
async def recall(query: str, matcher_type: str = "table", k: int = 10) -> dict:
    """
    测试召回功能（逐个 retriever）

    Args:
        query: 查询文本
        matcher_type: matcher 类型 (table/metric/column)
        k: 每个 retriever 返回的候选数

    Returns:
        {retriever_name: {"candidates": list[str], "explain": dict}}
    """
    matcher = _get_matcher(matcher_type)
    out = {}
    for r in matcher.retrievers:
        names, explain = r.retrieve(query, k)
        out[r.name] = {"candidates": names, "explain": explain}
    return {"query": query, "retrievers": out}


@mcp.tool()
async def rerank(query: str, matcher_type: str = "table") -> dict:
    """
    测试完整匹配（exact → 召回 → 别名打分）

    Returns:
        {"matched": str | None, "score": float, "candidates": list, "explain": dict}
    """
    r = _get_matcher(matcher_type).match(query)
    return {"query": query, "matched": r.matched, "score": r.score,
            "candidates": r.candidates, "explain": r.explain}


if __name__ == "__main__":
    # 初始化 matchers
    _init_matchers()
    print("Text2SQL Agent MCP Server initialized", file=sys.stderr)

    # 运行服务器
    mcp.run()
