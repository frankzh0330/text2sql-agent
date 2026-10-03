from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from common.types import MatcherType
from matcher import policy
from matcher.entity_matcher import EntityMatcher
from matcher.schema_loader import SQLSchema, load_sql_schema
from matcher.time_matcher import TimeMatcher, resolve_last_n_days

logger = logging.getLogger(__name__)


@dataclass
class ResolvedResult:
    """解析结果（含候选列表和置信度）"""
    value: str                      # 最终值（matched / inferred / default；列名为 "table.column"）
    score: float                    # 置信度 (0-100)
    method: str                     # 见 resolve_with_candidates 的 method 取值
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    needs_confirmation: bool = False  # 是否需要用户确认


BiasFn = Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]


class MatcherService:
    """Matcher 服务：表 / 列 / 业务指标 / 时间 的确定性解析

    三种实体共用 EntityMatcher（召回 + 别名打分），本类负责 accept / confirm 判定、
    主表与 join 推断。
    """

    def __init__(self, catalog_path: str = "catalog"):
        logger.info("Initializing MatcherService (sql schema)...")

        self.schema: SQLSchema = load_sql_schema(catalog_path)

        logger.info("Building matcher indexes...")
        self.table_matcher = EntityMatcher(self.schema.tables)
        self.column_matcher = EntityMatcher(self.schema.columns)
        self.metric_matcher = EntityMatcher(self.schema.metrics)
        self.time_matcher = TimeMatcher()
        logger.info("MatcherService initialized successfully!")

    # ==================== 带候选的解析方法 ====================

    def resolve_with_candidates(
        self, matcher_type: MatcherType, extractions: list, default: Optional[str] = None,
        base_table: Optional[str] = None, bias: Optional[BiasFn] = None,
    ) -> ResolvedResult:
        """从 LLM 提取结果中解析，返回完整候选和判定（阈值全部来自 matcher/policy.py）

        固定顺序：
        1. EntityMatcher 产出候选（exact / 召回 + 别名打分）
        2. bias（可选，用户偏好弱加权）只重排候选分数，在判定之前生效；
           阈值（采纳线 / CONFIRM_FLOOR）只看召回原始分 raw_score，偏好只能在都过线的候选间
           打破并列，不能单凭加分把低置信候选推成静默采纳
        3. exact 别名冲突：仅列冲突且恰为两路、有基表上下文时，按 join 图距离唯一最近者
           确定性胜出（revenue 语境下 "region" -> users.region）；否则确认流
        4. 分档：top1 >= 类型采纳线且不与第二名并列 → 采纳；>= CONFIRM_FLOOR 或并列 → 确认；
           否则视为无匹配（返回 default，不确认）

        method: exact | fuzzy | exact_collision_distance_resolved | exact_alias_collision |
                fuzzy_tied | fuzzy_low_confidence | no_match | no_extractions
                （偏好改变了 top1 时追加 "+user_bias"）
        """
        if not extractions:
            return ResolvedResult(value=default or "", score=0.0, method="no_extractions")

        first = extractions[0]
        query_text = first.text if hasattr(first, "text") else first.get("text", "")

        result = self._get_matcher(matcher_type).match(query_text)
        candidates = [{"value": c["name"], "score": float(c["score"])} for c in result.candidates]
        if not candidates and result.matched:
            candidates = [{"value": result.matched, "score": float(result.score)}]
        recall_top = candidates[0]["value"] if candidates else None
        if bias and candidates:
            candidates = bias(candidates)
        suffix = "+user_bias" if candidates and candidates[0]["value"] != recall_top else ""

        if result.explain.get("method") == "exact_alias_collision":
            resolved = self._resolve_exact_collision(matcher_type, base_table, candidates)
            if resolved is not None:
                return ResolvedResult(value=resolved, score=100.0,
                                      method="exact_collision_distance_resolved", candidates=candidates)
            return ResolvedResult(value=default or "", score=100.0, method="exact_alias_collision",
                                  candidates=candidates, needs_confirmation=True)

        top_raw = float(candidates[0].get("raw_score", candidates[0]["score"])) if candidates else 0.0
        if not candidates or top_raw < policy.CONFIRM_FLOOR:
            return ResolvedResult(value=default or "", score=float(result.score),
                                  method="no_match", candidates=candidates)

        top = candidates[0]
        tied = len(candidates) >= 2 and candidates[1]["score"] >= top["score"] - policy.TIE_MARGIN
        if top_raw >= policy.ACCEPT_SCORE[matcher_type] and not tied:
            return ResolvedResult(value=top["value"], score=top["score"],
                                  method=("exact" if top["score"] >= 100.0 else "fuzzy") + suffix,
                                  candidates=candidates)
        method = "fuzzy_tied" if tied and top_raw >= policy.ACCEPT_SCORE[matcher_type] else "fuzzy_low_confidence"
        return ResolvedResult(value=top["value"], score=top["score"], method=method + suffix,
                              candidates=candidates, needs_confirmation=True)

    def resolve_time(self, extraction: Any) -> Tuple[int, Dict[str, Any]]:
        """解析时间范围 → (days, explain)；days 由 sql_generator 翻译为 CH 表达式"""
        return resolve_last_n_days(extraction)

    # ==================== 表推断 ====================

    def infer_main_table(
        self,
        resolved_tables: List[str],
        resolved_metrics: List[str],
        resolved_columns: List[str],
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """主表推断（用户经常不提表名）：

        1. 显式解析出的表 → 直接用
        2. 无表但有指标 → 从指标聚合表达式提取表（revenue = sum(orders.amount) → orders）
        3. 无表无指标但有列 → 按列归属表投票
        """
        if resolved_tables:
            return resolved_tables[0], {"method": "explicit", "tables": resolved_tables}

        # 1) 指标显式声明的归属表（count() 类无表引用表达式只能靠这个）
        # 2) 从指标聚合表达式中提取表（revenue = sum(orders.amount) → orders）
        for m_id in resolved_metrics:
            m_info = self.schema.metrics.get(m_id, {})
            declared = m_info.get("table")
            if declared and declared in self.schema.tables:
                return declared, {"method": "inferred_from_metric", "metric": m_id,
                                  "source": "declared_table"}
            expr = m_info.get("expr", "")
            for t_name in self.schema.tables:
                if f"{t_name}." in expr:
                    return t_name, {
                        "method": "inferred_from_metric",
                        "metric": m_id, "expr": expr,
                    }

        if resolved_columns:
            table_votes: Dict[str, int] = {}
            for qualified in resolved_columns:
                t = qualified.split(".")[0]
                table_votes[t] = table_votes.get(t, 0) + 1
            best = max(table_votes.items(), key=lambda kv: kv[1])[0]
            return best, {"method": "inferred_from_columns", "votes": table_votes}

        return None, {"method": "no_signal"}

    # ==================== join 推断 ====================

    def infer_joins(
        self, base_table: str, qualified_columns: List[str]
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        """根据查询涉及的列推断需要 join 的表

        列在非主表上（如 orders 查询涉及 users.region）→ 查 join 配置生成步骤。
        join 是无向匹配，输出统一归一化为 {left: base, right: peer, condition}。
        找不到 join 路径的表由调用方走确认流。
        """
        needed_tables = {q.split(".")[0] for q in qualified_columns}
        steps: List[Dict[str, str]] = []
        for t in sorted(needed_tables):
            if t == base_table:
                continue
            j = self.schema.find_join(base_table, t)
            if j:
                cond = j.get("condition") or j.get(True) or j.get("on") or ""
                peer = j["right"] if j["left"] == base_table else j["left"]
                steps.append({"left": base_table, "right": peer, "condition": cond})

        missing = sorted(
            t for t in needed_tables
            if t != base_table and t not in {s["right"] for s in steps}
        )
        explain = {
            "base_table": base_table,
            "needed_tables": sorted(needed_tables),
            "steps": steps,
            "missing_join": missing,
        }
        return steps, explain

    # ==================== exact 冲突消歧 ====================

    def _resolve_exact_collision(
        self, matcher_type: MatcherType, base_table: Optional[str], candidates: List[Dict[str, Any]]
    ) -> Optional[str]:
        """两路列冲突 + 基表上下文 → join 图距离唯一最近者胜出；其余情况返回 None（确认流）"""
        if matcher_type != MatcherType.COLUMN or base_table is None or len(candidates) != 2:
            return None
        dists = []
        for c in candidates:
            table = str(c["value"]).split(".")[0]
            d = self._join_distance(base_table, table)
            if d is None:
                return None  # 不可达（无声明 join 路径）→ 不猜
            dists.append((d, str(c["value"])))
        dists.sort()
        if dists[0][0] < dists[1][0]:
            return dists[0][1]
        return None  # 距离并列 → 确认流

    def _join_distance(self, from_table: str, to_table: str, max_hops: int = 4) -> Optional[int]:
        """join 图 BFS 距离（同表 0；不可达 None）"""
        if from_table == to_table:
            return 0
        graph = self.schema.join_graph
        visited = {from_table}
        frontier = [from_table]
        for hop in range(1, max_hops + 1):
            nxt = []
            for t in frontier:
                for edge in graph.get(t, []):
                    peer = edge["peer"]
                    if peer == to_table:
                        return hop
                    if peer not in visited:
                        visited.add(peer)
                        nxt.append(peer)
            frontier = nxt
            if not frontier:
                break
        return None

    # ==================== 查询辅助 ====================

    def get_metric_expr(self, metric_id: str) -> str:
        return self.schema.metrics.get(metric_id, {}).get("expr", metric_id)

    def to_schema_prompt(self) -> str:
        """渲染 schema 摘要（SQL 生成 prompt 的上下文）"""
        return self.schema.to_schema_prompt()

    def _get_matcher(self, matcher_type: MatcherType):
        if matcher_type == MatcherType.TABLE:
            return self.table_matcher
        elif matcher_type == MatcherType.COLUMN:
            return self.column_matcher
        elif matcher_type == MatcherType.METRIC:
            return self.metric_matcher
        raise ValueError(f"Unknown matcher_type: {matcher_type}")
