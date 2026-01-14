"""
Shared models for statement processing.

These models provide:
- A typed representation of extracted statement items (`StatementItem`)
- A comparison payload for statement vs. Xero values (`CellComparison`)
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field, field_validator

Number = Union[int, float, str]


class StatementItem(BaseModel):
    """Canonical line item extracted from a supplier statement."""

    statement_item_id: str = ""
    date: Optional[str] = ""
    number: Optional[str] = ""
    total: Dict[str, Number] = Field(default_factory=dict)
    item_type: str = "invoice"
    due_date: Optional[str] = ""
    reference: Optional[str] = ""
    raw: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def _coerce_number(cls, v: Any) -> Any:
        """Convert numeric-looking values into int/float when possible."""
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return v
        s = str(v).replace(",", "").replace(" ", "").strip()
        if s == "":
            return ""
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return v

    @field_validator("total", mode="before")
    @classmethod
    def _coerce_total(cls, v: Any) -> Dict[str, Number]:
        """Normalize `total` into a `{label: value}` mapping regardless of input shape."""

        def _coerce_val(val: Any) -> Number:
            return cls._coerce_number(val)

        if v is None:
            return {}
        if isinstance(v, dict):
            coerced: Dict[str, Number] = {}
            for key, value in v.items():
                label = str(key or "").strip()
                if not label:
                    continue
                coerced[label] = _coerce_val(value)
            return coerced
        if isinstance(v, list):
            coerced: Dict[str, Number] = {}
            for entry in v:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label") or "").strip()
                if not label:
                    continue
                coerced[label] = _coerce_val(entry.get("value"))
            return coerced
        return {}


@dataclass(frozen=True)
class CellComparison:
    """Represents the comparison of a single statement cell versus the Xero value."""

    header: str
    statement_value: str
    xero_value: str
    matches: bool
    canonical_field: Optional[str] = None
