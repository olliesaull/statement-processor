from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CellComparison:
    """Represents the comparison of a single statement cell versus the Xero value."""

    header: str
    statement_value: str
    xero_value: str
    matches: bool
    canonical_field: Optional[str] = None
