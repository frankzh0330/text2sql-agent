"""Text2SQL Agent API — FastAPI 端点定义

app.py 只负责：
1. FastAPI 实例 + 请求/响应模型
2. 全局服务实例化
3. HTTP 端点（委托给 QueryOrchestrator）
4. Telegram 通知（依赖环境变量）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from service.session_manager import SessionManager
from service.task_manager import TaskManager
from memory.memory_writer import MemoryWriter
from memory.storage.memory_file import TaskStorage
from memory.user_preference_store import UserPreferenceStore
from service.query_orchestrator import QueryOrchestrator

app = FastAPI(title="text2sql-agent: NL to ClickHouse SQL")
logger = logging.getLogger(__name__)

# ====================
# 全局服务实例
# ====================

_matcher_service: Optional["MatcherService"] = None
session_manager = SessionManager(data_path="data")
task_manager = TaskManager(storage=TaskStorage(data_path="data/tasks"))
memory_writer = MemoryWriter(data_path="data/memory")
user_preference_store = UserPreferenceStore(data_path="data/user_preferences")
orchestrator = QueryOrchestrator(session_manager, task_manager, memory_writer, user_preference_store)


def set_matcher_service(service: "MatcherService") -> None:
    global _matcher_service
    _matcher_service = service
    logger.info("MatcherService registered")


def get_matcher_service() -> "MatcherService":
    if _matcher_service is None:
        raise RuntimeError("MatcherService not initialized. Start WebSocket server first.")
    return _matcher_service


# ====================
# 请求/响应模型
# ====================

class NL2SQLRequest(BaseModel):
    text: str
    project_id: int = Field(default=55)
    session_id: str | None = None
    user_id: str | None = None
    chat_id: str | None = None


class NL2SQLResponse(BaseModel):
    extraction_json: Dict[str, Any]
    sql: str
    resolved_intent: Dict[str, Any]
    explain: Dict[str, Any]
    session_id: str | None = None
    status: str = "success"
    message: str | None = None
    task_id: str | None = None
    candidates: Dict[str, Any] | None = None


# 兼容旧端点名
NL2DSLRequest = NL2SQLRequest
NL2DSLResponse = NL2SQLResponse


# ====================
# Telegram 通知
# ====================

async def _send_telegram_notification(chat_id: str, message: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning("Failed to send Telegram notification: %s", e)


# ====================
# HTTP 端点
# ====================

# 浏览器访问任意页面时会自动请求 /favicon.ico；内联 SVG 免 404
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<text y="0.9em" font-size="90">📊</text></svg>'
)


@app.get("/", include_in_schema=False)
def root():
    """服务信息 / 健康检查（同时避免浏览器直接访问根路径时 404）"""
    return {"service": app.title, "status": "ok", "docs": "/docs", "debug": "/sessions"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response

    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@app.post("/nl2sql", response_model=NL2SQLResponse)
@app.post("/nl2dsl", response_model=NL2SQLResponse, include_in_schema=False)
async def nl2sql(req: NL2SQLRequest) -> NL2SQLResponse:
    """NL → ClickHouse SQL 接口"""
    service = get_matcher_service()
    result = await orchestrator.process(req, service, service.schema, notify_fn=_send_telegram_notification)
    return NL2SQLResponse(**result)


@app.get("/sessions")
def list_sessions():
    sessions = session_manager.list_sessions()
    return {"count": len(sessions), "sessions": [s.to_dict() for s in sessions]}


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    ctx = session_manager.get_session(session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")
    return ctx.to_dict()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not session_manager.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}
