"""Statement routes -- list, detail, upload, deletion, and count.

Handles the statement list view with sorting/pagination, the individual
statement detail/reconciliation view, statement uploads, and deletion.
"""

from datetime import UTC, date, datetime
from typing import Any

from flask import Blueprint, abort, make_response, redirect, render_template, request, session, url_for
from sp_common.enums import TokenReservationStatus

from config import S3_BUCKET_NAME
from logger import logger
from statement_view_cache import cache_statement_view, get_cached_statement_view, invalidate_statement_view_cache
from tenant_billing_repository import TenantBillingRepository
from utils.auth import active_tenant_required, block_when_loading, route_handler_logging, xero_token_required
from utils.dynamo import (
    delete_statement_data,
    get_completed_statements,
    get_incomplete_statements,
    get_statement_record,
    mark_statement_completed,
    repair_processing_stage,
    set_all_statement_items_completed,
    set_statement_item_completed,
)
from utils.pagination import paginate
from utils.statement_detail import build_statement_view_data
from utils.statement_upload import get_active_contacts_for_upload, handle_upload_statements_post
from utils.storage import StatementJSONNotFoundError, fetch_json_statement, statement_json_s3_key

statements_bp = Blueprint("statements", __name__)

STATEMENT_ITEMS_PER_PAGE = 50


def _is_htmx_request() -> bool:
    """Return True if the current request was made by HTMX.

    HTMX sets the ``HX-Request: true`` header on every request it initiates.
    """
    return request.headers.get("HX-Request") == "true"


@statements_bp.route("/statements")
@active_tenant_required("Please select a tenant to view statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def statements():
    """Render the statement list with filtering and sorting."""
    tenant_id = session.get("xero_tenant_id")

    # Read query params and normalize sort direction.
    view = request.args.get("view", "incomplete").lower()
    show_completed = view == "completed"
    statement_rows = get_completed_statements(tenant_id) if show_completed else get_incomplete_statements(tenant_id)
    sort_key = request.args.get("sort", "uploaded").lower()
    dir_param = (request.args.get("dir") or "").strip().lower()
    ALLOWED_DIR = {"asc", "desc"}
    default_dir_map = {"contact": "asc", "date_range": "desc", "uploaded": "desc"}
    if sort_key not in {"contact", "date_range", "uploaded"}:
        sort_key = "uploaded"
    current_dir = dir_param if dir_param in ALLOWED_DIR else default_dir_map.get(sort_key, "desc")
    reverse = current_dir == "desc"
    message = session.pop("statements_message", None)

    def _parse_iso_date(value: object) -> date | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            return None

    def _parse_iso_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        try:
            # Support both "+00:00" and trailing "Z"
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    # Add derived fields for display and sorting.
    for row in statement_rows:
        earliest = _parse_iso_date(row.get("EarliestItemDate"))
        latest = _parse_iso_date(row.get("LatestItemDate"))
        row["_earliest_item_date"] = earliest
        row["_latest_item_date"] = latest
        row["_uploaded_at"] = _parse_iso_datetime(row.get("UploadedAt"))
        if earliest and latest:
            row["ItemDateRangeDisplay"] = earliest.isoformat() if earliest == latest else f"{earliest.isoformat()} - {latest.isoformat()}"
        elif latest:
            row["ItemDateRangeDisplay"] = latest.isoformat()
        elif earliest:
            row["ItemDateRangeDisplay"] = earliest.isoformat()
        else:
            row["ItemDateRangeDisplay"] = "\u2014"

    if sort_key == "date_range":
        statement_rows.sort(key=lambda r: r.get("_latest_item_date") or date.min, reverse=reverse)
    elif sort_key == "uploaded":
        statement_rows.sort(key=lambda r: r.get("_uploaded_at") or datetime.min.replace(tzinfo=UTC), reverse=reverse)
    else:
        # Contact: alphabetical or reverse, always keep missing/blank names last.
        sort_key = "contact"

        def _has_contact(row: dict) -> bool:
            name = row.get("ContactName")
            return isinstance(name, str) and bool(name.strip())

        nonempty = [r for r in statement_rows if _has_contact(r)]
        empty = [r for r in statement_rows if not _has_contact(r)]
        nonempty.sort(key=lambda r: str(r.get("ContactName")).strip().casefold(), reverse=reverse)
        statement_rows = nonempty + empty

    # Pagination: slice sorted rows to the current page.
    STATEMENTS_PER_PAGE_OPTIONS = [25, 50, 100]
    raw_page = request.args.get("page", "1")
    raw_per_page = request.args.get("per_page", "25")
    try:
        req_page = int(raw_page)
    except (ValueError, TypeError):
        req_page = 1
    try:
        req_per_page = int(raw_per_page)
    except (ValueError, TypeError):
        req_per_page = 25

    pagination = paginate(total_items=len(statement_rows), page=req_page, per_page=req_per_page, per_page_options=STATEMENTS_PER_PAGE_OPTIONS)

    # Remove helper fields before rendering.
    for row in statement_rows:
        row.pop("_earliest_item_date", None)
        row.pop("_latest_item_date", None)
        row.pop("_uploaded_at", None)

    # Total count before slicing for the item count chip.
    statement_count = len(statement_rows)
    statement_rows = statement_rows[pagination.start_index : pagination.end_index]

    # Preserve filters and pagination when building sort URLs.
    # Page is intentionally omitted -- sort changes reset to page 1.
    base_args: dict[str, Any] = {"per_page": pagination.per_page}
    if show_completed:
        base_args["view"] = "completed"

    # For each sort key, clicking its button toggles the direction if already active,
    # otherwise applies the default direction for that key.
    def next_dir_for(key: str) -> str:
        if key == sort_key:
            return "asc" if current_dir == "desc" else "desc"
        return default_dir_map.get(key, "desc")

    sort_links = {
        "contact": url_for("statements.statements", **dict(base_args, sort="contact", dir=next_dir_for("contact"))),
        "date_range": url_for("statements.statements", **dict(base_args, sort="date_range", dir=next_dir_for("date_range"))),
        "uploaded": url_for("statements.statements", **dict(base_args, sort="uploaded", dir=next_dir_for("uploaded"))),
    }

    logger.info(
        "Rendering statements",
        tenant_id=tenant_id,
        view=view,
        sort=sort_key,
        direction=current_dir,
        page=pagination.page,
        per_page=pagination.per_page,
        total_pages=pagination.total_pages,
        statements=len(statement_rows),
    )

    # HTMX requests receive only the content partial; normal requests get the full page.
    template = "partials/statements_content.html" if _is_htmx_request() else "statements.html"
    return render_template(
        template,
        statements=statement_rows,
        show_completed=show_completed,
        message=message,
        current_sort=sort_key,
        current_dir=current_dir,
        sort_links=sort_links,
        page=pagination.page,
        per_page=pagination.per_page,
        total_pages=pagination.total_pages,
        statement_count=statement_count,
    )


@statements_bp.route("/statements/count")
@active_tenant_required("Please select a tenant.")
@xero_token_required
@route_handler_logging
def statements_count():
    """Return the current statement count as an HTML fragment for HTMX count refresh.

    Reads the same view param as the statements list so the count reflects the
    currently visible tab (incomplete vs. completed).  Returns two out-of-band
    span elements that HTMX can swap into the page without a full reload.
    """
    tenant_id = session.get("xero_tenant_id")
    view = request.args.get("view", "incomplete").lower()
    show_completed = view == "completed"
    rows = get_completed_statements(tenant_id) if show_completed else get_incomplete_statements(tenant_id)
    count = len(rows)
    label = f"{count} statement{'s' if count != 1 else ''}"
    # Two OOB spans: one for the main action bar, one for the sticky dock.
    return (
        f'<span class="action-count-chip" id="statements-count-chip" hx-swap-oob="true">{label}</span>\n'
        f'<span class="action-count-chip" id="statements-count-chip-sticky" hx-swap-oob="true">{label}</span>'
    )


@statements_bp.route("/statement/<statement_id>/delete", methods=["POST"])
@active_tenant_required("Please select a tenant before deleting statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def delete_statement(statement_id: str):
    """Delete the statement and redirect back to the list view.

    Pagination and sort state (page, per_page, sort, dir, view) are forwarded
    via query params on the form action URL so the user lands back on the same
    page they were on rather than the default first page.
    """
    tenant_id = session.get("xero_tenant_id")

    # Preserve the caller's pagination and sort state for the redirect.
    redirect_args: dict[str, str] = {}
    for param in ("page", "per_page", "sort", "dir", "view"):
        val = request.args.get(param)
        if val:
            redirect_args[param] = val

    record = get_statement_record(tenant_id, statement_id)
    if record and str(record.get("TokenReservationStatus") or "").strip().lower() == TokenReservationStatus.RESERVED:
        logger.info("Delete rejected; statement still processing", tenant_id=tenant_id, statement_id=statement_id)
        session["tenant_error"] = "This statement is still processing and cannot be deleted yet."
        return redirect(url_for("statements.statements", **redirect_args))

    try:
        delete_statement_data(tenant_id, statement_id)
        session["statements_message"] = "Statement deleted."
    except Exception as exc:
        logger.exception("Failed to delete statement", tenant_id=tenant_id, statement_id=statement_id, error=exc)
        session["tenant_error"] = "Unable to delete the statement. Please try again."

    # HTMX deletions: return an empty 200 with a trigger so the client can
    # refresh the list without a full page reload.
    if _is_htmx_request():
        response = make_response("", 200)
        response.headers["HX-Trigger"] = "listUpdated"
        return response
    return redirect(url_for("statements.statements", **redirect_args))


def _parse_items_view(raw_value: str | None) -> str:
    """Normalize the statement item filter."""
    items_view = (raw_value or "incomplete").strip().lower()
    if items_view not in {"incomplete", "completed", "all"}:
        return "incomplete"
    return items_view


def _parse_show_payments(raw_value: str | None) -> bool:
    """Normalize the show payments flag."""
    value = (raw_value or "true").strip().lower()
    return value in {"true", "1", "yes", "on"}


def _handle_statement_post_actions(*, tenant_id: str, statement_id: str, form: Any, items_view: str, show_payments: bool, page: int) -> Any:
    """Handle POST actions for statement detail views, returning a redirect when applicable."""
    action = form.get("action")
    if action in {"mark_complete", "mark_incomplete"}:
        completed_flag = action == "mark_complete"
        try:
            mark_statement_completed(tenant_id, statement_id, completed_flag)
            try:
                set_all_statement_items_completed(tenant_id, statement_id, completed_flag)
            except Exception as exc:
                logger.exception("Failed to toggle all statement items", statement_id=statement_id, tenant_id=tenant_id, desired_state=completed_flag, error=exc)

            session["statements_message"] = "Statement marked as complete." if completed_flag else "Statement marked as incomplete."
            logger.info("Statement completion updated", tenant_id=tenant_id, statement_id=statement_id, completed=completed_flag)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Failed to toggle statement completion", statement_id=statement_id, tenant_id=tenant_id, desired_state=completed_flag, error=exc)
            abort(500)
        return redirect(url_for("statements.statements"))

    if action in {"complete_item", "incomplete_item"}:
        statement_item_id = (form.get("statement_item_id") or "").strip()
        if statement_item_id:
            desired_state = action == "complete_item"
            try:
                set_statement_item_completed(tenant_id, statement_item_id, desired_state)
                logger.info("Statement item updated", tenant_id=tenant_id, statement_id=statement_id, statement_item_id=statement_item_id, completed=desired_state)
            except Exception as exc:
                logger.exception(
                    "Failed to toggle statement item completion", statement_id=statement_id, statement_item_id=statement_item_id, tenant_id=tenant_id, desired_state=desired_state, error=exc
                )
        # For HTMX requests, fall through to re-render the partial with updated data
        # so the browser can swap the content without a full redirect/reload.
        if _is_htmx_request():
            return None
        return redirect(url_for("statements.statement", statement_id=statement_id, items_view=items_view, show_payments="true" if show_payments else "false", page=page))

    return None


@statements_bp.route("/statement/<statement_id>", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant to view statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def statement(statement_id: str):
    """Render the statement detail view, handling actions and exports."""
    tenant_id = session.get("xero_tenant_id")

    record = get_statement_record(tenant_id, statement_id)
    if not record:
        logger.info("Statement record not found", tenant_id=tenant_id, statement_id=statement_id)
        abort(404)

    items_view = _parse_items_view(request.values.get("items_view"))
    show_payments = _parse_show_payments(request.values.get("show_payments"))

    # Parse page from request.values so it works for both GET params and POST form data.
    # This must happen before the POST check so the value is available in both branches.
    raw_page_param = request.values.get("page", "1")
    try:
        page_param = int(raw_page_param)
    except (ValueError, TypeError):
        page_param = 1

    logger.info("Statement detail requested", tenant_id=tenant_id, statement_id=statement_id, items_view=items_view, show_payments=show_payments, method=request.method)

    raw_contact_name = record.get("ContactName")
    contact_name = str(raw_contact_name).strip() if raw_contact_name is not None else ""
    page_heading = contact_name or f"Statement {statement_id}"  # TODO: Could page heading include statement filename instead of statement_id? StatementID is useless for customer

    if request.method == "POST":
        # Invalidate cached view data so the fall-through re-render picks up
        # the new item status from DynamoDB.
        invalidate_statement_view_cache(tenant_id, statement_id)
        response = _handle_statement_post_actions(tenant_id=tenant_id, statement_id=statement_id, form=request.form, items_view=items_view, show_payments=show_payments, page=page_param)
        if response is not None:
            return response

    contact_id = record.get("ContactID")
    is_completed = str(record.get("Completed", "")).lower() == "true"
    base_context: dict[str, Any] = {
        "statement_id": statement_id,
        "contact_name": contact_name,
        "page_heading": page_heading,
        "items_view": items_view,
        "show_payments": show_payments,
        "is_completed": is_completed,
    }

    # --- Early-exit: statement not yet ready (processing or failed). ---
    # Check this before the cache/pipeline so processing states are never cached.
    json_statement_key = statement_json_s3_key(tenant_id, statement_id)
    try:
        statement_json_data = fetch_json_statement(tenant_id=tenant_id, bucket=S3_BUCKET_NAME, json_key=json_statement_key)
    except StatementJSONNotFoundError:
        reservation_status = str(record.get("TokenReservationStatus") or "").strip().lower()
        empty_context = {**base_context, "incomplete_count": 0, "completed_count": 0, "all_statement_rows": [], "statement_rows": [], "raw_statement_headers": [], "has_payment_rows": False}
        if reservation_status == TokenReservationStatus.RELEASED:
            logger.info("Statement processing failed; JSON missing after release", tenant_id=tenant_id, statement_id=statement_id, json_key=json_statement_key)
            repair_processing_stage(tenant_id, statement_id)
            return render_template("statement.html", is_processing=False, processing_failed=True, **empty_context)
        logger.info("Statement JSON pending", tenant_id=tenant_id, statement_id=statement_id, json_key=json_statement_key)
        return render_template(
            "statement.html",
            is_processing=True,
            processing_failed=False,
            processing_stage=str(record.get("ProcessingStage") or "").strip().lower(),
            processing_progress=record.get("ProcessingProgress"),
            processing_total_sections=record.get("ProcessingTotalSections"),
            **empty_context,
        )

    # --- Excel download: bypass cache, needs full pipeline data. ---
    if request.args.get("download") == "xlsx":
        result = build_statement_view_data(tenant_id=tenant_id, statement_id=statement_id, contact_id=contact_id, data=statement_json_data, record=record)
        # build_statement_view_data returns a Response for xlsx requests.
        return result

    # --- Normal path: check Redis cache, fall back to full pipeline. ---
    cached_view = get_cached_statement_view(tenant_id, statement_id)

    if cached_view is not None:
        # Cache hit: skip the full pipeline, go straight to filtering.
        statement_rows = cached_view["statement_rows"]
        display_headers = cached_view["display_headers"]
    else:
        # Cache miss: run the full build pipeline.
        result = build_statement_view_data(tenant_id=tenant_id, statement_id=statement_id, contact_id=contact_id, data=statement_json_data, record=record)
        statement_rows = result["statement_rows"]
        display_headers = result["display_headers"]
        cache_statement_view(tenant_id, statement_id, result)

    completed_count = sum(1 for row in statement_rows if row["is_completed"])
    incomplete_count = len(statement_rows) - completed_count
    has_payment_rows = any(row.get("item_type") == "payment" for row in statement_rows)

    if items_view == "completed":
        visible_rows = [row for row in statement_rows if row["is_completed"]]
    elif items_view == "incomplete":
        visible_rows = [row for row in statement_rows if not row["is_completed"]]
    else:
        visible_rows = statement_rows

    if not show_payments:
        visible_rows = [row for row in visible_rows if row.get("item_type") != "payment"]

    # Pagination: slice filtered rows to the current page.
    total_visible_count = len(visible_rows)
    pagination = paginate(total_items=total_visible_count, page=page_param, per_page=STATEMENT_ITEMS_PER_PAGE)
    visible_rows = visible_rows[pagination.start_index : pagination.end_index]

    logger.info(
        "Statement detail rendered",
        tenant_id=tenant_id,
        statement_id=statement_id,
        visible=len(visible_rows),
        total=len(statement_rows),
        completed=completed_count,
        incomplete=incomplete_count,
        items_view=items_view,
        show_payments=show_payments,
    )

    context: dict[str, Any] = {
        **base_context,
        "is_processing": False,
        "processing_failed": False,
        "raw_statement_headers": display_headers,
        "statement_rows": visible_rows,
        "all_statement_rows": statement_rows,
        "completed_count": completed_count,
        "incomplete_count": incomplete_count,
        "has_payment_rows": has_payment_rows,
        "page": pagination.page,
        "total_pages": pagination.total_pages,
        "total_visible_count": total_visible_count,
    }
    # Return the content partial for HTMX requests so the browser swaps only
    # the #statement-content region without a full page reload.
    template = "partials/statement_content.html" if _is_htmx_request() else "statement.html"
    return render_template(template, **context)


@statements_bp.route("/upload-statements", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant before uploading statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def upload_statements():
    """Upload one or more PDF statements and register them for processing."""
    tenant_id = session.get("xero_tenant_id")

    contacts_list, contact_lookup = get_active_contacts_for_upload()
    available_pages = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    success_count: int | None = None
    error_messages: list[str] = []
    logger.info("Rendering upload statements", tenant_id=tenant_id, available_contacts=len(contacts_list), available_pages=available_pages)

    if request.method == "POST":
        uploads_ok = handle_upload_statements_post(tenant_id, contact_lookup=contact_lookup, error_messages=error_messages)

        if uploads_ok:
            success_count = uploads_ok
        logger.info("Upload statements processed", tenant_id=tenant_id, succeeded=uploads_ok, errors=list(error_messages))

    return render_template("upload_statements.html", contacts=contacts_list, success_count=success_count, error_messages=error_messages, available_pages=available_pages)
