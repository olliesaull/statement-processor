"""Models and loaders for Playwright statement test runs."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, field_validator

DEFAULT_BASE_URL = "http://localhost:8080"
STATEMENTS_DIR = Path(os.getenv("PLAYWRIGHT_STATEMENTS_DIR", "/statements"))
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
TEST_RUNS_PATH = FIXTURES_DIR / "test_runs.json"
EXPECTED_EXCEL_DIR = FIXTURES_DIR / "expected"


class StatementFlowRun(BaseModel):
    """
    Represent a statement flow test run.

    This model captures the inputs needed for a single end-to-end statement scenario.

    Attributes:
        base_url: Base URL for the app under test.
        tenant_id: Tenant ID to seed in the test login.
        tenant_name: Tenant name to seed in the test login.
        contact_name: Contact name used in config + upload.
        number_column: Statement column mapped to invoice number.
        date_column: Statement column mapped to transaction date.
        total_column: Statement columns mapped to totals.
        date_format: Date format string for statement parsing.
        statement_filename: PDF filename stored in the statements directory.
        expected_excel_filename: Baseline Excel filename under fixtures/expected.
        expected_table_text: Optional substrings to assert in the statement table.
    """

    base_url: str = DEFAULT_BASE_URL
    tenant_id: str
    tenant_name: str
    contact_name: str
    number_column: str
    date_column: str
    total_column: list[str]
    date_format: str = "DD/MM/YYYY"
    statement_filename: str
    expected_excel_filename: str | None = None
    expected_table_text: list[str] = Field(default_factory=list)

    @field_validator("statement_filename")
    @classmethod
    def _validate_statement_filename(cls, value: str) -> str:
        """Validate the statement PDF filename.

        Args:
            value: Raw filename.

        Returns:
            Normalized filename.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("statement_filename must be set")
        return stripped

    @field_validator("tenant_id", "tenant_name", "contact_name", "number_column", "date_column", "date_format")
    @classmethod
    def _require_non_empty(cls, value: str, info: Any) -> str:
        """Ensure required string fields are not empty.

        Args:
            value: Field value.
            info: Pydantic field context.

        Returns:
            Normalized non-empty string.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{info.field_name} must be set")
        return stripped

    @field_validator("total_column", mode="before")
    @classmethod
    def _validate_total_column(cls, value: object) -> list[str]:
        """Validate total columns are provided.

        Args:
            value: Raw total column value.

        Returns:
            List of non-empty total column labels.
        """
        cleaned = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        if not cleaned:
            raise ValueError("total_column must contain at least one entry")
        return cleaned

    @field_validator("expected_excel_filename")
    @classmethod
    def _validate_expected_excel_filename(cls, value: str | None) -> str | None:
        """Normalize the expected Excel filename.

        Args:
            value: Raw filename or None.

        Returns:
            Normalized filename or None.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return stripped

    @field_validator("expected_table_text", mode="before")
    @classmethod
    def _parse_expected_table_text(cls, value: object) -> list[str]:
        """Coerce expected table text into a list of strings.

        Args:
            value: Raw value from env or JSON.

        Returns:
            List of non-empty strings.
        """
        if value is None:
            return []
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
            return [item for item in items if item]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise ValueError("expected_table_text must be a list or comma-separated string")

    def statement_path(self) -> Path:
        """Build the PDF path for this run.

        Returns:
            Path to the statement PDF inside the statements directory.
        """
        return STATEMENTS_DIR / self.statement_filename

    def expected_excel_path(self) -> Path | None:
        """Build the baseline Excel path for this run.

        Returns:
            Path to the expected Excel file or None when not configured.
        """
        if not self.expected_excel_filename:
            return None
        return EXPECTED_EXCEL_DIR / self.expected_excel_filename


def load_test_runs() -> list[StatementFlowRun]:
    """Load statement runs from the fixtures file.

    Returns:
        List of StatementFlowRun instances.
    """
    if not TEST_RUNS_PATH.exists():
        pytest.skip(f"{TEST_RUNS_PATH.name} not found in playwright_tests/fixtures.", allow_module_level=True)
    try:
        payload = json.loads(TEST_RUNS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{TEST_RUNS_PATH.name} must be valid JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{TEST_RUNS_PATH.name} must be a non-empty JSON list")
    return [StatementFlowRun.model_validate(item) for item in payload]
