"""Multimodal analysis prompts for madRAG.

These templates are consumed by ``madRAG.analyze_multimodal`` to produce
modality-specific analysis JSON written into each sidecar item's
``llm_analyze_result``.

Each template accepts the same variable set so the caller can format them
uniformly:

- ``language``  : target language for ``name`` / ``description`` outputs.
- ``content``   : modality body (table JSON/HTML, equation LaTeX, etc.).
                  Images pass an empty string and rely on ``image_inputs``.
- ``captions``  : caption text or ``"n/a"``.
- ``footnotes`` : joined footnotes string or ``"n/a"``.
- ``leading``   : surrounding leading context or ``"n/a"``.
- ``trailing``  : surrounding trailing context or ``"n/a"``.
- ``item_id``   : sidecar item identifier (for diagnostics, not required by
                  every template).
- ``file_path`` : source document path (diagnostics only).

The output schema differs by modality:

- Image    : ``{"name": str, "type": str, "description": str}``
- Table    : ``{"name": str, "description": str}``
- Equation : ``{"name": str, "equation": str, "description": str}``

Image ``type`` is restricted to :data:`IMAGE_TYPE_ENUM`; values outside the
enum are folded into :data:`IMAGE_TYPE_FALLBACK` by the caller.
"""

from __future__ import annotations

from madrag.prompts import load_prompts as _load_prompts

IMAGE_TYPE_ENUM: tuple[str, ...] = (
    "Photo",
    "Illustration",
    "Screenshot",
    "Icon",
    "Chart",
    "Table",
    "Infographic",
    "Flowchart",
    "Chat Log",
    "Wireframe",
    "Texture",
    "Other",
)

IMAGE_TYPE_FALLBACK = "Other"

_all_prompts = _load_prompts()
MULTIMODAL_PROMPTS: dict[str, str] = {
    k: _all_prompts[k]
    for k in ("image_analysis", "table_analysis", "equation_analysis")
}

__all__ = [
    "IMAGE_TYPE_ENUM",
    "IMAGE_TYPE_FALLBACK",
    "MULTIMODAL_PROMPTS",
]
