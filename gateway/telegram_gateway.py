from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

from bus.message_schema import BusMessage
from gateway.base import BaseGateway
from ingress.telegram_adapter import TelegramAdapter
from service.resolution_trace import trace_in_reply_enabled

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_CHARS = 4096  # Telegram sendMessage 文本上限


class TelegramGateway(BaseGateway):
    """
    Telegram Bot Gateway

    使用 long polling 方式接收消息。
    通过 MessageBus 将消息传递给 AgentWorker 处理，不再直接调用业务逻辑。
    """

    def __init__(self, bot_token: Optional[str] = None, bus=None):
        super().__init__(channel="telegram", bus=bus)
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._running = False
        self._offset: int = 0  # 用于 long polling

    async def start(self) -> None:
        """启动 Gateway，开始轮询消息"""
        if not self.bot_token:
            logger.warning("Telegram bot token not configured, gateway will not start")
            return

        self._running = True
        logger.info("TelegramGateway started")

        while self._running:
            try:
                await self._poll_updates()
            except Exception as e:
                # 带异常类型名，避免 httpx 超时类异常 str() 为空导致日志不可诊断
                logger.warning("Error polling updates: %s: %s", type(e).__name__, e)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 Gateway"""
        self._running = False
        logger.info("TelegramGateway stopped")

    async def _poll_updates(self) -> None:
        """轮询获取更新"""
        url = f"{self.base_url}/getUpdates"
        params = {
            "offset": self._offset + 1,
            "timeout": 30,
        }

        try:
            async with httpx.AsyncClient() as client:
                # 客户端超时放宽到 60s：长轮询 30s + 网络抖动余量
                response = await client.get(url, params=params, timeout=60)
        except httpx.TransportError as e:
            # TimeoutException 的父类，再覆盖 ReadError/ConnectError 等网络层抖动：
            # 长轮询循环本来就会重试，静默降为 DEBUG 即可
            logger.debug("Poll transport error (retrying): %s: %s", type(e).__name__, e)
            return

        data = response.json()

        if not data.get("ok"):
            logger.error(f"Failed to get updates: {data}")
            return

        updates = data.get("result", [])
        for update in updates:
            self._offset = update.get("update_id", self._offset)
            await self.handle_message(update)

    async def handle_message(self, update: Dict[str, Any]) -> None:
        """处理消息：Ingress 清洗 → 入队"""
        adapter = TelegramAdapter()
        std_msg = adapter.adapt(update)

        # 检查是否重复
        if std_msg.is_duplicate:
            logger.info(f"Duplicate message ignored: {std_msg.message_id}")
            return

        # 检查清洗后是否为空
        if not std_msg.text:
            logger.info(f"Empty message after cleaning: {std_msg.raw_text}")
            return

        logger.info(
            f"Received message: user={std_msg.user_id}, "
            f"chat={std_msg.chat_id}, text={std_msg.text}"
        )

        # 构建统一消息并通过 Bus 发送
        bus_msg = BusMessage(
            channel=self.channel,
            chat_id=std_msg.chat_id,
            user_id=std_msg.user_id,
            text=std_msg.text,
            project_id=55,
        )

        if self.bus:
            await self.bus.enqueue_request(bus_msg)
        else:
            logger.error("No bus configured, message dropped")

    def format_response(self, nl2sql_result: Any) -> str:
        """格式化响应消息为 Telegram 文本（TRACE_IN_REPLY=true 时末尾附 resolution trace）"""
        text = self._format_body(nl2sql_result)
        if trace_in_reply_enabled():
            trace = (nl2sql_result.get("explain") or {}).get("trace") or []
            if trace:
                text = f"{text}\n\n🔎 Trace\n" + "\n".join(trace)
        return text[:_TELEGRAM_MAX_CHARS]

    def _format_body(self, nl2sql_result: Any) -> str:
        status = nl2sql_result.get("status", "success")

        # 确认流：返回候选列表供用户选择
        if status == "needs_confirmation":
            return self._format_confirmation(nl2sql_result)

        if status == "early_exit":
            return nl2sql_result.get("message", "Unable to process this query.")

        # 正常结果：展示生成的 SQL 与解析意图
        intent = nl2sql_result.get("resolved_intent", {})
        sql = nl2sql_result.get("sql", "")

        lines = ["\U0001F4C4 ClickHouse SQL generated"]

        if intent.get("tables"):
            lines.append(f"Table: {', '.join(intent['tables'])}")
        if intent.get("metrics"):
            lines.append(f"Metrics: {', '.join(intent['metrics'])}")
        if intent.get("group_by"):
            lines.append(f"Group by: {', '.join(intent['group_by'])}")
        if intent.get("filters"):
            f_strs = [f"{f.get('column')} {f.get('op')} {f.get('value')}" for f in intent["filters"]]
            lines.append(f"Filters: {', '.join(f_strs)}")
        tr = intent.get("time_range") or {}
        if tr:
            lines.append(f"Time: {tr.get('type', 'last_n_days')} n={tr.get('n', '?')}")

        # AST 分析摘要（护栏效果可视化：扫描量估算 + 警告码）
        summary = self._format_ast_summary(nl2sql_result)
        if summary:
            lines.append(summary)

        lines.append("")
        lines.append(sql or "(empty SQL)")
        return "\n".join(lines)

    @staticmethod
    def _format_ast_summary(nl2sql_result: Any) -> str:
        """从 explain 中提取 AST 分析摘要行，无分析数据时返回空串"""
        try:
            gen = ((nl2sql_result.get("explain") or {}).get("resolver_explain") or {}).get("sql_generation") or {}
            ast = gen.get("ast_analysis") or {}
            cost = ast.get("cost") or {}
            if not cost:
                return ""
            scanned = cost.get("estimated_rows_scanned", 0)
            scan_str = f"{scanned / 1_000_000:.0f}M" if scanned >= 1_000_000 else f"{scanned / 1_000:.0f}K"
            parts = [f"📊 Est. scan ~{scan_str} rows"]
            if cost.get("join_count"):
                parts.append(f"join x{cost['join_count']}")
            warnings = [w.get("code", "") for w in ast.get("warnings", [])]
            parts.append("⚠️ " + ", ".join(warnings) if warnings else "✅ No warnings")
            if gen.get("repaired"):
                parts.append("🔧 Auto-repaired")
            return " · ".join(parts)
        except Exception:
            return ""

    async def send_response(self, recipient: str, response: Dict[str, Any]) -> None:
        """发送响应"""
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": recipient,
            "text": response.get("text", ""),
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

            if not data.get("ok"):
                logger.error(f"Failed to send message: {data}")
            else:
                logger.info(f"Message sent to {recipient}")

    def _format_confirmation(self, nl2sql_result: Any) -> str:
        """格式化确认请求消息"""
        message = nl2sql_result.get("message", "")
        candidates = nl2sql_result.get("candidates", {})

        if not candidates:
            return message

        lines = []
        for field_name, cands in candidates.items():
            field_display = {
                "tables": "table", "metrics": "metric", "join": "bridging table",
                "group_by_column": "group-by column", "detail_column": "column",
            }.get(field_name, field_name)
            lines.append(f"Please choose a {field_display}:")
            for i, c in enumerate(cands[:5], 1):
                lines.append(f"  {i}. {c['value']} (match {c['score']:.0f}%)")
        lines.append("Reply with a number or a name.")

        return "\n".join(lines)
