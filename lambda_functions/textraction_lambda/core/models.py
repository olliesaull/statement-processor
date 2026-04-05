"""
Pydantic models used across the Lambda.

These are small, focused schemas that:
- Validate/normalize the StepFunctions -> Lambda event payload (`TextractionEvent`)
- Define the extraction output contract (`ExtractionResult`)
- Provide a typed representation of extracted statement data (`StatementItem`, `SupplierStatement`)
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `total` values can arrive as numbers or numeric-looking strings; we normalize them into a simple union.
Number = int | float | str


class TextractionEvent(BaseModel):
    """Typed event payload for this Lambda.

    StepFunctions passes keys in camelCase; we expose snake_case
    attributes via Pydantic field aliases.
    """

    model_config = ConfigDict(populate_by_name=True)

    statement_id: str = Field(alias="statementId")
    tenant_id: str = Field(alias="tenantId")
    contact_id: str = Field(alias="contactId")
    pdf_key: str = Field(alias="pdfKey")
    json_key: str = Field(alias="jsonKey")
    pdf_bucket: str | None = Field(default=None, alias="pdfBucket")
    page_count: int = Field(alias="pageCount")


class StatementItem(BaseModel):
    """Canonical line item extracted from a supplier statement."""

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


class ExtractionResult(BaseModel):
    """Output contract for the extraction layer.

    Everything a caller needs from extract_statement(). Items already
    have floats in total (convert_amount ran inside the boundary).
    """

    items: list[StatementItem]
    detected_headers: list[str]
    header_mapping: dict[str, str]
    date_format: str
    date_confidence: str  # "high" or "low"
    decimal_separator: str
    thousands_separator: str
    input_tokens: int
    output_tokens: int
    request_ids: list[str] = Field(default_factory=list)


class SupplierStatement(BaseModel):
    """Top-level container for extracted statement rows.

    Self-describing: carries all metadata needed for display
    formatting, so the service never needs ContactConfig.
    """

    statement_items: list[StatementItem] = Field(default_factory=list)
    earliest_item_date: str | None = None
    latest_item_date: str | None = None
    date_format: str = ""
    date_confidence: str = "high"
    decimal_separator: str = "."
    thousands_separator: str = ","
    detected_headers: list[str] = Field(default_factory=list)
    header_mapping: dict[str, str] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
