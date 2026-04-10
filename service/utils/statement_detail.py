"""Statement detail helpers for building view data and classifying items.

Extracted from app.py to keep the route file focused on request handling.
These functions drive the statement detail page's data pipeline: fetching
Xero documents, classifying statement items, building display rows, and
persisting classification updates.
"""

import json
from io import BytesIO
from typing import Any

from flask import Response, current_app, request

from core.item_classification import guess_statement_item_type
from core.statement_detail_types import (
    ExcelExportRequest,
    MatchByItemId,
    MatchedInvoiceMap,
    PaymentNumberMap,
    StatementItemPayload,
    StatementRowsByHeader,
    StatementRowViewModel,
    StatementViewContext,
    XeroDocumentPayload,
)
from logger import logger
from utils.dynamo import get_statement_item_status_map, persist_item_types_to_dynamo
from utils.statement_rows import format_item_type_label, xero_ids_for_row
from utils.statement_view import build_right_rows, build_row_comparisons, match_invoices_to_statement_items, prepare_display_mappings
from utils.storage import statement_json_s3_key, upload_statement_to_s3
from xero_repository import get_xero_data_by_contact


def build_match_by_item_id(matched_invoice_to_statement_item: MatchedInvoiceMap) -> MatchByItemId:
    """Return a map of statement_item_id to matched document type/source.

    Iterates the invoice-to-statement-item match map and extracts the
    classification (invoice vs credit note) for each matched item so
    downstream classification can use it.

    Args:
        matched_invoice_to_statement_item: Map of matched Xero docs to statement items.

    Returns:
        Dict keyed by statement_item_id with type and source entries.
    """
    match_by_item_id: MatchByItemId = {}
    for match in matched_invoice_to_statement_item.values():
        stmt_item = match.get("statement_item") if isinstance(match, dict) else None
        doc = match.get("invoice") if isinstance(match, dict) else None
        if not isinstance(stmt_item, dict) or not isinstance(doc, dict):
            continue
        statement_item_id = stmt_item.get("statement_item_id")
        if not statement_item_id:
            continue
        doc_type = str(doc.get("type") or "").upper()
        if doc.get("credit_note_id") or doc_type.endswith("CREDIT"):
            match_by_item_id[statement_item_id] = {"type": "credit_note", "source": "credit_note_match"}
        else:
            match_by_item_id[statement_item_id] = {"type": "invoice", "source": "invoice_match"}
    return match_by_item_id


def build_payment_number_map(invoices: list[XeroDocumentPayload], payments: list[XeroDocumentPayload]) -> PaymentNumberMap:
    """Build a map of invoice number to payment rows for payment inference.

    Links Xero payments back to their parent invoice number so the
    classifier can detect payment-type statement items.

    Args:
        invoices: List of Xero invoice payloads.
        payments: List of Xero payment payloads.

    Returns:
        Dict mapping invoice numbers to their associated payment records.
    """
    invoice_number_by_id: dict[str, str] = {}
    for inv in invoices:
        inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
        inv_number = str(inv.get("number") or "").strip() if isinstance(inv, dict) else ""
        if inv_id and inv_number:
            invoice_number_by_id[str(inv_id)] = inv_number

    payment_number_map: PaymentNumberMap = {}
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        invoice_id = payment.get("invoice_id")
        if not invoice_id:
            continue
        invoice_number = invoice_number_by_id.get(str(invoice_id))
        if not invoice_number:
            continue
        payment_number_map.setdefault(invoice_number, []).append(payment)
    return payment_number_map


def classify_statement_items(
    *,
    items: list[StatementItemPayload],
    rows_by_header: StatementRowsByHeader,
    item_number_header: str | None,
    header_mapping: dict[str, str] | None,
    matched_invoice_to_statement_item: MatchedInvoiceMap,
    matched_numbers: set[str],
    match_by_item_id: MatchByItemId,
    payment_number_map: PaymentNumberMap,
    statement_id: str,
) -> tuple[list[str], dict[str, str]]:
    """Classify statement items in-place and return item types plus updates.

    Applies a priority chain: direct item-id match, invoice number match,
    payment number match, then heuristic guessing. Mutates item dicts by
    setting ``item_type`` when a new classification is found.

    Args:
        items: Statement item payloads (mutated in place).
        rows_by_header: Rows keyed by display header.
        item_number_header: Header containing the item/invoice number.
        header_mapping: Statement header-to-field mapping.
        matched_invoice_to_statement_item: Matched Xero invoice map.
        matched_numbers: Set of invoice numbers with existing matches.
        match_by_item_id: Pre-built item-id-to-type map.
        payment_number_map: Invoice-number-to-payments map.
        statement_id: Statement identifier for logging.

    Returns:
        Tuple of (item_types list, classification_updates dict).
    """
    classification_updates: dict[str, str] = {}
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        statement_item_id = it.get("statement_item_id")
        raw = it.get("raw", {}) if isinstance(it.get("raw"), dict) else {}
        current_type = str(it.get("item_type") or "").strip().lower()
        row_number = ""
        if item_number_header and idx < len(rows_by_header):
            row_number = str(rows_by_header[idx].get(item_number_header) or "").strip()

        new_type: str | None
        source: str | None
        new_type = None
        source = None

        if statement_item_id and statement_item_id in match_by_item_id:
            entry = match_by_item_id[statement_item_id]
            new_type = entry["type"]
            source = entry["source"]
        elif row_number and row_number in matched_numbers:
            match = matched_invoice_to_statement_item.get(row_number)
            doc = match.get("invoice") if isinstance(match, dict) else None
            if isinstance(doc, dict):
                doc_type = str(doc.get("type") or "").upper()
                if doc.get("credit_note_id") or doc_type.endswith("CREDIT"):
                    new_type = "credit_note"
                    source = "credit_note_match"
                else:
                    new_type = "invoice"
                    source = "invoice_match"
        elif row_number and row_number not in matched_numbers and row_number in payment_number_map:
            new_type = "payment"
            source = "payment_match"

        if not new_type:
            new_type = guess_statement_item_type(raw_row=raw, total_entries=it.get("total"), header_mapping=header_mapping)
            source = "heuristic"

        if new_type and new_type != current_type:
            it["item_type"] = new_type
            if statement_item_id:
                classification_updates[statement_item_id] = new_type
            logger.info("Statement item type updated", statement_id=statement_id, statement_item_id=statement_item_id, new_type=new_type, previous_type=current_type or "", source=source)

    item_types = [str((it.get("item_type") if isinstance(it, dict) else "") or "").strip().lower() for it in items]
    return item_types, classification_updates


def persist_classification_updates(*, data: dict[str, Any], statement_id: str, tenant_id: str, json_statement_key: str, classification_updates: dict[str, str]) -> None:
    """Persist updated item types back to S3 and DynamoDB.

    Only writes when there are actual classification changes. Uploads
    the full statement JSON to S3 and writes individual item type
    updates to DynamoDB.

    Args:
        data: Full statement JSON data (re-serialised to S3).
        statement_id: Statement identifier.
        tenant_id: Active tenant identifier.
        json_statement_key: S3 key for the statement JSON.
        classification_updates: Map of statement_item_id to new item type.
    """
    if not classification_updates:
        return

    try:
        json_payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        upload_statement_to_s3(BytesIO(json_payload), json_statement_key)
        logger.info("Persisted statement item types to S3", statement_id=statement_id, updated=len(classification_updates))
    except Exception as exc:
        logger.exception("Failed to persist statement JSON", statement_id=statement_id, error=str(exc))

    persist_item_types_to_dynamo(tenant_id, classification_updates)
    logger.info("Persisted statement item types to DynamoDB", statement_id=statement_id, updated=len(classification_updates))


def build_row_matches(rows_by_header: StatementRowsByHeader, item_number_header: str | None, matched_invoice_to_statement_item: MatchedInvoiceMap, row_comparisons: list[list[Any]]) -> list[bool]:
    """Return the per-row match status for colouring and export.

    Uses invoice number matching when available, otherwise falls back
    to strict all-cells comparison.

    Args:
        rows_by_header: Statement rows keyed by header.
        item_number_header: Header containing the item/invoice number.
        matched_invoice_to_statement_item: Matched Xero invoice map.
        row_comparisons: Per-cell comparison results.

    Returns:
        List of booleans indicating whether each row is matched.
    """
    if item_number_header:
        row_matches: list[bool] = []
        for r in rows_by_header:
            num = (r.get(item_number_header) or "").strip()
            row_matches.append(bool(num and matched_invoice_to_statement_item.get(num)))
        return row_matches

    # Fallback: if no number mapping, use strict all-cells match
    return [all(cell.matches for cell in row) for row in row_comparisons]


def _item_status(item: StatementItemPayload, item_status_map: dict[str, bool]) -> tuple[str | None, bool]:
    """Return the statement item ID and completion status.

    Args:
        item: Statement item payload.
        item_status_map: Map of statement_item_id to completion flag.

    Returns:
        Tuple of (statement_item_id or None, is_completed).
    """
    statement_item_id = item.get("statement_item_id") if isinstance(item, dict) else None
    if statement_item_id:
        return statement_item_id, item_status_map.get(statement_item_id, False)
    return None, False


def _item_flags(item: StatementItemPayload) -> list[str]:
    """Return normalised, unique flags for a statement item.

    Filters out non-string, blank, and duplicate flags.

    Args:
        item: Statement item payload.

    Returns:
        Ordered list of unique, stripped flag strings.
    """
    if not isinstance(item, dict):
        return []
    raw_flags = item.get("_flags") or []
    if not isinstance(raw_flags, list):
        return []
    seen_flags: set[str] = set()
    flags: list[str] = []
    for flag in raw_flags:
        if not isinstance(flag, str):
            continue
        normalized = flag.strip()
        if not normalized or normalized in seen_flags:
            continue
        seen_flags.add(normalized)
        flags.append(normalized)
    return flags


def build_statement_rows(
    *,
    rows_by_header: StatementRowsByHeader,
    row_comparisons: list[list[Any]],
    row_matches: list[bool],
    items: list[StatementItemPayload],
    item_types: list[str],
    item_status_map: dict[str, bool],
    item_number_header: str | None,
    matched_invoice_to_statement_item: MatchedInvoiceMap,
) -> list[StatementRowViewModel]:
    """Build the rows displayed in the statement detail UI.

    Assembles each row's comparison data, match status, item flags,
    Xero link IDs, and type label into the view model consumed by
    the template.

    Args:
        rows_by_header: Statement rows keyed by header names.
        row_comparisons: Per-cell comparison results.
        row_matches: Per-row match flags.
        items: Statement item payloads.
        item_types: Item type labels per row.
        item_status_map: Statement item completion flags.
        item_number_header: Header used to map Xero links.
        matched_invoice_to_statement_item: Matched Xero invoice map.

    Returns:
        List of row dicts for the statement detail table.
    """
    statement_rows: list[StatementRowViewModel] = []
    for idx, left_row in enumerate(rows_by_header):
        item = items[idx] if idx < len(items) else {}
        statement_item_id, is_item_completed = _item_status(item, item_status_map)

        flags = _item_flags(item)

        # Build Xero links by extracting IDs from matched data
        xero_invoice_id, xero_credit_note_id = xero_ids_for_row(item_number_header, left_row, matched_invoice_to_statement_item)

        item_type = (item.get("item_type") if isinstance(item, dict) else None) or (item_types[idx] if idx < len(item_types) else "invoice")
        statement_rows.append(
            {
                "statement_item_id": statement_item_id,
                "cell_comparisons": row_comparisons[idx] if idx < len(row_comparisons) else [],
                "matches": row_matches[idx] if idx < len(row_matches) else False,
                "is_completed": is_item_completed,
                "flags": flags,
                "item_type": item_type,
                "item_type_label": format_item_type_label(item_type),
                "xero_invoice_id": xero_invoice_id,
                "xero_credit_note_id": xero_credit_note_id,
            }
        )

    return statement_rows


def build_statement_excel_response(export_req: ExcelExportRequest) -> Response:
    """Build an XLSX export response for the current statement view.

    Delegates to the Excel payload builder and wraps the result in a
    Flask response with the correct content type and download filename.

    Args:
        export_req: Structured export request containing all pipeline
            data needed for the Excel export.

    Returns:
        Flask response containing the XLSX export.
    """
    from utils.statement_excel_export import build_statement_excel_payload  # pylint: disable=import-outside-toplevel

    excel_payload, download_name, row_count = build_statement_excel_payload(
        display_headers=export_req.display_headers,
        rows_by_header=export_req.rows_by_header,
        right_rows_by_header=export_req.right_rows_by_header,
        row_comparisons=export_req.row_comparisons,
        row_matches=export_req.row_matches,
        item_types=export_req.item_types,
        items=export_req.items,
        item_number_header=export_req.item_number_header,
        matched_invoice_to_statement_item=export_req.matched_invoice_to_statement_item,
        item_status_map=export_req.item_status_map,
        record=export_req.record,
        statement_id=export_req.statement_id,
    )
    response = current_app.response_class(excel_payload, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    logger.info("Statement Excel generated", tenant_id=export_req.tenant_id, statement_id=export_req.statement_id, rows=row_count, excel_filename=download_name)
    return response


def build_statement_view_data(*, tenant_id: str, statement_id: str, contact_id: str | None, data: dict[str, Any], record: dict[str, Any]) -> StatementViewContext | Response:
    """Run the full statement build pipeline and return cacheable view data.

    This is always the uncached path -- the caller is responsible for checking
    the Redis cache first and for handling early-exit states (processing,
    failed) before calling this function.

    Args:
        tenant_id: Active tenant.
        statement_id: Statement being viewed.
        contact_id: Xero contact linked to this statement (may be None).
        data: Parsed statement JSON from S3 (the ``fetch_json_statement`` result).
        record: DynamoDB statement record (used for Excel downloads).

    Returns:
        Dict with ``statement_rows`` and ``display_headers`` on the normal
        path.  Returns a Flask ``Response`` for ``?download=xlsx`` requests
        (the caller must handle this -- see the xlsx guard in the route).
    """
    # 1) Parse display configuration and left-side rows.
    items: list[StatementItemPayload] = data.get("statement_items", []) or []
    display_headers, rows_by_header, header_to_field, item_number_header = prepare_display_mappings(items, statement_data=data)

    # 2) Fetch Xero documents and classify each statement item.
    xero_data = get_xero_data_by_contact(contact_id, tenant_id=tenant_id)
    invoices: list[XeroDocumentPayload] = xero_data["invoices"]
    credit_notes: list[XeroDocumentPayload] = xero_data["credit_notes"]
    payments: list[XeroDocumentPayload] = xero_data["payments"]
    logger.info("Fetched Xero documents", statement_id=statement_id, contact_id=contact_id, invoices=len(invoices), credit_notes=len(credit_notes), payments=len(payments))

    docs_for_matching = invoices + credit_notes
    matched_invoice_to_statement_item: MatchedInvoiceMap = match_invoices_to_statement_items(
        items=items, rows_by_header=rows_by_header, item_number_header=item_number_header, invoices=docs_for_matching
    )

    matched_numbers: set[str] = {key for key in matched_invoice_to_statement_item if isinstance(key, str)}
    match_by_item_id_map = build_match_by_item_id(matched_invoice_to_statement_item)
    payment_number_map = build_payment_number_map(invoices, payments)

    item_types, classification_updates = classify_statement_items(
        items=items,
        rows_by_header=rows_by_header,
        item_number_header=item_number_header,
        header_mapping=data.get("header_mapping", {}),
        matched_invoice_to_statement_item=matched_invoice_to_statement_item,
        matched_numbers=matched_numbers,
        match_by_item_id=match_by_item_id_map,
        payment_number_map=payment_number_map,
        statement_id=statement_id,
    )

    persist_classification_updates(
        data=data, statement_id=statement_id, tenant_id=tenant_id, json_statement_key=statement_json_s3_key(tenant_id, statement_id), classification_updates=classification_updates
    )

    # 3) Build right-hand rows from matched invoices.
    right_rows_by_header = build_right_rows(
        rows_by_header=rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
        matched_map=matched_invoice_to_statement_item,
        item_number_header=item_number_header,
        date_format=data.get("date_format"),
    )

    # 4) Compare LEFT (statement) vs RIGHT (Xero) for per-cell indicators.
    row_comparisons = build_row_comparisons(left_rows=rows_by_header, right_rows=right_rows_by_header, display_headers=display_headers, header_to_field=header_to_field)
    row_matches = build_row_matches(rows_by_header, item_number_header, matched_invoice_to_statement_item, row_comparisons)

    item_status_map = get_statement_item_status_map(tenant_id, statement_id)

    # Excel downloads need intermediate pipeline data (rows_by_header,
    # right_rows_by_header, etc.) that is not stored in the cached dict.
    # The caller bypasses the cache for xlsx requests and handles this
    # response directly.
    if request.args.get("download") == "xlsx":
        export_req = ExcelExportRequest(
            display_headers=display_headers,
            rows_by_header=rows_by_header,
            right_rows_by_header=right_rows_by_header,
            row_comparisons=row_comparisons,
            row_matches=row_matches,
            item_types=item_types,
            items=items,
            item_number_header=item_number_header,
            matched_invoice_to_statement_item=matched_invoice_to_statement_item,
            item_status_map=item_status_map,
            record=record,
            statement_id=statement_id,
            tenant_id=tenant_id,
        )
        return build_statement_excel_response(export_req)

    statement_rows = build_statement_rows(
        rows_by_header=rows_by_header,
        row_comparisons=row_comparisons,
        row_matches=row_matches,
        items=items,
        item_types=item_types,
        item_status_map=item_status_map,
        item_number_header=item_number_header,
        matched_invoice_to_statement_item=matched_invoice_to_statement_item,
    )

    return {"statement_rows": statement_rows, "display_headers": display_headers}
