"""Unit tests for ``chunking_by_section_aware`` (process_options=S)."""

import pytest

from lightrag.chunker import chunking_by_section_aware
from lightrag.utils import Tokenizer, TokenizerInterface


class _CharTokenizer(TokenizerInterface):
    """1 char ≈ 1 token for deterministic assertions."""

    def encode(self, content: str):
        return [ord(ch) for ch in content]

    def decode(self, tokens):
        return "".join(chr(t) for t in tokens)


def _tok() -> Tokenizer:
    return Tokenizer("char-tokenizer", _CharTokenizer())


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_md_single_heading_produces_chunk():
    md = "# Introduction\n\nThis is the intro.\n"
    chunks = chunking_by_section_aware(_tok(), md, chunk_token_size=500, doc_type="md", chunk_overlap_token_size=0)
    assert len(chunks) >= 1
    joined = " ".join(c["content"] for c in chunks)
    assert "Introduction" in joined or "intro" in joined


@pytest.mark.offline
def test_md_heading_hierarchy_in_chunk():
    md = "# Chapter 1\n\n## Section 1.1\n\nBody text.\n"
    chunks = chunking_by_section_aware(_tok(), md, chunk_token_size=500, doc_type="md", chunk_overlap_token_size=0)
    assert len(chunks) >= 1
    # Section 1.1 chunk should have Chapter 1 as parent
    section_chunk = next(
        (c for c in chunks if c.get("heading") and "Section 1.1" in c["heading"]),
        None,
    )
    if section_chunk:
        assert "Chapter 1" in section_chunk.get("parent_headings", [])


@pytest.mark.offline
def test_md_multiple_sections_split():
    md = "# Sec A\n\nContent A.\n\n# Sec B\n\nContent B.\n"
    chunks = chunking_by_section_aware(_tok(), md, chunk_token_size=500, doc_type="md", chunk_overlap_token_size=0)
    headings = [c.get("heading", "") for c in chunks]
    assert any("Sec A" in h for h in headings)
    assert any("Sec B" in h for h in headings)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_html_h1_detected():
    html = "<h1>Title</h1><p>Some paragraph.</p><h2>Sub</h2><p>More text.</p>"
    chunks = chunking_by_section_aware(_tok(), html, chunk_token_size=500, doc_type="html", chunk_overlap_token_size=0)
    assert len(chunks) >= 1
    headings = [c.get("heading", "") for c in chunks]
    assert any("Title" in h for h in headings)


# ---------------------------------------------------------------------------
# Plain text (TXT)
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_txt_allcaps_section():
    txt = "INTRODUCTION\n\nThis is some intro content.\n\nCONCLUSION\n\nFinal thoughts.\n"
    chunks = chunking_by_section_aware(_tok(), txt, chunk_token_size=500, doc_type="txt", chunk_overlap_token_size=0)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# RST
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_rst_underline_section():
    rst = "Overview\n========\n\nRST body text.\n\nDetails\n-------\n\nMore detail.\n"
    chunks = chunking_by_section_aware(_tok(), rst, chunk_token_size=500, doc_type="rst", chunk_overlap_token_size=0)
    assert len(chunks) >= 1
    headings = [c.get("heading", "") for c in chunks]
    assert any("Overview" in h for h in headings)


# ---------------------------------------------------------------------------
# Python (code)
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_python_class_def_section():
    py = "class Foo:\n    def bar(self):\n        pass\n\nclass Baz:\n    def qux(self):\n        return 1\n"
    chunks = chunking_by_section_aware(_tok(), py, chunk_token_size=500, doc_type="py", chunk_overlap_token_size=0)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# Fallback: no sections → delegates to recursive character
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_no_sections_fallback():
    txt = "word " * 200
    chunks = chunking_by_section_aware(_tok(), txt, chunk_token_size=100, doc_type="txt", chunk_overlap_token_size=0)
    assert len(chunks) >= 1
    assert all("content" in c for c in chunks)


@pytest.mark.offline
def test_empty_input_returns_empty():
    chunks = chunking_by_section_aware(_tok(), "", chunk_token_size=500, doc_type="md", chunk_overlap_token_size=0)
    assert chunks == []


# ---------------------------------------------------------------------------
# Metadata fields on chunks
# ---------------------------------------------------------------------------

@pytest.mark.offline
def test_chunks_have_required_keys():
    md = "# Title\n\nContent here.\n"
    chunks = chunking_by_section_aware(_tok(), md, chunk_token_size=500, doc_type="md", chunk_overlap_token_size=0)
    for chunk in chunks:
        assert "content" in chunk
        assert "tokens" in chunk
        assert "chunk_order_index" in chunk
