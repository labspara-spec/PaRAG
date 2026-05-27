"""Unit tests for chunk metadata normalization (chunk_schema.py)."""

import pytest

from madrag.chunk_schema import normalize_chunk_section_meta, CHUNK_EXTENDED_META_FIELDS


@pytest.mark.offline
def test_file_name_derived_from_path():
    chunk = {"content": "x", "file_path": "/docs/report.pdf"}
    result = normalize_chunk_section_meta(chunk)
    assert result["file_name"] == "report.pdf"


@pytest.mark.offline
def test_file_type_derived_from_extension():
    chunk = {"content": "x", "file_path": "research/notes.md"}
    result = normalize_chunk_section_meta(chunk)
    assert result["file_type"] == "md"


@pytest.mark.offline
def test_file_type_lowercase():
    chunk = {"content": "x", "file_path": "DATA.PDF"}
    result = normalize_chunk_section_meta(chunk)
    assert result["file_type"] == "pdf"


@pytest.mark.offline
def test_chunk_char_count_matches_content_length():
    content = "Hello world"
    chunk = {"content": content, "file_path": "a.txt"}
    result = normalize_chunk_section_meta(chunk)
    assert result["chunk_char_count"] == len(content)


@pytest.mark.offline
def test_section_path_derived_from_heading():
    chunk = {
        "content": "body",
        "file_path": "doc.pdf",
        "heading": "Results",
        "parent_headings": ["Chapter 3"],
    }
    result = normalize_chunk_section_meta(chunk)
    assert result["section_path"] == ["Chapter 3", "Results"]


@pytest.mark.offline
def test_section_path_empty_when_no_heading():
    chunk = {"content": "body", "file_path": "doc.pdf"}
    result = normalize_chunk_section_meta(chunk)
    assert result.get("section_path", []) == []


@pytest.mark.offline
def test_file_size_bytes_injected():
    chunk = {"content": "x", "file_path": "a.txt"}
    result = normalize_chunk_section_meta(
        chunk, file_size_bytes=1024, ingested_at="2026-01-01T00:00:00Z"
    )
    assert result["file_size_bytes"] == 1024


@pytest.mark.offline
def test_ingested_at_injected():
    chunk = {"content": "x", "file_path": "a.txt"}
    result = normalize_chunk_section_meta(
        chunk, ingested_at="2026-05-27T10:00:00Z"
    )
    assert result["ingested_at"] == "2026-05-27T10:00:00Z"


@pytest.mark.offline
def test_no_path_does_not_crash():
    chunk = {"content": "x"}
    result = normalize_chunk_section_meta(chunk)
    assert "chunk_char_count" in result


@pytest.mark.offline
def test_chunk_extended_meta_fields_frozenset():
    required = {
        "page_number", "section_path", "file_name", "file_type",
        "file_size_bytes", "ingested_at", "processed_at", "chunk_char_count",
        "visibility", "owner", "allowed_users", "allowed_roles",
    }
    assert required <= CHUNK_EXTENDED_META_FIELDS
