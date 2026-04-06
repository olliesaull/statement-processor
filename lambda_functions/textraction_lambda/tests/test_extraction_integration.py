"""Integration tests for extract_statement with mocked Bedrock responses."""

from typing import Any
from unittest.mock import MagicMock, patch

from core.extraction import extract_statement
from core.models import ExtractionResult


def _mock_bedrock_response(tool_input: dict[str, Any], input_tokens: int = 100, output_tokens: int = 50) -> dict[str, Any]:
    """Build a mock Bedrock Converse API response with tool use."""
    return {
        "output": {"message": {"content": [{"toolUse": {"name": "extract_statement_rows", "input": tool_input}}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "ResponseMetadata": {"RequestId": "test-request-id-001"},
    }


def _single_chunk_tool_input() -> dict[str, Any]:
    """Tool input for a simple single-chunk PDF."""
    return {
        "detected_headers": ["Date", "Invoice No.", "Amount"],
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "column_order": ["date", "number", "Amount"],
        "items": [["15/01/2024", "INV-001", "1,234.56"], ["20/01/2024", "INV-002", "789.00"]],
    }


class TestExtractStatement:
    """extract_statement: full pipeline with mocked Bedrock."""

    @patch("core.extraction._get_bedrock_client")
    def test_single_chunk_pdf(self, mock_get_client: MagicMock) -> None:
        """Single-page PDF -> correct ExtractionResult."""
        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_bedrock_response(_single_chunk_tool_input())
        mock_get_client.return_value = mock_client

        from tests.helpers import make_test_pdf

        pdf_bytes = make_test_pdf(pages=1)

        result = extract_statement(pdf_bytes, page_count=1)

        assert isinstance(result, ExtractionResult)
        assert len(result.items) == 2
        assert result.items[0].date == "15/01/2024"
        assert result.items[0].number == "INV-001"
        assert result.items[0].total == {"Amount": 1234.56}
        assert result.date_format == "DD/MM/YYYY"
        assert result.date_confidence == "high"
        assert result.header_mapping == {"Date": "date", "Invoice No.": "number", "Amount": "total"}
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    @patch("core.extraction._get_bedrock_client")
    def test_multi_chunk_merges_items(self, mock_get_client: MagicMock) -> None:
        """Multi-chunk PDF -> items merged from all chunks."""
        chunk1_input = _single_chunk_tool_input()
        chunk2_input = {
            "detected_headers": ["Date", "Invoice No.", "Amount"],
            "date_format": "DD/MM/YYYY",
            "date_confidence": "high",
            "decimal_separator": ".",
            "thousands_separator": ",",
            "column_order": ["date", "number", "Amount"],
            "items": [["25/01/2024", "INV-003", "456.00"]],
        }

        mock_client = MagicMock()
        mock_client.converse.side_effect = [_mock_bedrock_response(chunk1_input, 100, 50), _mock_bedrock_response(chunk2_input, 80, 30)]
        mock_get_client.return_value = mock_client

        from tests.helpers import make_test_pdf

        pdf_bytes = make_test_pdf(pages=15)

        result = extract_statement(pdf_bytes, page_count=15)

        assert len(result.items) == 3
        assert result.input_tokens == 180
        assert result.output_tokens == 80

    @patch("core.extraction._get_bedrock_client")
    def test_metadata_from_chunk1(self, mock_get_client: MagicMock) -> None:
        """Chunk 1 metadata wins when chunks disagree."""
        chunk1_input = _single_chunk_tool_input()
        chunk2_input = dict(_single_chunk_tool_input())
        chunk2_input["date_format"] = "MM/DD/YYYY"
        chunk2_input["items"] = [["01/25/2024", "INV-003", "100.00"]]

        mock_client = MagicMock()
        mock_client.converse.side_effect = [_mock_bedrock_response(chunk1_input), _mock_bedrock_response(chunk2_input)]
        mock_get_client.return_value = mock_client

        from tests.helpers import make_test_pdf

        pdf_bytes = make_test_pdf(pages=15)

        result = extract_statement(pdf_bytes, page_count=15)

        assert result.date_format == "DD/MM/YYYY"
