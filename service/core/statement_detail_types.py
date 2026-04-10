"""Typed payload shapes for statement detail processing.

These aliases make route/helper contracts explicit without changing runtime behavior.
Statement rows/documents remain plain dictionaries loaded from S3/cache/Xero.

Design notes:
- StatementItemPayload, XeroDocumentPayload, and PaymentNumberMap are untyped
  aliases because their shapes come from external sources (S3 JSON, Xero API)
  and vary in practice. Pinning them to TypedDicts would require defensive
  casting throughout callers with no safety gain.
- MatchRecord IS typed because it is always constructed by this service's own
  matching logic with a fixed set of known fields.
"""

from typing import Any, Literal, TypedDict

from core.models import CellComparison

# region Untyped external payload aliases

# External data loaded from S3; schema is determined by the extraction lambda.
type StatementItemPayload = dict[str, Any]

# Rows derived from StatementItemPayload for display; shape mirrors raw PDF headers.
type StatementRowsByHeader = list[dict[str, Any]]

# Xero API document payload; shape is determined by the Xero API.
type XeroDocumentPayload = dict[str, Any]

# Maps Xero payment numbers to their document payloads.
type PaymentNumberMap = dict[str, list[XeroDocumentPayload]]

# endregion

# region Typed internal records


class MatchRecord(TypedDict):
    """Represents a single matched Xero document for a statement item.

    Built exclusively by this service's matching logic in statement_view.py.
    All fields are populated by _record_exact_matches and _record_substring_match.
    """

    invoice: XeroDocumentPayload
    """The matched Xero invoice or credit note payload."""

    statement_item: StatementItemPayload
    """The statement item that was matched."""

    match_type: Literal["exact", "substring"]
    """How the match was found: exact string equality or substring containment."""

    match_score: float
    """Confidence score for the match (always 1.0 for current strategies)."""

    matched_invoice_number: str
    """The Xero-side invoice number that was matched."""


# Maps statement invoice number -> MatchRecord for a set of matched items.
type MatchedInvoiceMap = dict[str, MatchRecord]

# endregion

# region View model types


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


# endregion
