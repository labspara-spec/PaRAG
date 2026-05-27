"""Section-aware chunking — the ``"S"`` strategy (default).

Detects document structure (headings, section boundaries) for each document
type and splits text *within* sections.  Each produced chunk carries heading
metadata so downstream retrieval can cite the exact section.

Supported section detection by document type:

==========  ==================================================================
Doc type    Detection strategy
==========  ==================================================================
md / mdx    ATX headings ``# … ######``
html / htm  ``<h1>``–``<h6>`` tags (regex, no heavy HTML parser dependency)
rst         Underline-only title convention (``===``, ``---``, ``~~~``, etc.)
txt / log   ALL-CAPS lines, numbered prefixes (``1.``, ``1.1.``, ``A.``)
py          Top-level ``class`` / ``def`` / ``async def`` declarations
js / ts     Top-level ``function``, ``class``, ``const``/``let``/``var``
            arrow-function assignments
java/kotlin Top-level ``class``, ``interface``, ``enum`` declarations
others      No section detection → falls back to Strategy R
==========  ==================================================================

When zero sections are detected the function delegates entirely to
:func:`lightrag.chunker.recursive_character.chunking_by_recursive_character`
so the output is always at least as good as Strategy R.
"""

from __future__ import annotations

import re
from typing import Any

from lightrag.utils import Tokenizer, logger
from lightrag.chunker.recursive_character import chunking_by_recursive_character


# ---------------------------------------------------------------------------
# Section-split regex patterns (compiled once at import time)
# ---------------------------------------------------------------------------

# Markdown ATX headings: ``# Title``, ``## Sub``, …, ``###### Leaf``
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)

# HTML headings (non-greedy, single-line first pass)
_HTML_HEADING_RE = re.compile(
    r"<h([1-6])[^>]*>(.*?)</h\1>",
    re.IGNORECASE | re.DOTALL,
)

# RST underline characters (official set) — line must be ≥2 chars and all same char
_RST_UNDERLINE_CHARS = frozenset("=-~^_*+#<>")
_RST_UNDERLINE_RE = re.compile(r"^([=\-~^_*+#<>])\1+$")

# Plain-text section heuristics
_TXT_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*\.?\s|[A-Z]\.\s)\S")  # ``1.``, ``1.1.``, ``A.``
_TXT_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z0-9 ,:'\-]{3,}$")  # ALL-CAPS line ≥4 chars

# Python top-level declarations
_PY_DECL_RE = re.compile(r"^(class|def|async def)\s+(\w+)", re.MULTILINE)

# JavaScript / TypeScript top-level declarations
_JS_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+(\w+)|class\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|\w+\s*=>))",
    re.MULTILINE,
)

# Java / Kotlin / C# top-level type declarations
_JAVA_DECL_RE = re.compile(
    r"^(?:public\s+|private\s+|protected\s+|internal\s+)?(?:static\s+|abstract\s+|final\s+)*"
    r"(?:class|interface|enum|object|record)\s+(\w+)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CODE_TYPES = frozenset(
    {
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "java",
        "kt",
        "cs",
        "go",
        "rb",
        "php",
        "cpp",
        "c",
        "h",
        "hpp",
        "swift",
    }
)


def _heading_dict(
    level: int, heading: str, parent_headings: list[str]
) -> dict[str, Any]:
    return {"level": level, "heading": heading, "parent_headings": list(parent_headings)}


def _section_path(parent_headings: list[str], heading: str) -> list[str]:
    return [h for h in parent_headings if h] + ([heading] if heading else [])


def _split_into_sections_md(content: str) -> list[tuple[str, int, str]]:
    """Return list of (body_text, level, heading_text) for Markdown."""
    matches = list(_MD_HEADING_RE.finditer(content))
    if not matches:
        return []

    sections: list[tuple[str, int, str]] = []
    prev_end = 0
    for i, m in enumerate(matches):
        # Text before the first heading becomes an intro section at level 0
        if i == 0 and m.start() > 0:
            intro = content[:m.start()].strip()
            if intro:
                sections.append((intro, 0, ""))
        level = len(m.group(1))
        heading_text = m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end].strip()
        sections.append((body, level, heading_text))
        prev_end = body_end

    return sections


def _split_into_sections_html(content: str) -> list[tuple[str, int, str]]:
    """Return list of (body_text, level, heading_text) for HTML."""
    # Strip HTML tags from heading text helper
    _tag_re = re.compile(r"<[^>]+>")

    matches = list(_HTML_HEADING_RE.finditer(content))
    if not matches:
        return []

    sections: list[tuple[str, int, str]] = []
    for i, m in enumerate(matches):
        if i == 0 and m.start() > 0:
            intro = _tag_re.sub("", content[: m.start()]).strip()
            if intro:
                sections.append((intro, 0, ""))
        level = int(m.group(1))
        heading_text = _tag_re.sub("", m.group(2)).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = _tag_re.sub("", content[body_start:body_end]).strip()
        sections.append((body, level, heading_text))

    return sections


def _split_into_sections_rst(content: str) -> list[tuple[str, int, str]]:
    """Return list of (body_text, level, heading_text) for reStructuredText.

    RST uses underline (and optional overline) adornments to denote heading
    level.  We assign level by first-seen order of adornment character.
    """
    lines = content.splitlines()
    heading_positions: list[tuple[int, int, str]] = []  # (line_idx, level, text)
    char_order: list[str] = []

    for i in range(1, len(lines)):
        m = _RST_UNDERLINE_RE.match(lines[i].strip())
        if m and len(lines[i].strip()) >= len(lines[i - 1].strip()):
            char = m.group(1)
            if char not in char_order:
                char_order.append(char)
            level = char_order.index(char) + 1
            heading_positions.append((i - 1, level, lines[i - 1].strip()))

    if not heading_positions:
        return []

    sections: list[tuple[str, int, str]] = []
    for j, (line_idx, level, heading_text) in enumerate(heading_positions):
        if j == 0 and line_idx > 0:
            intro = "\n".join(lines[:line_idx]).strip()
            if intro:
                sections.append((intro, 0, ""))
        next_line = (
            heading_positions[j + 1][0]
            if j + 1 < len(heading_positions)
            else len(lines)
        )
        # Skip the underline line (line_idx + 1)
        body = "\n".join(lines[line_idx + 2 : next_line]).strip()
        sections.append((body, level, heading_text))

    return sections


def _split_into_sections_txt(content: str) -> list[tuple[str, int, str]]:
    """Return sections for plain-text using heuristic heading detection."""
    lines = content.splitlines()
    heading_positions: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _TXT_ALLCAPS_RE.match(stripped) or _TXT_NUMBERED_RE.match(stripped):
            heading_positions.append((i, stripped))

    if not heading_positions:
        return []

    sections: list[tuple[str, int, str]] = []
    for j, (line_idx, heading_text) in enumerate(heading_positions):
        if j == 0 and line_idx > 0:
            intro = "\n".join(lines[:line_idx]).strip()
            if intro:
                sections.append((intro, 0, ""))
        next_line = (
            heading_positions[j + 1][0]
            if j + 1 < len(heading_positions)
            else len(lines)
        )
        body = "\n".join(lines[line_idx + 1 : next_line]).strip()
        sections.append((body, 1, heading_text))

    return sections


def _split_into_sections_code(
    content: str, doc_type: str
) -> list[tuple[str, int, str]]:
    """Return top-level declaration sections for code files."""
    if doc_type == "py":
        pattern = _PY_DECL_RE
        def _name(m: re.Match) -> str:
            return f"{m.group(1)} {m.group(2)}"
    elif doc_type in {"js", "ts", "jsx", "tsx"}:
        pattern = _JS_DECL_RE
        def _name(m: re.Match) -> str:
            return next(g for g in m.groups() if g)
    else:
        pattern = _JAVA_DECL_RE
        def _name(m: re.Match) -> str:
            return m.group(1)

    matches = list(pattern.finditer(content))
    if not matches:
        return []

    sections: list[tuple[str, int, str]] = []
    for i, m in enumerate(matches):
        if i == 0 and m.start() > 0:
            intro = content[: m.start()].strip()
            if intro:
                sections.append((intro, 0, ""))
        name = _name(m)
        body_start = m.start()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end].strip()
        sections.append((body, 1, name))

    return sections


def _detect_sections(
    content: str, doc_type: str
) -> list[tuple[str, int, str]]:
    """Dispatch to the appropriate section detector.  Returns [] on no-match."""
    dt = (doc_type or "").lower().lstrip(".")
    if dt in {"md", "mdx"}:
        return _split_into_sections_md(content)
    if dt in {"html", "htm", "xhtml"}:
        return _split_into_sections_html(content)
    if dt == "rst":
        return _split_into_sections_rst(content)
    if dt in {"txt", "log", "conf", "ini", "properties"}:
        return _split_into_sections_txt(content)
    if dt in _CODE_TYPES:
        return _split_into_sections_code(content, dt)
    return []


def _build_heading_stack(
    sections: list[tuple[str, int, str]]
) -> list[tuple[str, list[str]]]:
    """Convert a flat (body, level, heading) list into (body, parent_headings, heading) tuples.

    Maintains a heading stack so each section knows its full ancestor chain.
    Returns list of (body, parent_headings_list, heading_text).
    """
    stack: list[tuple[int, str]] = []  # (level, heading_text)
    result: list[tuple[str, list[str], str]] = []

    for body, level, heading in sections:
        if level == 0:
            # Intro block — no heading
            result.append((body, [], ""))
            continue
        # Pop stack entries at same or deeper level
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_headings = [h for _, h in stack]
        result.append((body, parent_headings, heading))
        stack.append((level, heading))

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunking_by_section_aware(
    tokenizer: Tokenizer,
    content: str,
    chunk_token_size: int = 1200,
    *,
    doc_type: str = "",
    chunk_overlap_token_size: int = 100,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Section-aware chunker — the ``"S"`` strategy (default).

    Detects structural sections in *content* based on *doc_type*, then
    applies recursive-character chunking *within* each section.  Every
    produced chunk carries ``heading`` and ``section_path`` metadata.

    Falls back to :func:`chunking_by_recursive_character` when no sections
    are detected (behaviour identical to Strategy R).

    Args:
        tokenizer: LightRAG tokenizer for token-accurate sizing.
        content: Full document text.
        chunk_token_size: Token budget per chunk.
        doc_type: Lowercase file extension without leading dot (e.g. ``"md"``,
                  ``"pdf"``, ``"txt"``).  Used to pick the section detector.
        chunk_overlap_token_size: Token overlap between adjacent chunks within
                                  a section.

    Returns:
        Ordered list of chunk dicts.  Each dict contains at minimum:
        ``tokens``, ``content``, ``chunk_order_index``.  Chunks from detected
        sections also carry ``heading`` and ``section_path``.
    """
    if not content or not content.strip():
        return []

    sections_raw = _detect_sections(content, doc_type)

    if not sections_raw:
        logger.debug(
            "[section_aware] no sections detected for doc_type=%r; "
            "delegating to recursive_character.",
            doc_type,
        )
        return chunking_by_recursive_character(
            tokenizer,
            content,
            chunk_token_size,
            chunk_overlap_token_size=chunk_overlap_token_size,
        )

    annotated = _build_heading_stack(sections_raw)
    results: list[dict[str, Any]] = []

    for body, parent_headings, heading_text in annotated:
        if not body:
            continue

        sub_chunks = chunking_by_recursive_character(
            tokenizer,
            body,
            chunk_token_size,
            chunk_overlap_token_size=chunk_overlap_token_size,
        )

        heading_meta: dict[str, Any] | None = None
        if heading_text:
            # Find level from original sections_raw
            level = next(
                (lvl for _, lvl, ht in sections_raw if ht == heading_text),
                1,
            )
            heading_meta = _heading_dict(level, heading_text, parent_headings)
        elif parent_headings:
            heading_meta = _heading_dict(0, "", parent_headings)

        sp = _section_path(parent_headings, heading_text)

        for sc in sub_chunks:
            chunk: dict[str, Any] = {
                **sc,
                "chunk_order_index": len(results),
            }
            if heading_meta is not None:
                chunk["heading"] = heading_meta
            if sp:
                chunk["section_path"] = sp
            results.append(chunk)

    if not results:
        # All section bodies were empty — fall back
        logger.warning(
            "[section_aware] all detected sections had empty bodies for "
            "doc_type=%r; falling back to recursive_character.",
            doc_type,
        )
        return chunking_by_recursive_character(
            tokenizer,
            content,
            chunk_token_size,
            chunk_overlap_token_size=chunk_overlap_token_size,
        )

    return results
