from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

Number = Union[int, float, str]


class StatementItem(BaseModel):
    """Canonical line item extracted from a supplier statement."""
    statement_item_id: str = ""
    amount_due: Dict[str, Number] = Field(default_factory=dict)
    amount_paid: Optional[Number] = ""
    date: Optional[str] = ""
    due_date: Optional[str] = ""
    number: Optional[str] = ""
    reference: Optional[str] = ""
    statement_date_format: str = ""
    total: Optional[Number] = ""
    raw: dict = Field(default_factory=dict)

    # Optional: coerce numeric-like strings for known numeric fields
    @field_validator("total", "amount_paid", mode="before")
    def _coerce_numbers(cls, v: Any) -> Any:
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

    @field_validator("amount_due", mode="before")
    def _coerce_amount_due(cls, v: Any) -> Dict[str, Number]:  # type: ignore[no-untyped-def]
        def _coerce_val(val: Any) -> Number:
            return cls._coerce_numbers(val)

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


class SupplierStatement(BaseModel):
    """Top-level container for extracted statement rows."""
    statement_items: List[StatementItem] = Field(default_factory=list)
