"""
Pydantic models used across the Lambda.

These are small, focused schemas that:
- Validate/normalize the StepFunctions -> Lambda event payload (`TextractionEvent`)
- Provide a typed representation of extracted statement data (`StatementItem`, `SupplierStatement`)
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `total` values can arrive as numbers or numeric-looking strings; we normalize them into a simple union.
Number = Union[int, float, str]


class TextractionEvent(BaseModel):
    """
    Typed event payload for this Lambda.

    StepFunctions passes keys in camelCase (e.g. `jobId`); we expose snake_case attributes via Pydantic field aliases.
    """

    model_config = ConfigDict(populate_by_name=True)

    job_id: str = Field(alias="jobId")
    statement_id: str = Field(alias="statementId")
    tenant_id: str = Field(alias="tenantId")
    contact_id: str = Field(alias="contactId")
    pdf_key: str = Field(alias="pdfKey")
    json_key: str = Field(alias="jsonKey")
    pdf_bucket: Optional[str] = Field(default=None, alias="pdfBucket")


class StatementItem(BaseModel):
    """Canonical line item extracted from a supplier statement."""

    # These fields are populated by `core/transform.table_to_json`.
    statement_item_id: str = ""
    date: Optional[str] = ""
    number: Optional[str] = ""
    total: Dict[str, Number] = Field(default_factory=dict)
    item_type: str = "invoice"
    due_date: Optional[str] = ""
    reference: Optional[str] = ""
    raw: dict = Field(default_factory=dict)

    @classmethod
    def _coerce_number(cls, v: Any) -> Any:
        # Best-effort conversion of numeric-looking values into int/float; otherwise keep the original.
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
    def _coerce_total(cls, v: Any) -> Dict[str, Number]:  # type: ignore[no-untyped-def]
        # Normalize `total` into a simple `{label: value}` mapping regardless of the input shape.
        def _coerce_val(val: Any) -> Number:
            return cls._coerce_number(val)

        coerced: Dict[str, Number] = {}

        if v is None:
            return {}
        if isinstance(v, dict):
            for key, value in v.items():
                label = str(key or "").strip()
                if not label:
                    continue
                coerced[label] = _coerce_val(value)
            return coerced
        if isinstance(v, list):
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
    earliest_item_date: Optional[str] = None
    latest_item_date: Optional[str] = None
