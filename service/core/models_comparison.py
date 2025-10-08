from dataclasses import dataclass


@dataclass(frozen=True)
class CellComparison:
    """Represents the comparison of a single statement cell versus the Xero value."""

    header: str
    statement_value: str
    xero_value: str
    matches: bool
