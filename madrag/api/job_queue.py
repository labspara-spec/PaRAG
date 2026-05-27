"""
Redis Streams job queue for cloud-mode document processing.

Workers use XREADGROUP so unacknowledged messages are automatically
redelivered after ``claim_idle_ms`` milliseconds (XAUTOCLAIM).  This
guarantees at-least-once delivery even when worker pods crash mid-job.

Usage (producer — API server):
    queue = DocumentJobQueue(redis_client)
    await queue.push(workspace="alice", doc_ids=["doc1"], track_id="upload_...")

Usage (consumer — worker service):
    await queue.ensure_group()
    while True:
        jobs = await queue.pop(consumer_id="worker-0")
        for msg_id, job in jobs:
            process(job)
            await queue.ack(msg_id)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

STREAM_KEY = "madrag:jobs"
GROUP_NAME = "workers"
# Milliseconds an unacknowledged message sits idle before XAUTOCLAIM reclaims it.
CLAIM_IDLE_MS = 60_000  # 1 minute


class DocumentJobQueue:
    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    async def push(
        self,
        workspace: str,
        doc_ids: list[str],
        track_id: str,
    ) -> str:
        """Enqueue a processing job; returns the Redis Stream entry ID."""
        entry_id = await self._redis.xadd(
            STREAM_KEY,
            {
                "workspace": workspace,
                "doc_ids": json.dumps(doc_ids),
                "track_id": track_id,
            },
            maxlen=100_000,
            approximate=True,
        )
        logger.debug(
            "JobQueue.push workspace=%s doc_ids=%s track_id=%s → %s",
            workspace,
            doc_ids,
            track_id,
            entry_id,
        )
        return entry_id

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def ensure_group(self) -> None:
        """Create consumer group if it doesn't exist yet."""
        try:
            await self._redis.xgroup_create(
                STREAM_KEY, GROUP_NAME, id="$", mkstream=True
            )
            logger.info("JobQueue: created consumer group '%s' on stream '%s'", GROUP_NAME, STREAM_KEY)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                pass  # group already exists — normal on restart
            else:
                logger.warning("JobQueue.ensure_group unexpected error: %s", exc)

    async def pop(
        self,
        consumer_id: str,
        count: int = 1,
        block_ms: int = 5_000,
    ) -> list[tuple[str, dict]]:
        """
        Claim up to ``count`` new messages for ``consumer_id``.
        Blocks at most ``block_ms`` milliseconds before returning an empty list.

        Returns list of (stream_entry_id, job_dict) tuples.
        """
        try:
            results = await self._redis.xreadgroup(
                GROUP_NAME,
                consumer_id,
                {STREAM_KEY: ">"},
                count=count,
                block=block_ms,
            )
        except Exception as exc:
            logger.error("JobQueue.pop error: %s", exc)
            return []

        if not results:
            return []

        out = []
        for msg_id, raw_fields in results[0][1]:
            job: dict = {}
            for k, v in raw_fields.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                job[key] = val
            # Deserialize doc_ids JSON string
            if "doc_ids" in job:
                try:
                    job["doc_ids"] = json.loads(job["doc_ids"])
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append((msg_id, job))

        return out

    async def ack(self, msg_id: str) -> None:
        """Acknowledge a processed message so it's removed from the PEL."""
        await self._redis.xack(STREAM_KEY, GROUP_NAME, msg_id)

    async def reclaim_stale(
        self,
        consumer_id: str,
        count: int = 10,
    ) -> list[tuple[str, dict]]:
        """
        Use XAUTOCLAIM to reclaim messages that have been idle > CLAIM_IDLE_MS
        (i.e. the previous consumer crashed without acknowledging them).
        Returns same format as ``pop()``.
        """
        try:
            result = await self._redis.xautoclaim(
                STREAM_KEY,
                GROUP_NAME,
                consumer_id,
                min_idle_time=CLAIM_IDLE_MS,
                start_id="0-0",
                count=count,
            )
        except Exception as exc:
            logger.error("JobQueue.reclaim_stale error: %s", exc)
            return []

        if not result or len(result) < 2:
            return []

        out = []
        for msg_id, raw_fields in result[1]:
            job: dict = {}
            for k, v in raw_fields.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                job[key] = val
            if "doc_ids" in job:
                try:
                    job["doc_ids"] = json.loads(job["doc_ids"])
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append((msg_id, job))

        return out

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def pending_count(self) -> int:
        """Return approximate number of unprocessed messages in the stream."""
        try:
            info = await self._redis.xinfo_groups(STREAM_KEY)
            for g in info:
                name = g.get(b"name", g.get("name", b""))
                if isinstance(name, bytes):
                    name = name.decode()
                if name == GROUP_NAME:
                    return int(g.get(b"pending", g.get("pending", 0)))
        except Exception:
            pass
        return 0
