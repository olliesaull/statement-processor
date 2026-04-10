"""Tests for typed extraction data structures.

Verifies the frozen dataclasses introduced in B1a behave correctly:
field access, immutability, and usage in chunk_pdf / _call_bedrock.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.extraction import BedrockResponse, ChunkResult, PdfChunk, chunk_pdf


class TestPdfChunk:
    """PdfChunk: frozen dataclass replacing (bytes, int, int) tuples."""

    def test_construction_and_field_access(self) -> None:
        chunk = PdfChunk(pdf_bytes=b"test", start_page=1, end_page=5)
        assert chunk.pdf_bytes == b"test"
        assert chunk.start_page == 1
        assert chunk.end_page == 5

    def test_frozen(self) -> None:
        chunk = PdfChunk(pdf_bytes=b"test", start_page=1, end_page=5)
        with pytest.raises(AttributeError):
            chunk.start_page = 2  # type: ignore[misc]


class TestBedrockResponse:
    """BedrockResponse: frozen dataclass replacing 4-tuples."""

    def test_construction_and_field_access(self) -> None:
        resp = BedrockResponse(tool_input={"items": []}, input_tokens=100, output_tokens=50, request_id="req-001")
        assert resp.tool_input == {"items": []}
        assert resp.input_tokens == 100
        assert resp.output_tokens == 50
        assert resp.request_id == "req-001"

    def test_frozen(self) -> None:
        resp = BedrockResponse(tool_input={}, input_tokens=0, output_tokens=0, request_id="")
        with pytest.raises(AttributeError):
            resp.input_tokens = 99  # type: ignore[misc]


class TestChunkResult:
    """ChunkResult: frozen dataclass replacing 6-tuples from _process_continuation."""

    def test_construction_and_field_access(self) -> None:
        result = ChunkResult(chunk_index=2, items=[{"date": "2024-01-01"}], input_tokens=80, output_tokens=30, request_id="req-002", warnings={"date_format": "MM/DD/YYYY"})
        assert result.chunk_index == 2
        assert len(result.items) == 1
        assert result.input_tokens == 80
        assert result.warnings == {"date_format": "MM/DD/YYYY"}

    def test_frozen(self) -> None:
        result = ChunkResult(chunk_index=0, items=[], input_tokens=0, output_tokens=0, request_id="", warnings={})
        with pytest.raises(AttributeError):
            result.chunk_index = 1  # type: ignore[misc]


class TestChunkPdfReturnType:
    """Verify chunk_pdf returns list[PdfChunk] instead of raw tuples."""

    def test_returns_pdf_chunks(self) -> None:
        """chunk_pdf must return PdfChunk instances with named fields."""
        from tests.helpers import make_test_pdf

        pdf_bytes = make_test_pdf(pages=3)
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks = chunk_pdf(reader)

        assert len(chunks) >= 1
        for chunk in chunks:
            assert isinstance(chunk, PdfChunk)
            assert isinstance(chunk.pdf_bytes, bytes)
            assert isinstance(chunk.start_page, int)
            assert isinstance(chunk.end_page, int)
            assert chunk.start_page >= 1
