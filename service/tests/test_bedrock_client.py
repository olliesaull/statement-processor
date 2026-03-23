"""Tests for Bedrock config suggestion client."""

import json

import core.bedrock_client as bedrock_client_module
from core.bedrock_client import build_suggestion_prompt, parse_suggestion_response, suggest_column_mapping


def test_build_suggestion_prompt_includes_sdf_tokens() -> None:
    """Prompt must include the SDF token table so the LLM outputs correct format."""
    headers = ["Date", "Invoice No", "Amount"]
    rows = [["15/03/2025", "INV-001", "1,234.56"]]
    prompt = build_suggestion_prompt(headers, rows)
    assert "YYYY" in prompt
    assert "DD/MM/YYYY" in prompt
    assert "Invoice No" in prompt
    assert "1,234.56" in prompt


def test_parse_suggestion_response_extracts_config() -> None:
    """Tool use response should be parsed into config dict + confidence notes."""
    tool_input = {
        "number": "Invoice No",
        "date": "Date",
        "due_date": "",
        "total": ["Amount"],
        "date_format": "DD/MM/YYYY",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "confidence_notes": "High confidence mapping",
    }
    mock_response = {"output": {"message": {"content": [{"toolUse": {"name": "suggest_config", "toolUseId": "test-id", "input": tool_input}}]}}, "stopReason": "tool_use"}
    config, notes = parse_suggestion_response(mock_response)
    assert config["number"] == "Invoice No"
    assert config["date"] == "Date"
    assert config["total"] == ["Amount"]
    assert config["date_format"] == "DD/MM/YYYY"
    assert notes == "High confidence mapping"


def test_parse_suggestion_response_raises_on_missing_tool_use() -> None:
    """Should raise ValueError when response has no tool use block."""
    mock_response = {"output": {"message": {"content": [{"text": "No tool use here"}]}}, "stopReason": "end_turn"}
    try:
        parse_suggestion_response(mock_response)
        assert False, "Expected ValueError"
    except ValueError:
        pass


def test_suggest_column_mapping_calls_bedrock_and_returns_config(monkeypatch) -> None:
    """Integration: verify the full suggest flow calls Bedrock and returns parsed result."""
    tool_input = {
        "number": "Ref",
        "date": "Date",
        "due_date": "",
        "total": ["Debit", "Credit"],
        "date_format": "DD/MM/YYYY",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "confidence_notes": "",
    }
    fake_response = {"output": {"message": {"content": [{"toolUse": {"name": "suggest_config", "toolUseId": "test-id", "input": tool_input}}]}}, "stopReason": "tool_use"}

    class FakeBedrock:
        def converse(self, **kwargs):
            return fake_response

    monkeypatch.setattr(bedrock_client_module, "bedrock_runtime_client", FakeBedrock())

    config, notes = suggest_column_mapping(headers=["Date", "Ref", "Debit", "Credit"], rows=[["15/03/2025", "INV-001", "100.00", ""], ["20/03/2025", "INV-002", "", "50.00"]])
    assert config["number"] == "Ref"
    assert config["total"] == ["Debit", "Credit"]
