"""
SSE event bus — two implementations sharing one protocol:

- LocalSSEBus   : in-process asyncio.Queue fan-out; single-server deployments.
- RedisSSEBus   : Redis Pub/Sub fan-out; cloud / multi-pod deployments.

Pipeline workers call the synchronous ``publish()`` method; SSE endpoints call
the async ``subscribe()`` / ``unsubscribe()`` methods (LocalSSEBus) or iterate
the async generator returned by ``subscribe()`` (RedisSSEBus).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import AsyncGenerator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class SSEBusProtocol(Protocol):
    """Minimal interface both bus implementations satisfy."""

    def publish(self, workspace: str, event: dict) -> None: ...


# ---------------------------------------------------------------------------
# Local (single-process) implementation
# ---------------------------------------------------------------------------


class LocalSSEBus:
    """In-process asyncio.Queue fan-out.

    ``publish`` is synchronous so pipeline workers (which may not hold the
    event loop) can call it without await.  Slow consumers get dropped events
    (QueueFull is silently swallowed) to prevent back-pressure stalls.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def publish(self, workspace: str, event: dict) -> None:
        for q in list(self._subscribers.get(workspace, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, workspace: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers[workspace].append(q)
        return q

    async def unsubscribe(self, workspace: str, q: asyncio.Queue) -> None:
        async with self._lock:
            try:
                self._subscribers[workspace].remove(q)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Redis Pub/Sub implementation
# ---------------------------------------------------------------------------


class RedisSSEBus:
    """Redis Pub/Sub fan-out for multi-pod cloud deployments.

    Each ``subscribe()`` call opens a *dedicated* Redis connection (required
    by redis-py's pub/sub API — you cannot share a connection between pub and
    sub).  The connection is closed automatically when the async generator is
    exhausted or the caller breaks out of the loop.

    ``publish`` is a *static async* method so it can be called from worker
    processes that already hold a Redis client without instantiating the full
    bus object.
    """

    CHANNEL_PREFIX = "events"

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    @classmethod
    def channel(cls, workspace: str) -> str:
        return f"{cls.CHANNEL_PREFIX}:{workspace}"

    @staticmethod
    async def publish(redis_client, workspace: str, event: dict) -> None:
        try:
            await redis_client.publish(
                RedisSSEBus.channel(workspace),
                json.dumps(event, default=str),
            )
        except Exception as exc:
            logger.warning("RedisSSEBus.publish failed for workspace=%s: %s", workspace, exc)

    async def subscribe(self, workspace: str) -> AsyncGenerator[dict, None]:
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise RuntimeError(
                "redis package is required for cloud mode. "
                "Install with: pip install redis"
            )

        client = aioredis.from_url(self._redis_url, decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(self.channel(workspace))
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    try:
                        yield json.loads(message["data"])
                    except (json.JSONDecodeError, KeyError):
                        pass
        finally:
            try:
                await pubsub.unsubscribe(self.channel(workspace))
                await pubsub.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Worker-side publish-only bus
# ---------------------------------------------------------------------------


class RedisPublishBus:
    """Sync-compatible event bus that publishes to Redis Pub/Sub.

    Designed to be assigned as ``LightRAG.event_bus`` on worker processes so
    that ``emit_status_event`` calls inside ``pipeline.py`` reach Redis — and
    therefore all SSE subscribers — without modifying the pipeline code.

    The ``publish`` call is fire-and-forget: it schedules an asyncio task on
    the running event loop and returns immediately.  Safe to call from any
    coroutine context; the task is awaited by the loop in the background.
    """

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    def publish(self, workspace: str, event: dict) -> None:
        try:
            asyncio.get_event_loop().create_task(
                RedisSSEBus.publish(self._redis, workspace, event)
            )
        except RuntimeError:
            pass
