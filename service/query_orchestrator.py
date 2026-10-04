"""查询编排器 — SQL 生成版核心业务逻辑

三条处理路径：
1. new_query: 完整的新查询（LLM 抽取意图 → Matcher 解析表/列/指标 → QueryState → LLM 生成 ClickHouse SQL）
2. followup_patch: 基于上轮状态的修改（检测 → 补丁 → 合并 → 重新生成 SQL）
3. confirmation: 用户确认低置信度候选（表 / 指标 / join 关系）

职责边界：
- LLM Layer 1 只抽取意图片段，SQL 结构组装由 generate_sql 完成
- 表/列/指标名字解析由 Matcher 确定性完成（倒排索引 + rapidfuzz），score 40-80 触发确认
- join 关系由 schema 配置推断，不靠 LLM 猜
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from common.types import MatcherType
from memory.memory_writer import MemoryWriter
from memory.user_preference_store import UserPreferenceStore
from service.followup_resolver import FollowupDecision, detect_followup
from service.llm_extractions import Extraction, extract_llm_async
from service.query_state_merger import merge_query_state
from service.resolution_trace import build_trace
from service.session_manager import SessionManager
from service.session_models import QueryState
from service.sql_ast_analyzer import build_analysis_context
from service.sql_generator import (
    generate_sql,
    parse_order_text,
    parse_window_text,
    time_range_to_ch_expr,
)
from service.task_manager import TaskManager

logger = logging.getLogger(__name__)



# ==================== 数据类 ====================

@dataclass
class FollowupPatchResult:
    patch: dict[str, Any]
    resolver_explain: Dict[str, Any]
    patch_hints: Dict[str, Any]
    confirmation_needed: Optional[tuple[str, Any]] = None
    unresolved_filters: Optional[list] = None


# ==================== 编排器 ====================

class QueryOrchestrator:
    """查询编排器：封装三条处理路径的业务逻辑"""

    def __init__(
        self,
        session_manager: SessionManager,
        task_manager: TaskManager,
        memory_writer: MemoryWriter,
        user_preference_store: UserPreferenceStore,
    ):
        self.session = session_manager
        self.task = task_manager
        self.memory = memory_writer
        self.preferences = user_preference_store

    # ==================== 主入口 ====================

    async def process(self, req, service, catalog=None, notify_fn=None) -> dict:
        """主入口：路由处理后附加 resolution trace（INFO 日志 + explain["trace"]）"""
        result = await self._route(req, service, notify_fn=notify_fn)
        try:
            trace = build_trace(result)
        except Exception as e:  # trace 只用于排查，绝不影响主流程
            logger.warning("build_trace failed: %s", e)
            return result
        sid = result.get("session_id") or req.session_id or "-"
        logger.info("[trace %s] input %r", sid, req.text)
        for line in trace:
            logger.info("[trace %s] %s", sid, line)
        result.setdefault("explain", {})["trace"] = trace
        return result

    async def _route(self, req, service, notify_fn=None) -> dict:
        """路由到三条处理路径

        Args:
            req: NL2SQLRequest
            service: MatcherService
            catalog: 兼容参数（SQL 版 schema 挂在 service.schema 上）
            notify_fn: async callable(chat_id, message) for Telegram notifications

        Returns:
            NL2SQLResponse dict
        """
        ctx = self.session.create_or_get(req.session_id, req.user_id, req.project_id)
        prev_qs = ctx.last_query_state

        # 1. 确认流拦截
        if ctx.pending_task_id:
            task = self.task.get_task(ctx.pending_task_id)
            if task and task.status == "waiting_confirmation":
                return await self._process_confirmation(req, ctx, task, service)

        # 2. LLM 抽取意图
        session_context = self.session.get_enhanced_context(ctx.session_id, query_text=req.text)
        timing = {}
        total_start = time.time()

        layer1_start = time.time()
        try:
            extraction_json = await extract_llm_async(req.text, session_context=session_context)
        except ValueError as e:
            # LLM 没返回合法 JSON（如对寒暄回了一段话）：不 500，返回可读提示
            logger.warning("Layer1 extraction failed: %s", str(e)[:200])
            self.session.add_message(ctx.session_id, "user", req.text, metadata={"error": "extraction_failed"})
            return self._make_response(
                extraction_json={}, status="early_exit", session_id=ctx.session_id,
                message="Could not identify a query intent in your message. Please rephrase (e.g. \"revenue by region last 7 days\").",
                explain={"extraction_error": str(e)[:200]},
            )
        timing["layer1_llm_extraction_s"] = round(time.time() - layer1_start, 3)
        logger.debug("Layer1: user=%s, intent=%s", req.text,
                      json.dumps(extraction_json.model_dump(), ensure_ascii=False))

        # 3. Follow-up 检测
        followup_decision = detect_followup(req.text, prev_qs, pending_task=False)
        logger.debug("Turn decision: mode=%s, confidence=%.2f, reason=%s",
                      followup_decision.mode, followup_decision.confidence, followup_decision.reason)

        # 4. Early exit（完全无查询信号且非 follow-up）
        has_signal = any([
            extraction_json.table_extractions,
            extraction_json.metric_extractions,
            extraction_json.column_extractions,
            extraction_json.filter_extractions,
            extraction_json.group_by_extractions,
            extraction_json.window_extractions,
            extraction_json.order_extractions,
        ])
        if not has_signal and not followup_decision.is_followup:
            self.session.add_message(ctx.session_id, "user", req.text, metadata={"error": "missing_query_signal"})
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id,
                message="You have not entered any table, metric or query condition, so no SQL can be generated.",
                explain={"turn_decision": _serialize_followup_decision(followup_decision)},
            )

        # 5. Follow-up 路径
        if followup_decision.is_followup and prev_qs is not None:
            return await self._process_followup(
                req, ctx, extraction_json, followup_decision, prev_qs, service,
            )

        # 6. 新查询路径
        return await self._process_new_query(
            req, ctx, extraction_json, followup_decision, service,
            timing, total_start, notify_fn=notify_fn,
        )

    # ==================== 新查询路径 ====================

    async def _process_new_query(
        self, req, ctx, extraction_json, followup_decision,
        service, timing, total_start, notify_fn=None,
    ) -> dict:
        """处理完整的新查询"""
        prev_qs = ctx.last_query_state

        # Layer2: Matcher 解析
        layer2_start = time.time()

        table_result, table_bias = await self._resolve_field(
            service, MatcherType.TABLE, extraction_json.table_extractions,
            None, "table", req.project_id, ctx.user_id, query_text=req.text,
        )
        metric_result, metric_bias = await self._resolve_field(
            service, MatcherType.METRIC, extraction_json.metric_extractions,
            None, "metric", req.project_id, ctx.user_id, query_text=req.text,
        )

        # 时间
        n_days, time_explain = service.resolve_time(extraction_json)
        time_range = _time_range_from(n_days, time_explain)

        # 主表先行推断（为列冲突的 join 距离消歧提供基表上下文）
        base_hint, _ = service.infer_main_table(
            [table_result.value] if table_result.value else [],
            [metric_result.value] if metric_result.value else [],
            [],
        )

        # 列解析（group_by / detail / filter column / window group）
        column_explain: Dict[str, Any] = {}
        group_by_cols, group_entries = self._resolve_texts_to_columns(
            service, [e.text for e in extraction_json.group_by_extractions], column_explain,
            base_table=base_hint, role="group_by",
        )
        detail_cols, detail_entries = self._resolve_texts_to_columns(
            service, [e.text for e in extraction_json.column_extractions], column_explain,
            base_table=base_hint, role="detail",
        )
        filters = self._resolve_filters(
            service, extraction_json.filter_extractions, column_explain, base_table=base_hint)
        if column_explain.get("filters_unresolved"):
            return self._unresolved_filters_response(
                req.text, extraction_json, ctx, column_explain["filters_unresolved"], {"columns": column_explain},
            )

        # window / order 意图（含方向词的 window 文本降级走全局 TopN 解析）
        window = None
        demoted_order_text = None
        if extraction_json.window_extractions:
            w_text = extraction_json.window_extractions[0].text
            w = parse_window_text(w_text)
            if not w:
                # L1 可能把窗口短语截断（如只抽到 "in each region"，"top 3" 留在原句）——
                # 用完整查询文本兜底解析，确定性找回 limit 和分组
                w = parse_window_text(req.text)
                if w:
                    column_explain["window"] = {"recovered_from_full_text": True, "fragment": w_text}
            if w:
                w_cols, _ = self._resolve_texts_to_columns(
                    service, [w["group_text"]], column_explain, base_table=base_hint, role="window")
                if w_cols:
                    window = {"group_by": w_cols[0], "limit": w["limit"]}
                else:
                    column_explain["window"] = {"dropped": True, "raw": w["raw"]}
            else:
                demoted_order_text = w_text

        order_by = None
        order_source = extraction_json.order_extractions[0].text if extraction_json.order_extractions else demoted_order_text
        if order_source:
            o = parse_order_text(order_source)
            if o:
                o_res, _ = await self._resolve_field(
                    service, MatcherType.METRIC, [Extraction(text=o["metric_text"])],
                    None, "metric", req.project_id, ctx.user_id, query_text=req.text,
                )
                # 排序词解析不出指标时（如"各品类销售额最高"切成"品类销售额"），回退到主指标
                order_metric = o_res.value or metric_result.value
                order_by = {
                    "metric": order_metric,
                    "metric_expr": service.get_metric_expr(order_metric) if order_metric else None,
                    "direction": o["direction"],
                    "limit": o["limit"],
                }

        timing["layer2_resolution_s"] = round(time.time() - layer2_start, 3)

        resolver_explain = self._build_resolver_explain(
            table_result, table_bias, metric_result, metric_bias,
            group_by_cols, detail_cols, filters, time_explain, column_explain,
            window=window, order_by=order_by,
        )

        # 确认流判断（表 / 指标）
        pending_fields = {}
        if table_result.needs_confirmation:
            pending_fields["tables"] = table_result.candidates
        if metric_result.needs_confirmation:
            pending_fields["metrics"] = metric_result.candidates
        # 列需确认（同名冲突无法消歧 / 低置信 / 并列）→ 升级确认
        group_pending = _first_pending_column(group_entries)
        if group_pending:
            pending_fields["group_by_column"] = group_pending
        detail_pending = _first_pending_column(detail_entries)
        if detail_pending:
            pending_fields["detail_column"] = detail_pending

        if pending_fields:
            return await self._create_confirmation_task(
                req, ctx, extraction_json, pending_fields, resolver_explain,
                table_result, metric_result, time_range,
                group_by_cols, detail_cols, filters, window, order_by,
                notify_fn=notify_fn,
            )

        # 组装 QueryState
        metric_ids = [metric_result.value] if metric_result.value else []
        tables = [table_result.value] if table_result.value else []
        query_state = self._assemble_query_state(
            req.project_id, tables, metric_ids, group_by_cols, detail_cols,
            filters, time_range, window, order_by, turn_type="new_query",
        )

        # 主表推断（用户可能不提表名）
        base_table, table_infer_explain = service.infer_main_table(
            tables, metric_ids, group_by_cols + detail_cols + [f["column"] for f in filters],
        )
        table_infer_explain = {**table_infer_explain, "table": base_table}
        if not base_table:
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id,
                message="Could not determine which table to query. Please name a table or use a known metric (e.g. revenue, number of orders).",
                explain={"resolver_explain": {**resolver_explain, "table_inference": table_infer_explain}},
            )
        query_state.tables = [base_table]
        resolver_explain["table_inference"] = table_infer_explain

        # join 推断 + 生成 SQL
        sql, gen_explain, join_error = await self._generate_sql_from_state(
            req.text, query_state, service,
        )
        if join_error:
            # join 缺失 → 确认流（候选 = 主表邻接表）
            resolver_explain["sql_generation"] = gen_explain
            return await self._create_join_confirmation(
                req, ctx, extraction_json, followup_decision, prev_qs,
                query_state, service, join_error, resolver_explain,
            )

        if gen_explain.get("generation_failed"):
            return self._generation_failed_response(req.text, extraction_json, ctx, gen_explain, resolver_explain)

        timing["total_s"] = round(time.time() - total_start, 3)
        resolver_explain["sql_generation"] = gen_explain

        # 会话记录
        self.session.add_message(ctx.session_id, "user", req.text, metadata={
            "tables": query_state.tables, "metrics": query_state.metrics,
        })
        self.session.update_query_state(ctx.session_id, query_state)
        self._record_preferences(ctx, query_state)

        # 异步记忆学习
        prev_state_dict = prev_qs.to_dict() if prev_qs else None
        asyncio.create_task(self.memory.maybe_save(
            project_id=req.project_id, user_query=req.text,
            extraction=extraction_json.model_dump(), resolver_explain=resolver_explain,
            current_state=query_state.to_dict(), prev_state=prev_state_dict,
        ))

        if req.chat_id and notify_fn:
            await notify_fn(req.chat_id, "SQL generated 👇")

        return self._make_response(
            extraction_json=extraction_json.model_dump(),
            sql=sql,
            resolved_intent=query_state.to_dict(),
            explain={
                "turn_explain": self._build_turn_explain("new_query", query_state, decision=followup_decision),
                "resolver_explain": resolver_explain, "timing": timing,
            },
            session_id=ctx.session_id,
        )

    # ==================== Follow-up 路径 ====================

    async def _process_followup(
        self, req, ctx, extraction_json, decision, prev_qs, service,
    ) -> dict:
        """处理 follow-up 补丁查询"""
        patch_result = await self._build_followup_patch(
            req.text, extraction_json, decision, service,
            project_id=req.project_id, user_id=ctx.user_id,
            base_table=(prev_qs.tables or [None])[0],
        )

        if patch_result.unresolved_filters:
            return self._unresolved_filters_response(
                req.text, extraction_json, ctx, patch_result.unresolved_filters, patch_result.resolver_explain,
            )

        if patch_result.confirmation_needed:
            field_name, resolved = patch_result.confirmation_needed
            return await self._create_followup_confirmation(
                req, ctx, extraction_json, decision, prev_qs, field_name, resolved,
            )

        logger.debug("Follow-up patch built: patch=%s, session_id=%s",
                      json.dumps(patch_result.patch, ensure_ascii=False), ctx.session_id)

        merged_state = merge_query_state(prev_qs, patch_result.patch, req.project_id)
        sql, gen_explain, join_error = await self._generate_sql_from_state(req.text, merged_state, service)
        if join_error:
            patch_result.resolver_explain["sql_generation"] = gen_explain
            return await self._create_join_confirmation(
                req, ctx, extraction_json, decision, prev_qs,
                merged_state, service, join_error, patch_result.resolver_explain,
            )

        if gen_explain.get("generation_failed"):
            return self._generation_failed_response(
                req.text, extraction_json, ctx, gen_explain, patch_result.resolver_explain)

        self.session.add_message(ctx.session_id, "user", req.text,
                                  metadata={"turn_mode": "followup_patch", "patch": patch_result.patch})
        self.session.update_query_state(ctx.session_id, merged_state)
        self._record_preferences(ctx, merged_state)

        return self._make_response(
            extraction_json=extraction_json.model_dump(),
            sql=sql,
            resolved_intent=merged_state.to_dict(),
            explain={
                "turn_explain": self._build_turn_explain("followup_patch", merged_state,
                                                          decision=decision, patch=patch_result.patch),
                "resolver_explain": {**patch_result.resolver_explain, "sql_generation": gen_explain},
            },
            session_id=ctx.session_id,
        )

    # ==================== 确认路径 ====================

    async def _process_confirmation(self, req, ctx, task, service) -> dict:
        """处理用户对确认任务的回复"""
        user_input = req.text.strip()
        confirmed_values = {}

        for field_name, candidates in task.candidates.items():
            matched = _match_user_input_to_candidates(user_input, candidates)
            if matched:
                self.task.confirm_task(task.task_id, field_name, matched)
                confirmed_values[field_name] = matched
                logger.info("User confirmed %s=%s for task %s", field_name, matched, task.task_id)

        if not confirmed_values:
            hint_msg = self._format_unmatched_message(task.candidates)
            return self._make_response(
                extraction_json=task.extraction, status="needs_confirmation",
                session_id=ctx.session_id, message=hint_msg,
                task_id=task.task_id, candidates=task.candidates,
            )

        all_confirmed = all(f in task.user_selection for f in task.candidates)
        if not all_confirmed:
            remaining = {f: c for f, c in task.candidates.items() if f not in task.user_selection}
            hint_msg = f"Confirmed: {confirmed_values}\n" + self._format_candidates_message(remaining)
            return self._make_response(
                extraction_json=task.extraction, status="needs_confirmation",
                session_id=ctx.session_id, message=hint_msg,
                task_id=task.task_id, candidates=remaining,
            )

        # 全部确认
        self.session.update_pending_task(ctx.session_id, None)
        self.task.update_status(task.task_id, "confirmed")

        qs = task.partial_query_state
        for field, value in task.user_selection.items():
            if field == "tables":
                qs.tables = [value]
            elif field == "metrics":
                qs.metrics = [value]
            elif field == "join":
                extra_table = value
                if extra_table not in qs.tables:
                    qs.tables.append(extra_table)
            elif field == "group_by_column":
                if value not in (qs.group_by or []):
                    qs.group_by = [*(qs.group_by or []), value]
            elif field == "detail_column":
                if value not in (qs.detail_columns or []):
                    qs.detail_columns = [*(qs.detail_columns or []), value]
        # 确认补齐的列可能带来新表（如选择了 payments.amount）→ 无基表时按列归属推断
        if not qs.tables:
            inferred_base, _ = service.infer_main_table(
                [], qs.metrics or [], (qs.group_by or []) + (qs.detail_columns or []),
            )
            if inferred_base:
                qs.tables = [inferred_base]
        _mark_confirmation_state(qs, task.user_selection)

        sql, gen_explain, join_error = await self._generate_sql_from_state(task.raw_query, qs, service)
        if join_error:
            hint_msg = f"Still unable to determine the join relationship: {join_error['missing']}"
            return self._make_response(
                extraction_json=task.extraction, status="needs_confirmation",
                session_id=ctx.session_id, message=hint_msg,
                task_id=task.task_id,
            )

        if gen_explain.get("generation_failed"):
            return self._generation_failed_response(task.raw_query, task.extraction, ctx, gen_explain)

        self.session.add_message(ctx.session_id, "user", task.raw_query)
        self.session.add_message(ctx.session_id, "user", user_input, metadata={"confirmed": task.user_selection})
        self.session.update_query_state(ctx.session_id, qs)
        self._record_preferences(ctx, qs)
        self.task.update_status(task.task_id, "completed")

        asyncio.create_task(self.memory.maybe_save(
            project_id=qs.project_id, user_query=task.raw_query,
            extraction=task.extraction, resolver_explain={"user_confirmed": task.user_selection},
            current_state=qs.to_dict(), confirmed_selection=task.user_selection,
        ))

        return self._make_response(
            extraction_json=task.extraction,
            sql=sql,
            resolved_intent=qs.to_dict(),
            explain={
                "confirmed": task.user_selection, "method": "user_confirmation",
                "turn_explain": self._build_turn_explain("confirmation", qs, confirmed_fields=task.user_selection),
                "sql_generation": gen_explain,
            },
            session_id=ctx.session_id, status="success",
            message=f"SQL generated from your selection: {task.user_selection}",
        )

    # ==================== 确认任务创建 ====================

    async def _create_confirmation_task(
        self, req, ctx, extraction_json, pending_fields, resolver_explain,
        table_result, metric_result, time_range,
        group_by_cols, detail_cols, filters, window, order_by, notify_fn=None,
    ) -> dict:
        """创建确认任务并返回 needs_confirmation 响应"""
        partial_state = self._assemble_query_state(
            req.project_id,
            [table_result.value] if table_result.value else [],
            [metric_result.value] if metric_result.value else [],
            group_by_cols, detail_cols, filters, time_range, window, order_by,
            turn_type="confirmation",
        )
        task = self.task.create_task(
            session_id=ctx.session_id, raw_query=req.text,
            extraction=extraction_json.model_dump(), user_id=ctx.user_id,
            candidates=pending_fields, partial_query_state=partial_state,
        )
        self.session.update_pending_task(ctx.session_id, task.task_id)

        confirm_msg = self._format_candidates_message(pending_fields) + "\nReply with a number or a name."
        if req.chat_id and notify_fn:
            await notify_fn(req.chat_id, confirm_msg)

        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="needs_confirmation",
            session_id=ctx.session_id, message=confirm_msg,
            task_id=task.task_id, candidates=pending_fields,
            explain={"resolver_explain": resolver_explain},
        )

    async def _create_followup_confirmation(
        self, req, ctx, extraction_json, decision, prev_qs, field_name, resolved_result,
    ) -> dict:
        """Follow-up 中触发确认流"""
        partial_state = merge_query_state(prev_qs, {}, req.project_id)
        task = self.task.create_task(
            session_id=ctx.session_id, raw_query=req.text,
            extraction=extraction_json.model_dump(), user_id=ctx.user_id,
            candidates={field_name: resolved_result.candidates},
            partial_query_state=partial_state,
        )
        self.session.update_pending_task(ctx.session_id, task.task_id)

        field_display = {
            "tables": "table", "metrics": "metric",
            "group_by_column": "group-by column", "detail_column": "column",
        }.get(field_name, field_name)
        lines = [f"Please choose a {field_display}:"]
        lines += [f"  {i}. {c['value']} (match {c['score']:.0f}%)" for i, c in enumerate(resolved_result.candidates[:5], 1)]
        lines.append("Reply with a number or a name.")

        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="needs_confirmation",
            session_id=ctx.session_id, message="\n".join(lines),
            task_id=task.task_id, candidates={field_name: resolved_result.candidates},
            explain={"turn_explain": {
                "mode": "followup_patch", "reason": decision.reason,
                "decision": _serialize_followup_decision(decision),
            }},
        )

    async def _create_join_confirmation(
        self, req, ctx, extraction_json, decision, prev_qs,
        query_state, service, join_error, resolver_explain,
    ) -> dict:
        """join 路径缺失时确认流：候选 = 主表的邻接表"""
        peers = service.schema.join_graph.get(query_state.tables[0], [])
        candidates = [{"value": p["peer"], "score": 100.0} for p in peers]

        if not candidates:
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id,
                message=f"Cannot determine how {join_error['missing']} relates to the base table. Please add the join to the schema config.",
                explain={"resolver_explain": resolver_explain},
            )

        task = self.task.create_task(
            session_id=ctx.session_id, raw_query=req.text,
            extraction=extraction_json.model_dump(), user_id=ctx.user_id,
            candidates={"join": candidates},
            partial_query_state=query_state,
        )
        self.session.update_pending_task(ctx.session_id, task.task_id)

        lines = [f"No direct join is configured between table {query_state.tables[0]} and {', '.join(join_error['missing'])}. Please choose a bridging table:"]
        lines += [f"  {i}. {c['value']}" for i, c in enumerate(candidates[:5], 1)]
        lines.append("Reply with a number or a name.")

        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="needs_confirmation",
            session_id=ctx.session_id, message="\n".join(lines),
            task_id=task.task_id, candidates={"join": candidates},
            explain={"resolver_explain": resolver_explain},
        )

    # ==================== SQL 生成 ====================

    async def _generate_sql_from_state(self, query_text, qs: QueryState, service):
        """从 QueryState 生成 ClickHouse SQL

        Returns:
            (sql, gen_explain, join_error) — join_error 非 None 表示 join 缺失需确认
        """
        base_table = qs.tables[0] if qs.tables else None
        if not base_table:
            raise ValueError("QueryState has no base table")

        # 派生列集合：group_by / detail / filter column / window group / 额外表
        filter_cols = [f["column"] for f in (qs.filters or []) if f.get("column")]
        derived_columns = sorted(set(
            list(qs.group_by or []) + list(qs.detail_columns or []) + filter_cols
            + ([qs.window["group_by"]] if qs.window and qs.window.get("group_by") else [])
        ))
        qs.columns = derived_columns

        # join 推断
        needed_tables = set(qs.tables[1:]) | {c.split(".")[0] for c in derived_columns}
        join_steps, join_explain = service.infer_joins(base_table, sorted(needed_tables))
        if join_explain.get("missing_join"):
            return "", {"join_explain": join_explain}, {"missing": join_explain["missing_join"]}

        # 时间表达式
        time_column = service.schema.time_column_of(base_table)
        time_expr = time_range_to_ch_expr(qs.time_range, f"{base_table}.{time_column}" if time_column else "")

        # order_by 的 metric_expr 补全
        order_by = None
        if qs.order_by:
            order_by = dict(qs.order_by)
            if not order_by.get("metric_expr"):
                order_by["metric_expr"] = service.get_metric_expr(order_by.get("metric", ""))

        intent = {
            "base_table": base_table,
            "metrics": [{"id": m, "expr": service.get_metric_expr(m)} for m in (qs.metrics or [])],
            "detail_columns": list(qs.detail_columns or []),
            "group_by": list(qs.group_by or []),
            "filters": list(qs.filters or []),
            "time_expr": time_expr,
            "joins": join_steps,
            "window": qs.window,
            "order_by": order_by,
        }

        try:
            sql, gen_explain = await generate_sql(
                query_text, intent, service.to_schema_prompt(),
                list(service.schema.tables.keys()),
                analysis_context=build_analysis_context(service.schema),
            )
        except ValueError as e:
            # 校验/修复循环耗尽：不向上抛 500，交给调用方转成可读的失败响应
            logger.warning("SQL generation failed: %s", e)
            return "", {"generation_failed": str(e), "join_explain": join_explain}, None
        gen_explain["join_explain"] = join_explain
        return sql, gen_explain, None

    # ==================== 统一 resolve + bias + cross-encoder ====================

    async def _resolve_field(self, service, matcher_type, extractions, default, field_name,
                             project_id, user_id, query_text=None):
        """统一 resolve：用户偏好在判定前弱加权候选；仍需确认时（可选）交给 LLM 受限终选"""
        bias_explain: Dict[str, Any] = {}

        def bias(candidates):
            reranked, explain = self.preferences.rerank_candidates(
                project_id, user_id, field_name, candidates)
            bias_explain.update(explain)
            return reranked

        result = service.resolve_with_candidates(matcher_type, extractions, default=default, bias=bias)
        if query_text and result.needs_confirmation:
            await self._apply_cross_encoder(result, query_text, field_name, bias_explain)
        return result, bias_explain

    async def _apply_cross_encoder(self, result, query_text, field_name, bias_explain):
        """LLM cross-encoder 受限终选（RERANKER_ENABLED 开启、且结果在确认带时触发）

        - 相关性/分差过 policy.LLM_ACCEPT_* → 静默采纳（免一次确认打断）
        - 否则保持确认流，仅按相关性重排候选（最优排第一）
        - explain 记录在 bias_explain["cross_encoder_rerank"]
        """
        from service import reranker

        if not reranker.is_reranker_enabled() or len(result.candidates) < 2:
            return

        reranked, rexplain = await reranker.rerank_candidates(query_text, field_name, result.candidates)
        bias_explain["cross_encoder_rerank"] = rexplain
        if not rexplain.get("applied"):
            return

        top = reranked[0]
        result.candidates = reranked
        if rexplain.get("auto_accept"):
            result.value = top["value"]
            result.score = float(top["relevance"])
            result.method = f"{result.method}+cross_encoder"
            result.needs_confirmation = False

    # ==================== 列 / 过滤解析 ====================

    def _resolve_texts_to_columns(
        self, service, texts: list[str], explain_sink: dict, base_table: Optional[str] = None,
        role: str = "column",
    ) -> tuple[list[str], list[dict]]:
        """自然语言片段 → 限定列名列表

        - 只接受无需确认的结果（exact / fuzzy / 冲突按 join 距离消歧）
        - 需要确认（exact 冲突无法消歧、低置信、并列）→ entry["confirm_candidates"]，
          由调用方升级确认流；不再静默取召回 top1
        - 低于确认下限 → 丢弃
        """
        resolved: list[str] = []
        entries: list[dict] = []
        for text in texts:
            if not text or not text.strip():
                continue
            r = service.resolve_with_candidates(
                MatcherType.COLUMN, [Extraction(text=text)], base_table=base_table)
            entry = {"role": role, "text": text, "method": r.method, "score": r.score}
            if r.matched_alias:
                entry["alias"] = r.matched_alias
            if r.needs_confirmation:
                entry["confirm_candidates"] = r.candidates
            elif r.value:
                entry["column"] = r.value
                resolved.append(r.value)
            else:
                entry["dropped"] = True
            entries.append(entry)
        if entries:
            # 追加而非覆盖：group_by / detail / window 各调一次，记录都要保留
            explain_sink.setdefault("columns", []).extend(entries)
        return resolved, entries

    def _resolve_filters(
        self, service, filter_extractions, explain_sink: dict, base_table: Optional[str] = None,
    ) -> list[dict]:
        """filter_extractions → 结构化 filters

        - column 只接受高置信匹配（exact/fuzzy/冲突距离消歧）。低置信或无匹配不再取 recall top1
          静默猜列（LLM 可能编造列，如 "user_type" 会被模糊匹配成 orders.user_id），
          而是记入 explain_sink["filters_unresolved"]，由调用方向用户澄清。
        - 同名列冲突：有基表上下文时按 join 距离确定性消歧（base=orders 下 "amount" ->
          orders.amount）；无法消歧则进 unresolved 澄清路径。
        - value 按列声明的 enum_values 规范化（"credit card" -> "credit_card"）。
        """
        filters: list[dict] = []
        entries: list[dict] = []
        unresolved: list[dict] = []
        for fe in filter_extractions:
            col_text = fe.column or fe.text
            r = service.resolve_with_candidates(
                MatcherType.COLUMN, [Extraction(text=col_text)], base_table=base_table)
            confident = bool(r.value) and not r.needs_confirmation and r.method in (
                "exact", "fuzzy", "exact_collision_distance_resolved")
            qualified = r.value if confident else None
            entry = {"text": fe.text, "column_text": col_text, "column": qualified,
                     "op": fe.op, "value": fe.value, "method": r.method, "score": r.score}
            if r.matched_alias:
                entry["alias"] = r.matched_alias
            if not confident:
                entry["dropped"] = True
                unresolved.append({
                    "text": fe.text, "column_text": col_text,
                    "candidates": [c["value"] for c in r.candidates[:3]],
                })
            elif fe.value is None:
                entry["dropped"] = True
            else:
                value = fe.value
                if fe.op in ("=", "!="):
                    value, enum_method = service.schema.normalize_enum_value(qualified, value)
                    if enum_method not in ("not_enum", "exact"):
                        entry["value_normalized"] = {"from": fe.value, "to": value, "method": enum_method}
                filters.append({"column": qualified, "op": fe.op, "value": value})
            entries.append(entry)
        if entries:
            explain_sink["filters"] = entries
        if unresolved:
            explain_sink["filters_unresolved"] = unresolved
        return filters

    def _unresolved_filters_response(self, text, extraction_json, ctx, unresolved: list[dict], resolver_explain=None) -> dict:
        """过滤列无法可靠解析 → 澄清（不静默猜列）。early_exit 通道，网关会把 message 回给用户。"""
        parts = []
        for u in unresolved:
            hint = f" (similar columns: {', '.join(u['candidates'])})" if u["candidates"] else ""
            parts.append(f"'{u['text']}'{hint}")
        msg = ("Could not map the following filters to a known column. Please rephrase or use a column name: "
               + "; ".join(parts))
        self.session.add_message(ctx.session_id, "user", text, metadata={"error": "unresolved_filter_column"})
        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="early_exit",
            session_id=ctx.session_id, message=msg,
            explain={"resolver_explain": resolver_explain or {}, "filters_unresolved": unresolved},
        )

    # ==================== Follow-up Patch ====================

    async def _build_followup_patch(
        self, req_text, extraction_json, decision, service,
        *, project_id, user_id, base_table: Optional[str] = None,
    ) -> FollowupPatchResult:
        """构建 follow-up 补丁，返回结果对象（不再用异常控制流）"""
        patch: dict[str, Any] = {}
        resolver_explain: Dict[str, Any] = {}
        column_explain: Dict[str, Any] = {}
        confirmation = None

        # Table
        if extraction_json.table_extractions:
            r, _ = await self._resolve_field(service, MatcherType.TABLE,
                extraction_json.table_extractions, None, "table", project_id, user_id,
                query_text=req_text)
            resolver_explain["table"] = _resolved_explain(r)
            if r.needs_confirmation and confirmation is None:
                confirmation = ("tables", r)
            else:
                patch["tables"] = [r.value]

        # Metric
        if extraction_json.metric_extractions:
            r, _ = await self._resolve_field(service, MatcherType.METRIC,
                extraction_json.metric_extractions, None, "metric", project_id, user_id,
                query_text=req_text)
            resolver_explain["metric"] = _resolved_explain(r)
            if r.needs_confirmation and confirmation is None:
                confirmation = ("metrics", r)
            else:
                patch["metrics"] = [r.value]

        # GroupBy（列解析）
        if extraction_json.group_by_extractions:
            cols, _ = self._resolve_texts_to_columns(
                service, [e.text for e in extraction_json.group_by_extractions], column_explain,
                base_table=base_table, role="group_by")
            if cols:
                patch["group_by"] = cols

        # 明细列
        if extraction_json.column_extractions:
            cols, _ = self._resolve_texts_to_columns(
                service, [e.text for e in extraction_json.column_extractions], column_explain,
                base_table=base_table, role="detail")
            if cols:
                patch["detail_columns"] = cols

        # 过滤
        if extraction_json.filter_extractions:
            filters = self._resolve_filters(
            service, extraction_json.filter_extractions, column_explain, base_table=base_table)
            if filters:
                patch["filters"] = filters

        # Time
        if extraction_json.time_extractions:
            n_days, time_explain = service.resolve_time(extraction_json)
            resolver_explain["time"] = time_explain
            patch["time_range"] = _time_range_from(n_days, time_explain)
        elif decision.patch_hints.get("time_range"):
            patch["time_range"] = decision.patch_hints["time_range"]
            resolver_explain["time"] = {"method": "followup_hint", "value": decision.patch_hints["time_range"]}

        # Window（含方向词时降级为全局 TopN）
        if extraction_json.window_extractions:
            w_text = extraction_json.window_extractions[0].text
            w = parse_window_text(w_text) or parse_window_text(req_text)
            if w:
                w_cols, _ = self._resolve_texts_to_columns(
                    service, [w["group_text"]], column_explain, base_table=base_table, role="window")
                if w_cols:
                    patch["window"] = {"group_by": w_cols[0], "limit": w["limit"]}
            else:
                o = parse_order_text(w_text)
                if o:
                    o_res, _ = await self._resolve_field(
                        service, MatcherType.METRIC, [Extraction(text=o["metric_text"])],
                        None, "metric", project_id, user_id, query_text=req_text,
                    )
                    patch["order_by"] = {
                        "metric": o_res.value,
                        "metric_expr": service.get_metric_expr(o_res.value),
                        "direction": o["direction"],
                        "limit": o["limit"],
                    }

        # Order
        if extraction_json.order_extractions:
            o = parse_order_text(extraction_json.order_extractions[0].text)
            if o:
                o_res, _ = await self._resolve_field(
                    service, MatcherType.METRIC, [Extraction(text=o["metric_text"])],
                    None, "metric", project_id, user_id, query_text=req_text)
                patch["order_by"] = {
                    "metric": o_res.value,
                    "metric_expr": service.get_metric_expr(o_res.value),
                    "direction": o["direction"],
                    "limit": o["limit"],
                }

        if column_explain:
            resolver_explain["columns"] = column_explain.get("columns", [])
            resolver_explain["filters_parse"] = column_explain.get("filters", [])

        return FollowupPatchResult(
            patch=patch, resolver_explain=resolver_explain,
            patch_hints=decision.patch_hints, confirmation_needed=confirmation,
            unresolved_filters=column_explain.get("filters_unresolved"),
        )

    # ==================== 状态组装 / 偏好 ====================

    _SQL_STATE_FIELDS = ("tables", "metrics", "detail_columns", "filters",
                         "time_range", "group_by", "order_by", "limit", "window")

    def _assemble_query_state(
        self, project_id, tables, metrics, group_by, detail_cols,
        filters, time_range, window, order_by, turn_type,
    ) -> QueryState:
        explicit = []
        field_sources: Dict[str, str] = {}
        values = {
            "tables": tables, "metrics": metrics, "detail_columns": detail_cols,
            "filters": filters, "time_range": time_range, "group_by": group_by,
            "order_by": order_by, "window": window,
        }
        for name in self._SQL_STATE_FIELDS:
            if values.get(name):
                explicit.append(name)
                field_sources[name] = "explicit"
        return QueryState(
            project_id=project_id,
            tables=tables, metrics=metrics, detail_columns=detail_cols,
            filters=filters, time_range=time_range, group_by=group_by,
            order_by=order_by, window=window,
            explicit_fields=explicit, field_sources=field_sources,
            turn_type=turn_type,
        )

    def _record_preferences(self, ctx, qs: QueryState) -> None:
        self.preferences.record_selection(
            qs.project_id, ctx.user_id,
            table=qs.tables[0] if qs.tables else None,
            metric=qs.metrics[0] if qs.metrics else None,
            columns=(qs.group_by or [])[:2],
        )

    # ==================== explain / 消息 ====================

    def _build_resolver_explain(
        self, tr, tb, mr, mb, group_by_cols, detail_cols, filters,
        time_explain, column_explain, *, window=None, order_by=None,
    ) -> dict:
        def _field_explain(result, bias):
            e = _resolved_explain(result)
            if bias.get("applied"):
                e["user_preference_bias"] = bias
            if bias.get("cross_encoder_rerank"):
                e["cross_encoder_rerank"] = bias["cross_encoder_rerank"]
            return e
        return {
            "table": _field_explain(tr, tb),
            "metric": _field_explain(mr, mb),
            "group_by": group_by_cols,
            "detail_columns": detail_cols,
            "filters": filters,
            "time": time_explain,
            "columns_parse": column_explain,
            "window": window,
            "order_by": order_by,
        }

    def _build_turn_explain(self, mode, qs, *, decision=None, patch=None, confirmed_fields=None) -> dict:
        explain = {
            "mode": mode,
            "explicit_fields": qs.explicit_fields,
            "inherited_fields": qs.inherited_fields,
            "field_sources": qs.field_sources,
            "state_snapshot": _build_state_snapshot(qs),
        }
        if decision is not None:
            explain["decision"] = _serialize_followup_decision(decision)
        if patch is not None:
            explain["applied_patch"] = patch
            explain["applied_patch_fields"] = list(patch.keys())
        if confirmed_fields:
            explain["confirmed_fields"] = confirmed_fields
        return explain

    def _format_candidates_message(self, pending_fields: dict) -> str:
        """格式化候选提示消息"""
        field_display_map = {
            "tables": "table", "metrics": "metric", "join": "bridging table",
            "group_by_column": "group-by column", "detail_column": "column",
        }
        lines = []
        for field_name, cands in pending_fields.items():
            lines.append(f"Please choose a {field_display_map.get(field_name, field_name)}:")
            for i, c in enumerate(cands[:5], 1):
                lines.append(f"  {i}. {c['value']} (match {c['score']:.0f}%)")
        return "\n".join(lines)

    def _format_unmatched_message(self, candidates: dict) -> str:
        """格式化未匹配候选的消息"""
        lines = ["Could not recognize your choice. Reply with a number or a name:"]
        lines.append(self._format_candidates_message(candidates))
        return "\n".join(lines)

    def _generation_failed_response(self, text, extraction_json, ctx, gen_explain: dict, resolver_explain=None) -> dict:
        reason = gen_explain.get("generation_failed", "")
        self.session.add_message(ctx.session_id, "user", text, metadata={"error": "sql_generation_failed"})
        return self._make_response(
            extraction_json=extraction_json.model_dump() if hasattr(extraction_json, "model_dump") else extraction_json,
            status="early_exit", session_id=ctx.session_id,
            message=f"Could not generate SQL that passes validation. Please rephrase and try again. Reason: {reason}",
            explain={"resolver_explain": resolver_explain or {}, "sql_generation": gen_explain},
        )

    @staticmethod
    def _make_response(**kwargs) -> dict:
        """构建响应 dict（保持字段顺序）"""
        defaults = {"sql": "", "resolved_intent": {}, "explain": {}, "status": "success"}
        defaults.update(kwargs)
        return defaults


# ==================== 模块级工具函数 ====================

def _time_range_from(days: int, explain: Dict[str, Any]) -> Dict[str, Any]:
    """TimeMatcher explain → QueryState.time_range"""
    return {"type": explain.get("pattern", "last_n_days"), "n": days}


def _build_state_snapshot(qs: QueryState) -> Dict[str, Any]:
    return {
        "tables": qs.tables, "metrics": qs.metrics,
        "time_range": qs.time_range,
        "filters": qs.filters,
        "group_by": qs.group_by,
        "window": qs.window, "order_by": qs.order_by,
    }


def _serialize_followup_decision(decision: FollowupDecision) -> Dict[str, Any]:
    return {
        "mode": decision.mode, "confidence": decision.confidence,
        "reason": decision.reason, "matched_signals": decision.matched_signals,
        "patch_hints": decision.patch_hints, "normalized_text": decision.normalized_text,
    }


def _match_user_input_to_candidates(user_input: str, candidates: list[dict]) -> str | None:
    text = user_input.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]["value"]
    text_lower = text.lower()
    for c in candidates:
        if c["value"].lower() == text_lower:
            return c["value"]
    for c in candidates:
        if text_lower in c["value"].lower() or c["value"].lower() in text_lower:
            return c["value"]
    return None


def _resolved_explain(r) -> dict:
    """ResolvedResult → explain（含原话片段、结果与命中别名，供 trace 定位来源）"""
    e = {"input": r.input_text, "value": r.value, "method": r.method, "score": r.score}
    if r.matched_alias:
        e["alias"] = r.matched_alias
    return e


def _first_pending_column(entries: list[dict]) -> Optional[list[dict]]:
    """取第一个需确认列的候选列表（用于升级确认流）"""
    for e in entries:
        if e.get("confirm_candidates"):
            return e["confirm_candidates"]
    return None


def _mark_confirmation_state(qs: QueryState, confirmed_fields: Dict[str, str]) -> None:
    qs.turn_type = "confirmation"
    merged_explicit = list(dict.fromkeys([*(qs.explicit_fields or []), *confirmed_fields.keys()]))
    qs.explicit_fields = merged_explicit
    active_fields = [f for f in QueryOrchestrator._SQL_STATE_FIELDS if getattr(qs, f, None)]
    qs.inherited_fields = [f for f in active_fields if f not in merged_explicit]
    field_sources = dict(qs.field_sources or {})
    for f in active_fields:
        if f in confirmed_fields:
            field_sources[f] = "confirmed"
        elif f not in field_sources:
            field_sources[f] = "explicit" if f in merged_explicit else "inherited"
    qs.field_sources = field_sources
