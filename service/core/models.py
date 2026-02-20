"""
Shared models for statement processing.

These models provide:
- A typed representation of extracted statement items (`StatementItem`)
- A typed representation of contact mapping config (`ContactConfig`)
- A comparison payload for statement vs. Xero values (`CellComparison`)
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

Number = int | float | str


class StatementItem(BaseModel):
    """Canonical line item extracted from a supplier statement."""

    statement_item_id: str = ""
    date: str | None = ""
    number: str | None = ""
    total: dict[str, Number] = Field(default_factory=dict)
    item_type: str = "invoice"
    due_date: str | None = ""
    reference: str | None = ""
    raw: dict[str, Any] = Field(default_factory=dict)

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
    def _coerce_total(cls, v: Any) -> dict[str, Number]:
        """Normalize `total` into a `{label: value}` mapping regardless of input shape."""

        def _coerce_val(val: Any) -> Number:
            return cls._coerce_number(val)

        if v is None:
            return {}
        if isinstance(v, dict):
            coerced: dict[str, Number] = {}
            for key, value in v.items():
                label = str(key or "").strip()
                if not label:
                    continue
                coerced[label] = _coerce_val(value)
            return coerced
        if isinstance(v, list):
            coerced: dict[str, Number] = {}
            for entry in v:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label") or "").strip()
                if not label:
                    continue
                coerced[label] = _coerce_val(entry.get("value"))
            return coerced
        return {}


class ContactConfig(BaseModel):
    """Contact-specific mapping config persisted for statement extraction and rendering."""

    model_config = ConfigDict(extra="allow")

    date: str = ""
    due_date: str = ""
    number: str = ""
    total: list[str] = Field(default_factory=list)
    date_format: str = ""
    decimal_separator: str = "."
    thousands_separator: str = ","
    raw: dict[str, str] = Field(default_factory=dict)

    @field_validator("total", mode="before")
    @classmethod
    def _coerce_total(cls, value: Any) -> list[str]:
        """Normalize configured total headers into a trimmed string list."""
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("total must be a list")
        return [str(item).strip() for item in value if str(item).strip()]


@dataclass(frozen=True)
class CellComparison:
    """Represents the comparison of a single statement cell versus the Xero value."""

    header: str
    statement_value: str
    xero_value: str
    matches: bool
    canonical_field: str | None = None
