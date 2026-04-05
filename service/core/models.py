"""Pydantic models used across the service."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

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
        """Best-effort conversion of numeric-looking values into int/float."""
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
        """Normalize `total` into a simple `{label: value}` mapping."""
        def _coerce_val(val: Any) -> Number:
            return cls._coerce_number(val)

        coerced: dict[str, Number] = {}
        if v is None:
            return {}
        if isinstance(v, dict):
            for key, value in v.items():
                label = str(key or "").strip()
                if not label:
                    continue
                coerced[label] = _coerce_val(value)
            return coerced
        return {}


@dataclass(frozen=True)
class CellComparison:
    """Per-cell comparison between statement and Xero values."""

    header: str
    statement_value: str
    xero_value: str
    matches: bool
    canonical_field: str | None = None
