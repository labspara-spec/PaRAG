"""Access control helpers for PaRAG chunk retrieval.

Access control is **disabled by default**.  Set ``ENABLE_ACCESS_CONTROL=true``
(env var) or pass ``enable_access_control=True`` to the ``LightRAG``
constructor to activate it.

When disabled every public function returns early with zero overhead — existing
deployments are completely unaffected.

Architecture (v1 — universal Python post-filter)
-------------------------------------------------
All VDB backends return their normal result lists.  After the query returns,
:func:`apply_permission_filter` trims the list to only chunks the caller is
permitted to see.  This single implementation works across every backend
(NanoVectorDB, FAISS, Postgres, Qdrant, Milvus, OpenSearch) without
per-backend filter DSL knowledge.

Native VDB filter push-down (Postgres WHERE, Qdrant Filter, Milvus expr,
OpenSearch bool query) is a v2 optimisation for high-scale deployments.

Permission model (three orthogonal layers)
------------------------------------------
1. **Visibility label** — ``visibility`` field on every chunk.
   ``None`` / absent → unrestricted.  ``"public"`` → world-readable.
   ``"internal"`` / ``"restricted"`` / ``"confidential"`` require a matching
   user or role.

2. **Document-level ACL** — ``allowed_users`` / ``allowed_roles`` lists
   inherited by all chunks in a document.  ``None`` = no restriction.

3. **Chunk-level ACL override** — ``chunk_allowed_users`` /
   ``chunk_allowed_roles`` on individual chunks.  When set they are *merged*
   with the document-level lists (union, not replace).

Access rule (evaluated per chunk):

    ALLOWED if ANY of:
      • caller is owner (``current_user == chunk["owner"]``)
      • ``visibility`` is None / absent / "public"
      • both ``allowed_users`` and ``allowed_roles`` are None (unrestricted)
      • ``current_user`` ∈ effective_users
      • any role in ``current_roles`` ∈ effective_roles

where effective_users = (allowed_users ∪ chunk_allowed_users) and
      effective_roles = (allowed_roles ∪ chunk_allowed_roles).
"""

from __future__ import annotations

from typing import Any

from lightrag.base import PermissionFilter
from lightrag.constants import ENABLE_ACCESS_CONTROL


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------


def build_permission_filter(
    current_user: str | None,
    current_roles: list[str] | None = None,
    *,
    force_enabled: bool = False,
) -> PermissionFilter | None:
    """Return a :class:`PermissionFilter` when AC is active, else ``None``.

    ``None`` is the zero-cost bypass signal: callers that receive ``None``
    skip all filtering entirely.

    Args:
        current_user: The authenticated user's ID (or ``None`` for anonymous).
        current_roles: List of role names the caller holds.
        force_enabled: Override the global ``ENABLE_ACCESS_CONTROL`` flag.
                       Useful in tests and admin code paths.
    """
    if not ENABLE_ACCESS_CONTROL and not force_enabled:
        return None
    return PermissionFilter(
        current_user=current_user or None,
        current_roles=list(current_roles or []),
    )


# ---------------------------------------------------------------------------
# Access rule
# ---------------------------------------------------------------------------


def _is_chunk_allowed(chunk: dict[str, Any], pf: PermissionFilter) -> bool:
    """Return True when *pf* has read access to *chunk*."""
    visibility = chunk.get("visibility")
    owner = chunk.get("owner")
    allowed_users: list[str] | None = chunk.get("allowed_users")
    allowed_roles: list[str] | None = chunk.get("allowed_roles")
    chunk_allowed_users: list[str] | None = chunk.get("chunk_allowed_users")
    chunk_allowed_roles: list[str] | None = chunk.get("chunk_allowed_roles")

    # Owner always wins
    if pf.current_user and pf.current_user == owner:
        return True

    # No visibility restriction or explicitly public
    if not visibility or visibility == "public":
        return True

    # No ACL set → document is unrestricted
    if allowed_users is None and allowed_roles is None:
        return True

    # Build effective user + role sets (doc-level ∪ chunk-level)
    effective_users: set[str] = set(allowed_users or [])
    if chunk_allowed_users:
        effective_users.update(chunk_allowed_users)

    effective_roles: set[str] = set(allowed_roles or [])
    if chunk_allowed_roles:
        effective_roles.update(chunk_allowed_roles)

    if pf.current_user and pf.current_user in effective_users:
        return True

    if pf.current_roles and effective_roles.intersection(pf.current_roles):
        return True

    return False


# ---------------------------------------------------------------------------
# Post-query filter
# ---------------------------------------------------------------------------


def apply_permission_filter(
    results: list[dict[str, Any]],
    pf: PermissionFilter | None,
) -> list[dict[str, Any]]:
    """Filter *results* to only chunks *pf* is allowed to see.

    When *pf* is ``None`` (access control disabled) the original list is
    returned unchanged — zero allocation, zero iteration.

    Args:
        results: Raw VDB query result list (dicts with chunk metadata).
        pf: Permission context built by :func:`build_permission_filter`.
            ``None`` means AC is disabled → no-op.

    Returns:
        Filtered list; order preserved.
    """
    if pf is None:
        return results
    return [chunk for chunk in results if _is_chunk_allowed(chunk, pf)]
