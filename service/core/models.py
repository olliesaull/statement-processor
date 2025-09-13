from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator

Number = Union[int, float, str]


class StatementItem(BaseModel):
    amount_due: List[Number] = Field(default_factory=list)
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
    statement_items: List[StatementItem] = Field(default_factory=list)
