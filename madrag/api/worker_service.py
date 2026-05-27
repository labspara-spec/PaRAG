"""
madrag-worker — standalone document-processing worker service.

Run one or more of these alongside the API servers in cloud deployments:

    madrag-worker --workers 4

Each worker process:
1. Connects to Redis and subscribes to the madrag:jobs stream.
2. Maintains a local LRU cache of madRAG instances (one per workspace).
3. Picks up jobs, runs the pipeline, and publishes SSE events via Redis Pub/Sub.
4. Idle instances are evicted from the cache after ``--worker-idle-timeout`` seconds.
5. Crashed workers leave unacked messages; XAUTOCLAIM re-delivers them to any
   surviving worker after ``CLAIM_IDLE_MS`` milliseconds.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("madrag.worker")


# ---------------------------------------------------------------------------
# Per-worker instance cache helpers
# ---------------------------------------------------------------------------


async def _evict_stale_instances(
    cache: dict[str, tuple[Any, float]],
    idle_timeout: float,
) -> None:
    """Finalize and remove instances idle longer than idle_timeout."""
    now = time.monotonic()
    to_evict = [ws for ws, (_, last) in cache.items() if (now - last) > idle_timeout]
    for ws in to_evict:
        rag, _ = cache.pop(ws)
        try:
            await rag.finalize_storages()
            logger.info("Worker: evicted idle instance workspace=%s", ws)
        except Exception as exc:
            logger.warning("Worker: error finalizing workspace=%s: %s", ws, exc)


async def _get_cached_instance(
    cache: dict[str, tuple[Any, float]],
    workspace: str,
    rag_factory,
    max_cached: int,
    idle_timeout: float,
) -> Any:
    """Return cached madRAG for workspace, or create a new one."""
    if workspace in cache:
        rag, _ = cache[workspace]
        cache[workspace] = (rag, time.monotonic())
        return rag

    # Evict stale instances before potentially creating a new one
    await _evict_stale_instances(cache, idle_timeout)

    # If still at capacity, evict the LRU entry
    if len(cache) >= max_cached:
        lru_ws = min(cache, key=lambda k: cache[k][1])
        lru_rag, _ = cache.pop(lru_ws)
        try:
            await lru_rag.finalize_storages()
            logger.info("Worker: LRU-evicted workspace=%s", lru_ws)
        except Exception as exc:
            logger.warning("Worker: error finalizing LRU workspace=%s: %s", lru_ws, exc)

    rag = await rag_factory(workspace)
    cache[workspace] = (rag, time.monotonic())
    logger.info("Worker: created instance workspace=%s", workspace)
    return rag


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------


async def _run_worker(worker_id: str, args: argparse.Namespace) -> None:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        raise RuntimeError(
            "redis package is required for worker mode. "
            "Install with: pip install redis"
        )

    from madrag.api.job_queue import DocumentJobQueue
    from madrag.api.sse_bus import RedisSSEBus, RedisPublishBus

    redis_client = aioredis.from_url(args.redis_uri, decode_responses=False)
    job_queue = DocumentJobQueue(redis_client)
    await job_queue.ensure_group()

    # Build a madRAG factory using the same config as the API server would.
    # We import the factory builder from madrag_server to avoid duplicating
    # configuration logic.
    from madrag.api.madrag_server import build_rag_factory

    # Wire a RedisPublishBus so pipeline.py emit_status_event calls (PARSING,
    # ANALYZING, PROCESSING, PROCESSED) are published to Redis and reach SSE
    # clients, not just the worker-local default LocalSSEBus.
    event_bus = RedisPublishBus(redis_client)
    rag_factory = build_rag_factory(args, event_bus=event_bus)

    instance_cache: dict[str, tuple[Any, float]] = {}
    max_cached: int = args.worker_instance_cache_size
    idle_timeout: float = args.worker_idle_timeout

    logger.info(
        "Worker %s started (redis=%s, max_cached=%d, idle_timeout=%.0fs)",
        worker_id,
        args.redis_uri,
        max_cached,
        idle_timeout,
    )

    try:
        while True:
            # First, try to reclaim any stale unacked jobs from crashed workers
            stale = await job_queue.reclaim_stale(worker_id)
            jobs = stale or await job_queue.pop(worker_id, count=1, block_ms=5_000)

            if not jobs:
                # Heartbeat: evict stale instances during idle periods
                await _evict_stale_instances(instance_cache, idle_timeout)
                continue

            for msg_id, job in jobs:
                workspace: str = job.get("workspace", "")
                doc_ids: list[str] = job.get("doc_ids", [])
                track_id: str = job.get("track_id", "")

                if not workspace:
                    logger.warning("Worker: job missing workspace, skipping msg_id=%s", msg_id)
                    await job_queue.ack(msg_id)
                    continue

                logger.info(
                    "Worker %s: processing workspace=%s doc_ids=%s track_id=%s",
                    worker_id, workspace, doc_ids, track_id,
                )

                rag = await _get_cached_instance(
                    instance_cache, workspace, rag_factory, max_cached, idle_timeout
                )

                # Notify SSE subscribers that the pipeline is starting
                await RedisSSEBus.publish(redis_client, workspace, {
                    "event": "pipeline_start",
                    "doc_ids": doc_ids,
                    "track_id": track_id,
                    "worker_id": worker_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                try:
                    await rag.apipeline_process_enqueue_documents()
                    logger.info(
                        "Worker %s: completed workspace=%s", worker_id, workspace
                    )
                except Exception as exc:
                    logger.error(
                        "Worker %s: pipeline error workspace=%s: %s",
                        worker_id, workspace, exc,
                    )
                    await RedisSSEBus.publish(redis_client, workspace, {
                        "event": "pipeline_error",
                        "doc_ids": doc_ids,
                        "track_id": track_id,
                        "error": str(exc),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                await job_queue.ack(msg_id)
                cache_entry = instance_cache.get(workspace)
                if cache_entry:
                    instance_cache[workspace] = (cache_entry[0], time.monotonic())

    finally:
        logger.info("Worker %s shutting down, finalizing %d instance(s)...", worker_id, len(instance_cache))
        for ws, (rag, _) in list(instance_cache.items()):
            try:
                await rag.finalize_storages()
            except Exception:
                pass
        try:
            await redis_client.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Multi-worker launcher
# ---------------------------------------------------------------------------


async def _run_all_workers(args: argparse.Namespace) -> None:
    hostname = socket.gethostname()
    workers = [
        asyncio.create_task(
            _run_worker(f"{hostname}-{i}-{uuid.uuid4().hex[:6]}", args),
            name=f"madrag-worker-{i}",
        )
        for i in range(args.workers)
    ]
    logger.info("Launched %d worker(s)", args.workers)
    await asyncio.gather(*workers)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env", override=False)

    from madrag.constants import (
        DEFAULT_WORKER_INSTANCE_CACHE_SIZE,
        DEFAULT_WORKER_IDLE_TIMEOUT,
        DEFAULT_WOKERS,
    )

    p = argparse.ArgumentParser(
        prog="madrag-worker",
        description="madRAG document-processing worker for cloud deployments",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("WORKERS", DEFAULT_WOKERS)),
        help="Number of concurrent worker coroutines in this process",
    )
    p.add_argument(
        "--redis-uri",
        default=os.getenv("REDIS_URI", "redis://localhost:6379"),
        help="Redis URI used for the job queue and SSE pub/sub",
    )
    p.add_argument(
        "--worker-instance-cache-size",
        type=int,
        default=int(os.getenv("RAG_WORKER_INSTANCE_CACHE_SIZE", DEFAULT_WORKER_INSTANCE_CACHE_SIZE)),
        help="Max madRAG instances this worker caches locally",
    )
    p.add_argument(
        "--worker-idle-timeout",
        type=float,
        default=float(os.getenv("RAG_WORKER_IDLE_TIMEOUT", DEFAULT_WORKER_IDLE_TIMEOUT)),
        help="Seconds a workspace instance may be idle before eviction",
    )
    # Forward the most important server args so the worker can build a
    # madRAG instance with the same configuration as the API server.
    # All remaining config is read from .env / environment variables.
    p.add_argument("--working-dir", default=os.getenv("WORKING_DIR", "./rag_storage"))
    p.add_argument("--input-dir", default=os.getenv("INPUT_DIR", "./inputs"))
    p.add_argument("--workspace", default=os.getenv("WORKSPACE", ""))
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    asyncio.run(_run_all_workers(args))


if __name__ == "__main__":
    main()
