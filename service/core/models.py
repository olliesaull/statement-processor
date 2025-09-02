from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator

Number = Union[int, float, str]

class DateField(BaseModel):
    value: str = ""
    format: str = "DD/MM/YY"

class StatementMeta(BaseModel):
    supplier_name: str = ""
    statement_date: DateField = Field(default_factory=DateField)
    currency: str = ""
    source_filename: str = ""

class StatementItem(BaseModel):
    transaction_date: DateField = Field(default_factory=DateField)
    customer_account_number: str = ""
    branch_store_shop: str = ""
    document_type: str = ""
    description_details: str = ""
    debit: Optional[Number] = ""
    credit: Optional[Number] = ""
    invoice_balance: Optional[Number] = ""
    balance: Optional[Number] = ""
    customer_reference: str = ""
    supplier_reference: str = ""
    allocated_to: str = ""
    raw: dict = Field(default_factory=dict)

    # Optional: coerce numeric-like strings here
    @field_validator("debit", "credit", "invoice_balance", "balance", mode="before")
    def _coerce_numbers(cls, v):
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

class SupplierStatement(BaseModel):
    statement_meta: StatementMeta
    statement_items: List[StatementItem] = Field(default_factory=list)
