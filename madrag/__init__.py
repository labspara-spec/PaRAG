from typing import TYPE_CHECKING, Any

from ._version import __version__ as __version__

__all__ = [
    "madRAG",
    "QueryParam",
    "RoleLLMConfig",
    "RoleSpec",
    "ROLES",
    "__version__",
]

if TYPE_CHECKING:
    from .madrag import (
        madRAG as madRAG,
        QueryParam as QueryParam,
        ROLES as ROLES,
        RoleLLMConfig as RoleLLMConfig,
        RoleSpec as RoleSpec,
    )


_LAZY_EXPORTS = {"madRAG", "QueryParam", "RoleLLMConfig", "RoleSpec", "ROLES"}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        from .madrag import madRAG, QueryParam, RoleLLMConfig, RoleSpec, ROLES

        values = {
            "madRAG": madRAG,
            "QueryParam": QueryParam,
            "RoleLLMConfig": RoleLLMConfig,
            "RoleSpec": RoleSpec,
            "ROLES": ROLES,
        }
        value = values[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__author__ = "Zirui Guo"
__url__ = "https://github.com/HKUDS/madRAG"
