"""
Pydantic models used across the Lambda.

These are small, focused schemas that:
- Validate/normalize the StepFunctions -> Lambda event payload (`ExtractionEvent`)
- Define the extraction output contract (`ExtractionResult`)
- Provide a typed representation of extracted statement data (`StatementItem`, `SupplierStatement`)

StatementItem and Number are imported from the shared common package.
Lambda-specific models (ExtractionEvent, ExtractionResult, SupplierStatement) remain here.
"""

from pydantic import BaseModel, ConfigDict, Field
from src.models import StatementItem
from src.types import Number

# Re-export so existing imports continue to work.
__all__ = ["ExtractionEvent", "ExtractionResult", "Number", "StatementItem", "SupplierStatement"]


class ExtractionEvent(BaseModel):
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
    detected_headers: list[str] = Field(default_factory=list)
    header_mapping: dict[str, str] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
