"""
Pydantic models used across the Lambda.

These are small, focused schemas that:
- Validate/normalize the StepFunctions -> Lambda event payload (`TextractionEvent`)
- Provide a typed representation of contact mapping config (`ContactConfig`)
- Provide a typed representation of extracted statement data (`StatementItem`, `SupplierStatement`)
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `total` values can arrive as numbers or numeric-looking strings; we normalize them into a simple union.
Number = int | float | str


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
    pdf_bucket: str | None = Field(default=None, alias="pdfBucket")


class ContactConfig(BaseModel):
    """Contact mapping config used by table extraction and persisted in DynamoDB."""

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


class StatementItem(BaseModel):
    """Canonical line item extracted from a supplier statement."""

    # These fields are populated by `core/transform.table_to_json`.
    statement_item_id: str = ""
    date: str | None = ""
    number: str | None = ""
    total: dict[str, Number] = Field(default_factory=dict)
    item_type: str = "invoice"
    due_date: str | None = ""
    reference: str | None = ""
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
    @classmethod
    def _coerce_total(cls, v: Any) -> dict[str, Number]:
        # Normalize `total` into a simple `{label: value}` mapping from dict input.
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


class SupplierStatement(BaseModel):
    """Top-level container for extracted statement rows."""

    statement_items: list[StatementItem] = Field(default_factory=list)
    earliest_item_date: str | None = None
    latest_item_date: str | None = None
