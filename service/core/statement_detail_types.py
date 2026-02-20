"""Typed payload shapes for statement detail processing.

These aliases make route/helper contracts explicit without changing runtime behavior.
Statement rows/documents remain plain dictionaries loaded from S3/cache/Xero.
"""

from typing import Any, Literal, TypedDict

from core.models import CellComparison

type StatementItemPayload = dict[str, Any]
type StatementRowsByHeader = list[dict[str, Any]]
type XeroDocumentPayload = dict[str, Any]
type MatchRecord = dict[str, Any]
type MatchedInvoiceMap = dict[str, MatchRecord]
type PaymentNumberMap = dict[str, list[XeroDocumentPayload]]


class ItemTypeMatchEntry(TypedDict):
    """Represents a matched statement item classification source."""

    type: Literal["invoice", "credit_note"]
    source: Literal["invoice_match", "credit_note_match"]


type MatchByItemId = dict[str, ItemTypeMatchEntry]


class StatementRowViewModel(TypedDict):
    """Represents one rendered row in the statement detail table."""

    statement_item_id: str | None
    cell_comparisons: list[CellComparison]
    matches: bool
    is_completed: bool
    flags: list[str]
    item_type: str
    item_type_label: str
    xero_invoice_id: str | None
    xero_credit_note_id: str | None
