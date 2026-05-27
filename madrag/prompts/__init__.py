"""madRAG prompt library — XML-backed prompt store.

Usage::

    from madrag.prompts import load_prompts
    PROMPTS = load_prompts()
"""

from .loader import load_prompts

__all__ = ["load_prompts"]
