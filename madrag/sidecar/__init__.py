"""madRAG Sidecar writer infrastructure.

Spec: ``docs/madRAGSidecarFormat-zh.md``.

This package owns the *single executable specification* of the madRAG Sidecar
file format. Parser engines (native / mineru / docling) hand it an
``IRDoc`` (intermediate representation) describing the document; the writer
emits the spec-compliant ``*.parsed/`` directory.

See :func:`madrag.sidecar.writer.write_sidecar` for the entry point.
"""

from madrag.sidecar.ir import (
    AssetSpec,
    IRBlock,
    IRDoc,
    IRDrawing,
    IREquation,
    IRPosition,
    IRTable,
)
from madrag.sidecar.writer import write_sidecar

__all__ = [
    "AssetSpec",
    "IRBlock",
    "IRDoc",
    "IRDrawing",
    "IREquation",
    "IRPosition",
    "IRTable",
    "write_sidecar",
]
