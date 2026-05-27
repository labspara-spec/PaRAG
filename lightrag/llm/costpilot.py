"""CostPilot cost-tracking and service-health integration for PaRAG LLM clients.

Enabled by setting COSTPILOT_API_KEY in the environment.
Install: pip install lightrag-hku[observability]

Config priority (matches CostPilot SDK):
  constructor args → env vars → .costpilot.yaml → auto-generated from PaRAG env → defaults

Service health tracking is auto-detected from PaRAG's existing REDIS_URI / QDRANT_URL
env vars and written to .costpilot/_generated.yaml when no explicit config file exists.
"""
import os
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from lightrag.utils import logger

COSTPILOT_ENABLED = False
_cp_client: Any = None

# Config-path resolution order:
#   1. COSTPILOT_CONFIG_PATH env var (explicit override)
#   2. .costpilot.yaml in cwd (user-managed via `costpilot init`)
#   3. .costpilot/_generated.yaml (auto-generated from PaRAG env vars)
_USER_YAML = ".costpilot.yaml"
_GENERATED_YAML = ".costpilot/_generated.yaml"


# ---------------------------------------------------------------------------
# Service config helpers
# ---------------------------------------------------------------------------

def _parse_redis_service() -> dict[str, Any] | None:
    uri = os.environ.get("REDIS_URI", "").strip()
    if not uri:
        return None
    try:
        p = urlparse(uri)
        host = p.hostname or "localhost"
        port = p.port or 6379
        mode = "docker" if host in ("localhost", "127.0.0.1", "::1") else "managed"
        return {"enabled": True, "mode": mode, "host": host, "port": port}
    except Exception as exc:
        logger.debug("CostPilot: could not parse REDIS_URI: %s", exc)
        return None


def _parse_qdrant_service() -> dict[str, Any] | None:
    url = os.environ.get("QDRANT_URL", "").strip()
    if not url:
        return None
    try:
        p = urlparse(url)
        host = p.hostname or "localhost"
        port = p.port or 6333
        mode = "docker" if host in ("localhost", "127.0.0.1", "::1") else "managed"
        return {"enabled": True, "mode": mode, "host": host, "port": port}
    except Exception as exc:
        logger.debug("CostPilot: could not parse QDRANT_URL: %s", exc)
        return None


def _generate_config_yaml(api_key: str, project: str, environment: str) -> str:
    """Build .costpilot.yaml content from PaRAG env vars."""
    import yaml  # PyYAML already in PaRAG deps

    config: dict[str, Any] = {
        "project": project,
        "environment": environment,
        "api_key": api_key,
        "privacy": {
            "hash_user_ids": True,
            "capture_prompts": False,
        },
        "llm": {
            "providers": ["anthropic", "openai", "azure-openai", "gemini"],
        },
        "services": {},
        "cloud": {
            "region": os.environ.get("COSTPILOT_REGION", "us-east-1"),
        },
    }

    redis_cfg = _parse_redis_service()
    if redis_cfg:
        config["services"]["redis"] = redis_cfg
        logger.debug(
            "CostPilot: Redis service health tracking enabled (%s:%s, mode=%s)",
            redis_cfg["host"],
            redis_cfg["port"],
            redis_cfg["mode"],
        )

    qdrant_cfg = _parse_qdrant_service()
    if qdrant_cfg:
        config["services"]["qdrant"] = qdrant_cfg
        logger.debug(
            "CostPilot: Qdrant service health tracking enabled (%s:%s, mode=%s)",
            qdrant_cfg["host"],
            qdrant_cfg["port"],
            qdrant_cfg["mode"],
        )

    return yaml.safe_dump(config, default_flow_style=False, sort_keys=False)


def _resolve_config_path(api_key: str, project: str, environment: str) -> str | None:
    """Return the path to use for config_path, generating one if needed."""
    # 1. Explicit override
    explicit = os.environ.get("COSTPILOT_CONFIG_PATH", "").strip()
    if explicit:
        return explicit

    # 2. User-managed file
    if Path(_USER_YAML).exists():
        return _USER_YAML

    # 3. Auto-generate only if there are services to track
    redis_cfg = _parse_redis_service()
    qdrant_cfg = _parse_qdrant_service()
    if not redis_cfg and not qdrant_cfg:
        return None  # No services — skip YAML, use constructor args only

    generated = Path(_GENERATED_YAML)
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(
        _generate_config_yaml(api_key, project, environment), encoding="utf-8"
    )
    logger.debug("CostPilot: wrote generated config to %s", _GENERATED_YAML)
    return str(generated)


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------

def _init_costpilot() -> Any:
    global COSTPILOT_ENABLED, _cp_client

    api_key = os.environ.get("COSTPILOT_API_KEY", "").strip()
    license_key = os.environ.get("COSTPILOT_LICENSE_KEY", "").strip()

    if not api_key and not license_key:
        return None

    try:
        from costpilot import CostPilotClient  # type: ignore[import-untyped]

        project = os.environ.get("COSTPILOT_PROJECT", "parag")
        environment = os.environ.get("COSTPILOT_ENVIRONMENT", "local")
        user_id = os.environ.get("COSTPILOT_USER_ID") or None
        session_id = os.environ.get("COSTPILOT_SESSION_ID") or None
        feature = os.environ.get("COSTPILOT_FEATURE") or None

        auth_key = api_key or license_key
        config_path = _resolve_config_path(auth_key, project, environment)

        kwargs: dict[str, Any] = {
            "project": project,
            "environment": environment,
            "user_id": user_id,
            "session_id": session_id,
            "feature": feature,
        }
        if api_key:
            kwargs["api_key"] = api_key
        else:
            kwargs["license_key"] = license_key
        if config_path:
            kwargs["config_path"] = config_path

        _cp_client = CostPilotClient(**kwargs)
        COSTPILOT_ENABLED = True
        logger.info(
            "CostPilot cost tracking enabled (project=%s, env=%s, config=%s)",
            project,
            environment,
            config_path or "env-only",
        )
    except ImportError:
        logger.debug("costpilot package not installed; cost tracking disabled")
    except Exception as exc:
        logger.warning("CostPilot initialization failed: %s", exc)

    return _cp_client


def get_costpilot() -> Any:
    """Return singleton CostPilotClient, or None if not configured."""
    if _cp_client is not None or COSTPILOT_ENABLED:
        return _cp_client
    return _init_costpilot()


def wrap_with_costpilot(client: Any) -> Any:
    """Wrap an LLM provider client with CostPilot tracking.

    Returns the original client unchanged if CostPilot is not configured
    or if the wrap call fails.
    """
    cp = get_costpilot()
    if cp is None:
        return client
    try:
        wrapped = cp.wrap(client)
        logger.debug("CostPilot: wrapped %s", type(client).__name__)
        return wrapped
    except Exception as exc:
        logger.warning("CostPilot wrap failed for %s: %s", type(client).__name__, exc)
        return client
