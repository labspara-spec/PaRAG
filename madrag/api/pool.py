"""
madRAG instance pool.

One madRAG instance is kept alive per workspace.  Instances are created on
first access, reused for subsequent requests to the same workspace, and evicted
(after calling ``finalize_storages()``) once they have been idle longer than
``idle_timeout`` seconds.

The pool is used in both deployment modes:
- Local / single-server: in-process LRU cache; replaces the singleton ``rag``.
- Cloud / multi-pod (RAG_CLOUD_MODE=true): API pods use the pool for query
  contexts; document processing is offloaded to separate worker processes via
  the job queue.

Thread / asyncio safety: all mutations are protected by a single asyncio.Lock.
The pool is not safe to call from multiple OS threads simultaneously.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class PooledInstance:
    rag: object  # madRAG — typed as object to avoid circular import
    workspace: str
    created_at: float
    last_used_at: float
    ref_count: int = 0

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used_at

    def touch(self) -> None:
        self.last_used_at = time.monotonic()


class madRAGPool:
    """
    Workspace-keyed pool of madRAG instances with TTL-based eviction.

    Parameters
    ----------
    factory:
        Async callable ``factory(workspace: str) -> madRAG`` that creates
        and initializes (``initialize_storages()``) a new instance.
    max_size:
        Maximum number of live instances.  When full, the least-recently-used
        idle instance is evicted before creating a new one.
    min_size:
        Minimum instances kept alive at all times (never evicted by the sweep).
    idle_timeout:
        Seconds an instance must be idle (ref_count == 0) before the cleanup
        sweep evicts it.
    cleanup_interval:
        How often (seconds) the background sweep runs.
    """

    def __init__(
        self,
        factory: Callable[[str], Awaitable[object]],
        max_size: int = 8,
        min_size: int = 1,
        idle_timeout: float = 300.0,
        cleanup_interval: float = 60.0,
    ) -> None:
        self._factory = factory
        self._max_size = max_size
        self._min_size = min_size
        self._idle_timeout = idle_timeout
        self._cleanup_interval = cleanup_interval

        self._instances: dict[str, PooledInstance] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self, workspace: str) -> AsyncGenerator[object, None]:
        """
        Context manager that returns the madRAG instance for *workspace*.

        Creates the instance if it doesn't exist.  Increments ref_count on
        entry, decrements on exit.  The instance is never evicted while
        ref_count > 0.
        """
        instance = await self._get_or_create(workspace)
        instance.ref_count += 1
        instance.touch()
        try:
            yield instance.rag
        finally:
            instance.ref_count -= 1
            instance.touch()

    async def initialize(self, default_workspace: str) -> None:
        """
        Pre-warm the pool with the default workspace and start the cleanup task.
        Called once at server startup.
        """
        await self._get_or_create(default_workspace)
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="rag-pool-cleanup"
        )
        logger.info(
            "madRAGPool started: max_size=%d min_size=%d idle_timeout=%.0fs",
            self._max_size, self._min_size, self._idle_timeout,
        )

    async def shutdown(self) -> None:
        """Cancel the cleanup task and finalize all instances."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            workspaces = list(self._instances.keys())

        for ws in workspaces:
            await self._finalize_instance(ws)

        logger.info("madRAGPool shut down; %d instances finalized.", len(workspaces))

    @property
    def status(self) -> list[dict]:
        """Snapshot of pool state for the /pool/status endpoint."""
        now = time.monotonic()
        return [
            {
                "workspace": inst.workspace,
                "ref_count": inst.ref_count,
                "age_seconds": round(now - inst.created_at, 1),
                "idle_seconds": round(now - inst.last_used_at, 1),
            }
            for inst in self._instances.values()
        ]

    def instance_count(self) -> int:
        return len(self._instances)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_or_create(self, workspace: str) -> PooledInstance:
        async with self._lock:
            if workspace in self._instances:
                return self._instances[workspace]

            if len(self._instances) >= self._max_size:
                await self._evict_lru_locked()

            instance = await self._create_instance_locked(workspace)
            self._instances[workspace] = instance
            return instance

    async def _create_instance_locked(self, workspace: str) -> PooledInstance:
        logger.info("madRAGPool: creating instance for workspace=%s", workspace)
        rag = await self._factory(workspace)
        now = time.monotonic()
        return PooledInstance(
            rag=rag, workspace=workspace, created_at=now, last_used_at=now
        )

    async def _evict_lru_locked(self) -> None:
        """Evict the least-recently-used idle instance. Caller holds self._lock."""
        idle = [
            (ws, inst)
            for ws, inst in self._instances.items()
            if inst.ref_count == 0
        ]
        if not idle:
            raise RuntimeError(
                "madRAGPool exhausted: all instances are in use "
                f"(max_size={self._max_size}). "
                "Increase RAG_POOL_MAX_SIZE or reduce concurrent workspaces."
            )
        ws, _ = min(idle, key=lambda x: x[1].last_used_at)
        logger.info("madRAGPool: LRU-evicting workspace=%s to make room", ws)
        inst = self._instances.pop(ws)
        # finalize outside lock to avoid holding it during I/O
        asyncio.create_task(self._finalize_rag(inst.rag, ws))

    async def _finalize_instance(self, workspace: str) -> None:
        async with self._lock:
            inst = self._instances.pop(workspace, None)
        if inst is not None:
            await self._finalize_rag(inst.rag, workspace)

    @staticmethod
    async def _finalize_rag(rag: object, workspace: str) -> None:
        try:
            await rag.finalize_storages()  # type: ignore[attr-defined]
            logger.info("madRAGPool: finalized workspace=%s", workspace)
        except Exception as exc:
            logger.warning(
                "madRAGPool: error finalizing workspace=%s: %s", workspace, exc
            )

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_stale()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("madRAGPool cleanup error: %s", exc)

    async def _cleanup_stale(self) -> None:
        now = time.monotonic()
        async with self._lock:
            to_evict = [
                ws
                for ws, inst in self._instances.items()
                if (
                    inst.ref_count == 0
                    and (now - inst.last_used_at) > self._idle_timeout
                    and len(self._instances) > self._min_size
                )
            ]

        for ws in to_evict:
            async with self._lock:
                # Re-check under lock — instance may have been re-acquired
                inst = self._instances.get(ws)
                if inst is None or inst.ref_count > 0:
                    continue
                if len(self._instances) <= self._min_size:
                    break
                self._instances.pop(ws)

            logger.info(
                "madRAGPool: evicted idle workspace=%s (idle=%.0fs)",
                ws,
                now - inst.last_used_at,
            )
            asyncio.create_task(self._finalize_rag(inst.rag, ws))
