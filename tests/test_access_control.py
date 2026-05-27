"""Unit tests for the access control system (access_control.py)."""

import os
import pytest

from madrag.access_control import (
    build_permission_filter,
    apply_permission_filter,
)
from madrag.base import PermissionFilter


# ---------------------------------------------------------------------------
# build_permission_filter
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_build_returns_none_when_ac_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ACCESS_CONTROL", "false")
    pf = build_permission_filter("alice", ["admin"])
    assert pf is None


@pytest.mark.offline
def test_build_returns_filter_when_ac_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ACCESS_CONTROL", "true")
    pf = build_permission_filter("alice", ["admin"], force_enabled=True)
    assert pf is not None
    assert pf.current_user == "alice"
    assert "admin" in pf.current_roles


@pytest.mark.offline
def test_build_with_force_enabled_ignores_env(monkeypatch):
    monkeypatch.setenv("ENABLE_ACCESS_CONTROL", "false")
    pf = build_permission_filter("bob", [], force_enabled=True)
    assert pf is not None
    assert pf.current_user == "bob"


# ---------------------------------------------------------------------------
# apply_permission_filter — pf is None → zero overhead, pass-through
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_apply_none_filter_passthrough():
    chunks = [{"content": "x", "visibility": "restricted"}]
    result = apply_permission_filter(chunks, None)
    assert result is chunks  # same object, no copy


# ---------------------------------------------------------------------------
# apply_permission_filter — public visibility
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_public_chunk_visible_to_anyone():
    pf = PermissionFilter(current_user="unknown", current_roles=[])
    chunks = [{"content": "pub", "visibility": "public"}]
    result = apply_permission_filter(chunks, pf)
    assert len(result) == 1


@pytest.mark.offline
def test_none_visibility_treated_as_unrestricted():
    pf = PermissionFilter(current_user="someone", current_roles=[])
    chunks = [{"content": "x"}]  # no visibility key → unrestricted
    result = apply_permission_filter(chunks, pf)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# apply_permission_filter — owner always wins
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_owner_can_see_restricted_chunk():
    pf = PermissionFilter(current_user="alice", current_roles=[])
    chunk = {"content": "secret", "visibility": "restricted", "owner": "alice"}
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 1


@pytest.mark.offline
def test_non_owner_blocked_by_restricted_no_acl():
    pf = PermissionFilter(current_user="bob", current_roles=[])
    chunk = {
        "content": "secret",
        "visibility": "restricted",
        "owner": "alice",
        "allowed_users": None,
        "allowed_roles": None,
    }
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# apply_permission_filter — allowed_users
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_allowed_user_can_see_chunk():
    pf = PermissionFilter(current_user="alice", current_roles=[])
    chunk = {
        "content": "x",
        "visibility": "internal",
        "allowed_users": ["alice", "charlie"],
    }
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 1


@pytest.mark.offline
def test_non_allowed_user_blocked():
    pf = PermissionFilter(current_user="dave", current_roles=[])
    chunk = {
        "content": "x",
        "visibility": "internal",
        "allowed_users": ["alice"],
        "allowed_roles": None,
    }
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# apply_permission_filter — allowed_roles
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_role_match_grants_access():
    pf = PermissionFilter(current_user="bob", current_roles=["researcher"])
    chunk = {
        "content": "x",
        "visibility": "confidential",
        "allowed_roles": ["researcher", "admin"],
        "allowed_users": None,
    }
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 1


@pytest.mark.offline
def test_no_role_match_blocked():
    pf = PermissionFilter(current_user="carol", current_roles=["viewer"])
    chunk = {
        "content": "x",
        "visibility": "confidential",
        "allowed_roles": ["admin"],
        "allowed_users": None,
    }
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# apply_permission_filter — chunk-level override
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_chunk_allowed_users_override():
    pf = PermissionFilter(current_user="bob", current_roles=[])
    chunk = {
        "content": "x",
        "visibility": "restricted",
        "allowed_users": ["alice"],
        "chunk_allowed_users": ["bob"],
    }
    result = apply_permission_filter([chunk], pf)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# apply_permission_filter — mixed batch
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_mixed_batch_filters_correctly():
    pf = PermissionFilter(current_user="alice", current_roles=["reader"])
    chunks = [
        {"content": "pub", "visibility": "public"},
        {"content": "alice-only", "visibility": "restricted", "allowed_users": ["alice"]},
        {"content": "bob-only", "visibility": "restricted", "allowed_users": ["bob"]},
        {"content": "open"},  # no visibility = unrestricted
    ]
    result = apply_permission_filter(chunks, pf)
    assert len(result) == 3
    contents = {c["content"] for c in result}
    assert "bob-only" not in contents
