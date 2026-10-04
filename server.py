from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

# 全局日志配置（默认 INFO；调试时 LOG_LEVEL=DEBUG python server.py）
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

# 第三方 HTTP 库的 DEBUG 日志是纯噪音（每次 TLS/请求头都打一行），统一压到 WARNING
for _noisy in ("httpx", "httpcore", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from app import app, set_matcher_service
from bus import create_bus
from bus.direct_call_bus import DirectCallBus
from dispatcher.response_dispatcher import ResponseDispatcher
from gateway.telegram_gateway import TelegramGateway
from matcher.matcher_service import MatcherService
from worker.agent_worker import AgentWorker

logger = logging.getLogger(__name__)


# ====================
# 组件初始化
# ====================

# 全局消息总线
bus = create_bus()

# Agent Worker
worker = AgentWorker()

# 响应分派器
dispatcher = ResponseDispatcher()

# Gateway 管理器
_gateways: dict[str, TelegramGateway] = {}


# ====================
# 服务生命周期
# ====================

@asynccontextmanager
async def websocket_lifespan(app: FastAPI):
    """
    服务生命周期管理

    启动时:
    1. 初始化 MatcherService（构建索引，一次性加载 catalog/ 下 tables / metrics / aliases 三个元数据源）
    2. 创建 MessageBus + AgentWorker + ResponseDispatcher
    3. 启动 Telegram Gateway
    4. 启动 Session 定时清理（每 5 分钟清理 60 分钟未活跃会话）

    注：Catalog 定时同步调度器为生产方向、当前未实现（README「Real Production Environment」第 1 条）；
    demo 态 schema 仅在启动时加载一次，更新 YAML 需重启进程。
    """
    logger.info("=== Starting Server ===")

    # 1. 初始化 MatcherService（构建索引）
    matcher_service = MatcherService(catalog_path="catalog")
    set_matcher_service(matcher_service)
    logger.info("MatcherService initialized and registered")

    # 2. 组装 Bus + Worker + Dispatcher
    if isinstance(bus, DirectCallBus):
        bus.bind_worker(worker)
        bus.bind_dispatcher(dispatcher)
        logger.info("DirectCallBus: worker and dispatcher bound")
    else:
        # Redis 模式：启动 worker 和 dispatcher 循环
        asyncio.create_task(worker.run_loop(bus))
        asyncio.create_task(dispatcher.run_loop(bus))
        logger.info("Redis bus: worker and dispatcher loops started")

    # 3. 启动 Telegram Gateway
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        telegram_gw = TelegramGateway(bot_token=telegram_token, bus=bus)
        _gateways["telegram"] = telegram_gw

        # 注册到 Dispatcher
        dispatcher.register("telegram", telegram_gw)

        # 在后台运行 Gateway
        async def run_gateway():
            await telegram_gw.start()

        asyncio.create_task(run_gateway())
        logger.info("TelegramGateway started in background")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set, skipping TelegramGateway")

    # 4. 启动 Session 定时清理
    from app import session_manager

    async def _session_cleanup_loop():
        while True:
            await asyncio.sleep(300)  # 每 5 分钟
            try:
                session_manager.cleanup_inactive(max_age_minutes=60)
            except Exception as e:
                logger.warning("Session cleanup failed: %s", e)

    asyncio.create_task(_session_cleanup_loop())
    logger.info("Session cleanup loop started (interval=5min, max_age=60min)")

    yield

    # 清理
    logger.info("=== Stopping Server ===")
    for gw in _gateways.values():
        await gw.stop()


# 创建带生命周期的 FastAPI 应用
app_with_ws = FastAPI(
    title="text2sql-agent: WebSocket + HTTP",
    lifespan=websocket_lifespan,
)

# 直接挂载原有路由（不使用 /api 前缀）
for route in app.routes:
    app_with_ws.routes.append(route)


# ====================
# 启动入口
# ====================

def main():
    """启动服务器"""
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    logger.info(f"Starting server on {host}:{port}")

    uvicorn.run(
        app_with_ws,
        host=host,
        port=port,
        log_level=LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
