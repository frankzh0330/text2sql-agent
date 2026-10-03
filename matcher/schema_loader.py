"""Catalog 加载器：多元数据源 → 内存中的 SQLSchema

每个文件模拟一个企业里的独立元数据系统，各由一个 source adapter 读取，
再由 load_sql_schema 合并（matcher / AST analyzer 只依赖合并后的 SQLSchema）：

  catalog/tables.yaml   ← 物理 catalog（模拟 Unity Catalog）：表/列/类型/注释/owner/行数/枚举采样
  catalog/metrics.yaml  ← 语义层（模拟 LookML）：指标定义、join 图、默认时间维度
  catalog/aliases.yaml  ← alias 表：(entity_id, alias, source, confidence, status)

生产方向：把 adapter 换成对应系统的 API 客户端，定时同步后重建索引（见 README Production Notes）。

产出结构：
  tables:   {table: {aliases, alias_weights, description, owner, time_column, est_rows,
                     columns: {col: {type, comment, enum_values}}}}
  columns:  {"table.column": {table, column, type, comment, enum_values, aliases, alias_weights}}
  joins:    [{left, right, condition, relationship}]
  metrics:  {metric_id: {aliases, alias_weights, expr, table, description}}
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import yaml

logger = logging.getLogger(__name__)

TABLES_FILE = "tables.yaml"
METRICS_FILE = "metrics.yaml"
ALIASES_FILE = "aliases.yaml"

# alias 来源 → 默认置信度（单行可用 confidence 覆盖）
ALIAS_SOURCE_CONFIDENCE = {
    "catalog": 1.0,    # 规范名本身（loader 自动添加）
    "curated": 1.0,
    "glossary": 0.9,
    "comment": 0.7,
    "query_log": 0.6,
    "feedback": 0.6,
    "llm": 0.5,
}


@dataclass(frozen=True)
class SQLSchema:
    tables: Dict[str, Dict]
    columns: Dict[str, Dict]          # "table.column" -> {table, column, aliases, type, ...}
    joins: List[Dict] = field(default_factory=list)
    metrics: Dict[str, Dict] = field(default_factory=dict)
    join_graph: Dict[str, List[Dict]] = field(default_factory=dict)  # table -> [{peer, condition}]

    def normalize_enum_value(self, qualified_column: str, value):
        """把自然语言取值规范化为列声明的枚举值（确定性，不依赖 LLM）

        返回 (value, method)：
          method = "not_enum"   列未声明 enum_values，原样返回
                 = "exact"      已是规范值
                 = "normalized" 大小写/空格/连字符差异（"Credit Card" -> "credit_card"）
                 = "fuzzy"      近似匹配（RapidFuzz >= 90）
                 = "unmatched"  没有对应枚举值，原样返回（调用方可据此告警）
        """
        enums = (self.columns.get(qualified_column) or {}).get("enum_values") or []
        if not enums or not isinstance(value, str):
            return value, "not_enum"
        if value in enums:
            return value, "exact"

        def _key(v: str) -> str:
            return "".join(ch for ch in v.lower() if ch.isalnum())

        by_key = {_key(e): e for e in enums}
        k = _key(value)
        if k in by_key:
            return by_key[k], "normalized"
        from rapidfuzz import fuzz, process

        best = process.extractOne(k, list(by_key.keys()), scorer=fuzz.ratio)
        if best and best[1] >= 90:
            return by_key[best[0]], "fuzzy"
        return value, "unmatched"

    def columns_of_table(self, table: str) -> Dict[str, Dict]:
        return self.tables.get(table, {}).get("columns", {})

    def time_column_of(self, table: str) -> str:
        return self.tables.get(table, {}).get("time_column", "")

    def find_join(self, left: str, right: str) -> Dict | None:
        """查找两表之间的 join 定义（无向）"""
        for j in self.joins:
            pair = {j["left"], j["right"]}
            if left in pair and right in pair:
                return j
        return None

    def to_schema_prompt(self, max_tables: int = 10) -> str:
        """渲染给 LLM 的 schema 摘要（供 SQL 生成 prompt 使用）"""
        lines = []
        for t_name, t_info in list(self.tables.items())[:max_tables]:
            cols = []
            for c, c_info in t_info.get("columns", {}).items():
                col = f"{c} {c_info.get('type', 'String')}"
                if c_info.get("enum_values"):
                    col += " in (" + ",".join(f"'{v}'" for v in c_info["enum_values"]) + ")"
                cols.append(col)
            desc = t_info.get("description", "")
            lines.append(f"- {t_name} ({desc}): {', '.join(cols)}")
        for j in self.joins:
            lines.append(f"- JOIN: {j['left']} ↔ {j['right']} ON {j['condition']}")
        for m_id, m_info in self.metrics.items():
            lines.append(f"- METRIC {m_id} = {m_info['expr']}")
        return "\n".join(lines)


# =========================
# Source adapters（每个对应一个企业元数据系统）
# =========================

def _read_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_physical_catalog(path: str) -> Dict[str, Dict]:
    """物理 catalog（模拟 Unity Catalog list_tables）→ {table: {description, owner, est_rows, columns}}"""
    raw = _read_yaml(path)
    tables: Dict[str, Dict] = {}
    for t in raw.get("tables", []) or []:
        columns = {
            c["name"]: {
                "type": c.get("type_text", "String"),
                "comment": c.get("comment", ""),
                "enum_values": list(c.get("distinct_values", []) or []),
            }
            for c in t.get("columns", []) or []
        }
        tables[t["name"]] = {
            "description": t.get("comment", ""),
            "owner": t.get("owner", ""),
            "est_rows": int((t.get("properties") or {}).get("num_rows", 0) or 0),
            "columns": columns,
        }
    return tables


def load_semantic_layer(path: str) -> Tuple[Dict[str, str], List[Dict], Dict[str, Dict]]:
    """语义层（模拟 LookML）→ (time_dimensions, joins, metrics)"""
    raw = _read_yaml(path)
    time_dims = {
        view: (info or {}).get("time_dimension", "")
        for view, info in (raw.get("views", {}) or {}).items()
    }
    joins = [
        {"left": j["from"], "right": j["to"], "condition": j["sql_on"],
         "relationship": j.get("relationship", "")}
        for j in raw.get("joins", []) or []
    ]
    metrics = {
        m_id: {"expr": m["sql"], "table": m.get("view", ""), "description": m.get("description", "")}
        for m_id, m in (raw.get("metrics", {}) or {}).items()
    }
    return time_dims, joins, metrics


def load_alias_table(path: str) -> List[Dict]:
    """alias 表 → [{entity_id, alias, source, confidence}]（只保留 approved 行）"""
    raw = _read_yaml(path)
    rows: List[Dict] = []
    for r in raw.get("aliases", []) or []:
        source = r.get("source", "curated")
        if source not in ALIAS_SOURCE_CONFIDENCE:
            raise ValueError(f"aliases.yaml: unknown source {source!r} for {r}")
        if r.get("status", "approved") != "approved":
            continue
        rows.append({
            "entity_id": r["entity_id"],
            "alias": str(r["alias"]),
            "source": source,
            "confidence": float(r.get("confidence", ALIAS_SOURCE_CONFIDENCE[source])),
        })
    return rows


# =========================
# 合并
# =========================

def _attach_aliases(entity: Dict, canonical: str, rows: List[Dict]) -> None:
    """规范名（置信度 1.0）+ alias 表行 → entity["aliases"] / entity["alias_weights"]

    同一别名多来源时取最高置信度。
    """
    weights: Dict[str, float] = {canonical: ALIAS_SOURCE_CONFIDENCE["catalog"]}
    for r in rows:
        weights[r["alias"]] = max(weights.get(r["alias"], 0.0), r["confidence"])
    entity["aliases"] = list(weights.keys())
    entity["alias_weights"] = weights


def load_sql_schema(base_dir: str = "catalog") -> SQLSchema:
    """读取三个元数据源并合并为 SQLSchema（引用不存在的实体直接报错，fail fast）"""
    tables = load_physical_catalog(os.path.join(base_dir, TABLES_FILE))
    time_dims, joins, metrics = load_semantic_layer(os.path.join(base_dir, METRICS_FILE))
    alias_rows = load_alias_table(os.path.join(base_dir, ALIASES_FILE))

    by_entity: Dict[str, List[Dict]] = {}
    for r in alias_rows:
        by_entity.setdefault(r["entity_id"], []).append(r)

    # 语义层引用必须存在于物理 catalog
    for view, col in time_dims.items():
        if view not in tables:
            raise ValueError(f"metrics.yaml: view {view!r} not in physical catalog")
        if col and col not in tables[view]["columns"]:
            raise ValueError(f"metrics.yaml: time_dimension {view}.{col} not in physical catalog")
    for j in joins:
        for t in (j["left"], j["right"]):
            if t not in tables:
                raise ValueError(f"metrics.yaml: join references unknown table {t!r}")
    for m_id, m in metrics.items():
        if m["table"] and m["table"] not in tables:
            raise ValueError(f"metrics.yaml: metric {m_id!r} references unknown view {m['table']!r}")

    known_ids = set()
    columns: Dict[str, Dict] = {}
    for t_name, t_info in tables.items():
        t_info["time_column"] = time_dims.get(t_name, "")
        _attach_aliases(t_info, t_name, by_entity.get(f"table:{t_name}", []))
        known_ids.add(f"table:{t_name}")
        # 展开列为 "table.column" 文档（同名列可区分归属表）
        for c_name, c_info in t_info["columns"].items():
            qualified = f"{t_name}.{c_name}"
            col = {"table": t_name, "column": c_name, **c_info}
            _attach_aliases(col, c_name, by_entity.get(f"column:{qualified}", []))
            columns[qualified] = col
            known_ids.add(f"column:{qualified}")

    for m_id, m_info in metrics.items():
        _attach_aliases(m_info, m_id, by_entity.get(f"metric:{m_id}", []))
        known_ids.add(f"metric:{m_id}")

    unknown = sorted(set(by_entity) - known_ids)
    if unknown:
        raise ValueError(f"aliases.yaml: unknown entity_id(s) {unknown}")

    # join 邻接表（无向）
    join_graph: Dict[str, List[Dict]] = {}
    for j in joins:
        join_graph.setdefault(j["left"], []).append({"peer": j["right"], "condition": j["condition"]})
        join_graph.setdefault(j["right"], []).append({"peer": j["left"], "condition": j["condition"]})

    logger.debug(
        "Catalog loaded: %d tables, %d columns, %d metrics, %d alias rows",
        len(tables), len(columns), len(metrics), len(alias_rows),
    )
    return SQLSchema(tables=tables, columns=columns, joins=joins, metrics=metrics, join_graph=join_graph)
