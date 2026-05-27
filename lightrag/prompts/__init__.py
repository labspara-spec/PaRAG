"""PaRAG prompt library — XML-backed prompt store.

Usage::

    from lightrag.prompts import load_prompts
    PROMPTS = load_prompts()
"""

from .loader import load_prompts

__all__ = ["load_prompts"]
