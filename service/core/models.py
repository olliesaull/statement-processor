"""Pydantic models used across the service.

StatementItem and Number are imported from the shared common package.
CellComparison is service-only (used for statement/Xero reconciliation).
"""

from dataclasses import dataclass

from src.models import StatementItem
from src.types import Number

# Re-export so existing imports continue to work.
__all__ = ["CellComparison", "Number", "StatementItem"]


@dataclass(frozen=True)
class CellComparison:
    """Per-cell comparison between statement and Xero values."""

    header: str
    statement_value: str
    xero_value: str
    matches: bool
    canonical_field: str | None = None
