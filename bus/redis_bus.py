from __future__ import annotations

import logging
import os
from typing import Optional

import redis.asyncio as aioredis

from bus.base import MessageBus
from bus.message_schema import BusMessage, BusResult

logger = logging.getLogger(__name__)

REQUEST_QUEUE = "text2sql_agent:request_queue"
RESULT_QUEUE = "text2sql_agent:result_queue"


class RedisMessageBus(MessageBus):
    """
    Redis LIST 实现的消息总线。

    - 请求队列：text2sql_agent:request_queue（所有 Gateway 共用）
    - 结果队列：text2sql_agent:result_queue（所有 Worker 共用）

    使用 BLPOP 实现阻塞式消费，支持多 Worker 并行。
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis: Optional[aioredis.Redis] = None

    async def _get_redis(self) -> aioredis.Redis:
        """延迟创建 Redis 连接"""
        if self._redis is None:
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            logger.info(f"Redis connected: {self._redis_url}")
        return self._redis

    async def enqueue_request(self, msg: BusMessage) -> None:
        """将消息放入请求队列"""
        r = await self._get_redis()
        await r.rpush(REQUEST_QUEUE, msg.model_dump_json())
        logger.info(f"Enqueued request: msg_id={msg.msg_id} channel={msg.channel}")

    async def dequeue_request(self, timeout: float = 30) -> Optional[BusMessage]:
        """从请求队列阻塞取出消息"""
        r = await self._get_redis()
        result = await r.blpop(REQUEST_QUEUE, timeout=int(timeout))
        if result is None:
            return None
        _, data = result
        return BusMessage.model_validate_json(data)

    async def enqueue_result(self, result: BusResult) -> None:
        """将结果放入结果队列"""
        r = await self._get_redis()
        await r.rpush(RESULT_QUEUE, result.model_dump_json())
        logger.info(f"Enqueued result: msg_id={result.msg.msg_id}")

    async def dequeue_result(self, timeout: float = 30) -> Optional[BusResult]:
        """从结果队列阻塞取出结果"""
        r = await self._get_redis()
        result = await r.blpop(RESULT_QUEUE, timeout=int(timeout))
        if result is None:
            return None
        _, data = result
        return BusResult.model_validate_json(data)

    async def close(self) -> None:
        """关闭 Redis 连接"""
        if self._redis:
            await self._redis.close()
            logger.info("Redis connection closed")
