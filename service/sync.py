"""
Sync Xero datasets to local cache and S3.

This module:
- fetches contacts, invoices, payments, and credit notes from Xero
- merges incremental results with cached data
- writes datasets locally and uploads them to S3
- builds per-contact index files for fast statement page lookups
- updates tenant sync status in DynamoDB
"""

import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from xero_python.accounting import AccountingApi

from billing_service import LAST_MUTATION_SOURCE_WELCOME_GRANT, WELCOME_GRANT_TOKENS, BillingService
from config import LOCAL_DATA_DIR, S3_BUCKET_NAME, s3_client, tenant_data_table
from logger import logger
from statement_view_cache import bump_tenant_generation
from tenant_data_repository import ALL_SYNC_RESOURCES, SYNC_STALE_THRESHOLD_MS, ProgressStatus, TenantDataRepository, TenantStatus, _progress_attribute_name
from utils.auth import get_xero_api_client
from xero_repository import CONTACT_DOC_TYPES, XeroType, get_contacts_from_xero, get_credit_notes, get_invoices, get_payments


def _sync_resource(api: AccountingApi, tenant_id: str, fetcher: Callable[..., Any], resource: XeroType, start_message: str, done_message: str, modified_since: datetime | None = None) -> bool:
    """Fetch, cache, and upload a single Xero dataset.

    Writes per-resource sync progress to DynamoDB around the work:

    - ``in_progress`` with ``records_fetched=0, record_total=None`` at start.
    - ``in_progress`` with live counts after each fetcher page (via callback).
    - ``complete`` on success, ``failed`` on exception.

    Progress is best-effort telemetry; a write failure is logged but does not
    fail the sync. Otherwise a DynamoDB blip during a 60-minute sync could
    leave a user permanently stuck on the not-ready page.
    """
    if not tenant_id:
        logger.error("Missing TenantID")
        return False

    logger.info(start_message, tenant_id=tenant_id)

    # Initial in_progress write so the UI renders a zeroed bar before the
    # first Xero page lands (Xero calls can take 10+ seconds each).
    last_counts: dict[str, int | None] = {"records_fetched": 0, "record_total": None}
    _safe_update_progress(tenant_id, resource, ProgressStatus.IN_PROGRESS, records_fetched=0, record_total=None)

    resource_filename = f"{resource}.json"

    try:
        local_dir = os.path.join(LOCAL_DATA_DIR, tenant_id)
        local_path = os.path.join(local_dir, resource_filename)
        s3_key = f"{tenant_id}/data/{resource_filename}"

        def _on_page(records_fetched: int, record_total: int | None) -> None:
            last_counts["records_fetched"] = records_fetched
            last_counts["record_total"] = record_total
            _safe_update_progress(tenant_id, resource, ProgressStatus.IN_PROGRESS, records_fetched=records_fetched, record_total=record_total)

        # Fetch the latest dataset from Xero. Fetchers added progress_callback
        # support in Step 3; legacy tests without that kwarg still work because
        # it's optional on every fetcher.
        data = fetcher(tenant_id, api=api, modified_since=modified_since, progress_callback=_on_page)

        existing_payload = None
        if os.path.exists(local_path):
            try:
                with open(local_path, encoding="utf-8") as existing_file:
                    existing_payload = json.load(existing_file)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("Failed to load existing dataset", tenant_id=tenant_id, resource=resource, error=str(exc))

        # Merge incremental results with any cached data so we retain the full dataset.
        payload = _merge_resource_payload(resource, existing_payload, data) if modified_since else data if data is not None else existing_payload

        if payload is None:
            payload = []

        os.makedirs(local_dir, exist_ok=True)

        with open(local_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4, ensure_ascii=False, default=str)

        s3_client.upload_file(local_path, S3_BUCKET_NAME, s3_key)

        record_count = len(payload) if isinstance(payload, (list, dict)) else None
        logger.info(done_message, tenant_id=tenant_id, records=record_count)

        # Record counts on the final write: prefer the actual payload size
        # because that's the authoritative post-merge total.
        final_records_fetched = record_count if record_count is not None else last_counts["records_fetched"]
        _safe_update_progress(tenant_id, resource, ProgressStatus.COMPLETE, records_fetched=final_records_fetched, record_total=last_counts["record_total"])
        return True

    except Exception:
        logger.exception("Unexpected error syncing resource", tenant_id=tenant_id, resource=resource_filename)
        _safe_update_progress(tenant_id, resource, ProgressStatus.FAILED, records_fetched=last_counts["records_fetched"], record_total=last_counts["record_total"])
        return False


def _safe_update_progress(tenant_id: str, resource: str, status: str, records_fetched: int | None = None, record_total: int | None = None) -> None:
    """Write per-resource progress without letting DDB blips fail the sync.

    Progress writes are telemetry, not a contract — a failed write at most
    leaves the UI showing stale counts until the next heartbeat. Blocking the
    sync on a DynamoDB timeout would be a much worse UX.
    """
    try:
        TenantDataRepository.update_resource_progress(tenant_id, resource, status, records_fetched=records_fetched, record_total=record_total)
    except Exception:
        logger.exception("Failed to write sync progress", tenant_id=tenant_id, resource=resource, status=status)


def _resolve_modified_since(record: dict[str, Any] | None) -> datetime | None:  # pylint: disable=too-many-return-statements
    """Return LastSyncTime as a timezone-aware datetime if present."""
    if not record:
        return None

    raw_value = record.get("LastSyncTime")
    if raw_value is None:
        return None

    try:
        # Support raw epoch seconds/milliseconds or numeric strings.
        if isinstance(raw_value, (Decimal, int, float)):
            timestamp = float(raw_value)
        elif isinstance(raw_value, str) and raw_value.strip():
            timestamp = float(raw_value.strip())
        else:
            return None
    except (ValueError, TypeError):
        try:
            normalised = str(raw_value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalised)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    if timestamp > 1e11:
        timestamp /= 1000

    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _merge_resource_payload(resource: XeroType, existing: Any, delta: Any) -> Any:
    """
    Combine newly fetched records with any previously cached dataset.
    When we only pull a delta, this keeps the local/S3 files authoritative.
    """
    if delta is None or (isinstance(delta, (list, dict)) and not delta):  # Nothing changed
        return existing
    if existing is None:  # Only new data exists (initial load)
        return delta

    def _as_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)]
        return []

    key_fields = {XeroType.CONTACTS: "contact_id", XeroType.CREDIT_NOTES: "credit_note_id", XeroType.PAYMENTS: "payment_id", XeroType.INVOICES: "invoice_id"}
    key = key_fields.get(resource)
    if key is None:
        return delta

    existing_list = _as_list(existing)
    delta_list = _as_list(delta)

    merged: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for source in (existing_list, delta_list):
        for item in source:
            identifier = item.get(key)
            if identifier:
                merged[identifier] = item
            else:
                extras.append(item)

    combined = list(merged.values()) + extras

    sort_keys = {
        XeroType.CONTACTS: lambda c: (c.get("name") or "").casefold(),
        XeroType.CREDIT_NOTES: lambda note: note.get("credit_note_id") or "",
        XeroType.PAYMENTS: lambda payment: payment.get("payment_id") or "",
        XeroType.INVOICES: lambda inv: str(inv.get("number") or "").casefold(),
    }
    sort_key = sort_keys.get(resource)
    if sort_key:
        combined.sort(key=sort_key)

    return combined


def sync_contacts(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync contact data from Xero."""
    return _sync_resource(api, tenant_id, get_contacts_from_xero, XeroType.CONTACTS, "Syncing contacts", "Synced contacts", modified_since=modified_since)


def sync_credit_notes(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync credit note data from Xero."""
    return _sync_resource(api, tenant_id, get_credit_notes, XeroType.CREDIT_NOTES, "Syncing credit notes", "Synced credit notes", modified_since=modified_since)


def sync_invoices(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync invoice data from Xero."""
    return _sync_resource(api, tenant_id, get_invoices, XeroType.INVOICES, "Syncing invoices", "Synced invoices", modified_since=modified_since)


def sync_payments(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync payment data from Xero."""
    return _sync_resource(api, tenant_id, get_payments, XeroType.PAYMENTS, "Syncing payments", "Synced payments", modified_since=modified_since)


def _s3_data_exists(tenant_id: str) -> bool:
    """Check whether the tenant's core data files exist in S3.

    Uses a single head_object call on contacts.json as a canary. If this
    file is missing, the other datasets are likely missing too.
    """
    canary_key = f"{tenant_id}/data/contacts.json"
    try:
        s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=canary_key)
        return True
    except ClientError as exc:
        # head_object returns a 404 ClientError when the object is missing
        # (not NoSuchKey, which is specific to get_object).
        status_code = exc.response.get("Error", {}).get("Code", "")
        if status_code in ("404", "NoSuchKey"):
            return False
        logger.exception("S3 head_object failed, assuming data exists", tenant_id=tenant_id, key=canary_key)
        return True
    except Exception:
        # Non-AWS errors (network, etc.) — assume data exists to be safe.
        logger.exception("S3 head_object failed, assuming data exists", tenant_id=tenant_id, key=canary_key)
        return True


def check_load_required(tenant_id: str) -> bool:
    """Check whether a tenant needs a full data load on connection.

    Handles three cases:
    - New tenant (no DynamoDB record): seeds the row, grants welcome tokens.
    - Returning tenant (ERASED or LOAD_INCOMPLETE): resets to LOADING,
      cancels any pending erasure. No welcome tokens.
    - Existing tenant (any other status): no action. If a pending erasure
      exists, cancels it (the tenant reconnected before the Lambda ran).

    Returns True when a full LOADING sync should be triggered.
    """
    try:
        response = tenant_data_table.get_item(Key={"TenantID": tenant_id})
        item = response.get("Item")

        if not item:
            # Case 1: Brand-new tenant — seed record and grant welcome tokens.
            try:
                tenant_data_table.put_item(Item={"TenantID": tenant_id, "TenantStatus": TenantStatus.LOADING}, ConditionExpression="attribute_not_exists(TenantID)")
                logger.info("Seeded tenant record with LOADING status", tenant_id=tenant_id)

                try:
                    BillingService.adjust_token_balance(tenant_id, WELCOME_GRANT_TOKENS, source=LAST_MUTATION_SOURCE_WELCOME_GRANT, price_per_token_pence=0)
                    logger.info("Granted welcome tokens", tenant_id=tenant_id, token_count=WELCOME_GRANT_TOKENS)
                except Exception:
                    logger.exception("Failed to grant welcome tokens — login continues", tenant_id=tenant_id)

            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    logger.exception("Failed to seed tenant status for new tenant", tenant_id=tenant_id)

            logger.info("New tenant requires initial load", tenant_id=tenant_id)
            return True

        # Record exists — parse status directly to avoid coupling to TenantDataRepository
        # for a simple enum lookup (TenantDataRepository is mocked in tests for cancel_erasure).
        raw_status = item.get("TenantStatus", "")
        candidate = raw_status.strip().upper() if isinstance(raw_status, str) else str(raw_status)
        status: TenantStatus = next((s for s in TenantStatus if candidate == s), TenantStatus.FREE)
        has_pending_erasure = "EraseTenantDataTime" in item

        if status in (TenantStatus.ERASED, TenantStatus.LOAD_INCOMPLETE):
            # Case 2: Returning tenant — reset to LOADING and cancel any pending
            # erasure in a single atomic DynamoDB call to avoid a race window.
            update_expr = "SET TenantStatus = :loading"
            if has_pending_erasure:
                update_expr += " REMOVE EraseTenantDataTime"
            tenant_data_table.update_item(Key={"TenantID": tenant_id}, UpdateExpression=update_expr, ExpressionAttributeValues={":loading": TenantStatus.LOADING})
            logger.info("Returning tenant requires fresh load", tenant_id=tenant_id, previous_status=str(status))
            return True

        # Case 3: Normal reconnection (FREE, SYNCING, etc.) — no reload.
        if has_pending_erasure:
            TenantDataRepository.cancel_erasure(tenant_id)
            logger.info("Cancelled pending erasure for reconnecting tenant", tenant_id=tenant_id)

        # Verify S3 data actually exists. DynamoDB may say FREE but data could
        # be missing (e.g. manual deletion, partial sync on first load). Check a
        # canary file — if contacts.json is absent, trigger a full reload.
        if status == TenantStatus.FREE and not _s3_data_exists(tenant_id):
            tenant_data_table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="SET TenantStatus = :loading", ExpressionAttributeValues={":loading": TenantStatus.LOADING})
            logger.warning("S3 data missing for FREE tenant, triggering reload", tenant_id=tenant_id)
            return True

        logger.info("Checked tenant sync requirement", tenant_id=tenant_id, sync_required=False)
        return False

    except ClientError:
        logger.exception("DynamoDB get_item failed", tenant_id=tenant_id)
        return True


def update_tenant_status(tenant_id: str, tenant_status: TenantStatus = TenantStatus.FREE, last_sync_time: int | None = None) -> bool:
    """Persist the tenant's status in DynamoDB."""
    if not tenant_id:
        logger.error("Missing TenantID while marking sync state")
        return False

    try:
        update_expression = "SET TenantStatus = :tenant_status"
        expression_values = {":tenant_status": tenant_status}

        if last_sync_time is not None:
            update_expression += ", LastSyncTime = :last_sync_time"
            expression_values[":last_sync_time"] = last_sync_time

        tenant_data_table.update_item(Key={"TenantID": tenant_id}, UpdateExpression=update_expression, ExpressionAttributeValues=expression_values)
        logger.info("Updated tenant sync state", tenant_id=tenant_id, tenant_status=tenant_status, last_sync_time=last_sync_time)
        return True
    except ClientError:
        logger.exception("Failed to update tenant sync state", tenant_id=tenant_id)
        return False


def build_per_contact_index(tenant_id: str) -> None:
    """Group synced Xero data by contact_id and write per-contact files.

    Reads the flat dataset files (invoices.json, credit_notes.json,
    payments.json) from local disk, groups each by contact_id, and writes
    a combined {contact_id}.json into xero_by_contact/ — both locally
    and in S3. This allows the statement detail page to load only the
    data for one contact instead of the full tenant dataset.

    Called after each sync (both full and incremental). Always rebuilds
    all per-contact files from the current flat files — the cost is
    negligible (in-memory grouping + parallel S3 PUTs).

    S3 uploads are parallelised with a thread pool because large tenants
    can have 1,000+ contacts, and sequential uploads at ~50 ms each would
    block the sync for 50+ seconds.

    TODO: Consider writing an empty per-contact file for every contact in
    contacts.json (not just those with at least one invoice/credit-note/
    payment). Today, contacts with no transactions have no per-contact
    file, so every view of their statement detail misses the cache and
    pays the ~150 ms full-dataset fallback in xero_repository.
    get_contact_data. Trade-off: more S3 PUTs per sync (one per contact
    rather than one per active contact), and "file exists" loses its
    current implicit meaning of "contact has transactions". Edge cases to
    think through: new contacts added between syncs and stale
    references to deleted Xero contacts still need the silent-miss
    fallback in _load_per_contact_file as a safety net.
    """
    local_dir = os.path.join(LOCAL_DATA_DIR, tenant_id)

    def _load_flat(resource: XeroType) -> list[dict[str, Any]]:
        """Load a flat dataset file, deriving the filename from XeroType."""
        path = os.path.join(local_dir, f"{resource}.json")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return [item for item in data if isinstance(item, dict)]
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    # Map XeroType → CONTACT_DOC_TYPES key so both sides derive from
    # the same constants.
    resource_to_doc_key = {XeroType.INVOICES: "invoices", XeroType.CREDIT_NOTES: "credit_notes", XeroType.PAYMENTS: "payments"}

    datasets = {doc_key: _load_flat(resource) for resource, doc_key in resource_to_doc_key.items()}

    total_records = sum(len(docs) for docs in datasets.values())
    logger.info("Building per-contact index", tenant_id=tenant_id, total_records=total_records)

    # Group by contact_id.  Items without a contact_id are intentionally
    # skipped — statements are always contact-scoped, so orphaned items
    # can never be reached by the per-contact lookup on the detail page.
    by_contact: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for doc_key, docs in datasets.items():
        for doc in docs:
            cid = doc.get("contact_id")
            if cid:
                if cid not in by_contact:
                    by_contact[cid] = {k: [] for k in CONTACT_DOC_TYPES}
                by_contact[cid][doc_key].append(doc)

    if not by_contact:
        return

    contact_dir = os.path.join(local_dir, "xero_by_contact")
    os.makedirs(contact_dir, exist_ok=True)

    # Write all per-contact files locally first (fast, sequential I/O).
    upload_tasks: list[tuple[str, str, str]] = []
    for contact_id, contact_data in by_contact.items():
        filename = f"{contact_id}.json"
        local_path = os.path.join(contact_dir, filename)
        s3_key = f"{tenant_id}/data/xero_by_contact/{filename}"

        with open(local_path, "w", encoding="utf-8") as handle:
            json.dump(contact_data, handle, ensure_ascii=False, default=str)

        upload_tasks.append((local_path, s3_key, contact_id))

    # Upload to S3 in parallel.  10 workers keeps connection count
    # manageable on App Runner (shared vCPU) while still cutting a
    # 1,000-contact sync from ~50s sequential to ~5s.
    s3_upload_workers = 10
    upload_failures = 0

    def _upload_one(task: tuple[str, str, str]) -> bool:
        """Upload one per-contact file. Returns True on failure."""
        path, key, cid = task
        try:
            s3_client.upload_file(path, S3_BUCKET_NAME, key)
            return False
        except Exception:
            logger.exception("Failed to upload per-contact file to S3", tenant_id=tenant_id, contact_id=cid, s3_key=key)
            return True

    with ThreadPoolExecutor(max_workers=s3_upload_workers) as pool:
        futures = {pool.submit(_upload_one, task): task for task in upload_tasks}
        for future in as_completed(futures):
            if future.result():
                upload_failures += 1

    logger.info("Built per-contact index", tenant_id=tenant_id, contacts=len(by_contact), upload_failures=upload_failures)


def _resource_is_complete(tenant_item: dict[str, Any] | None, resource: str) -> bool:
    """Return True when the stored ``*Progress.status`` is ``complete``."""
    if not tenant_item:
        return False
    progress = tenant_item.get(_progress_attribute_name(resource))
    return isinstance(progress, dict) and progress.get("status") == ProgressStatus.COMPLETE


def _should_skip_for_retry(resource: str, only_run: set[str] | None, tenant_item: dict[str, Any] | None) -> bool:
    """Decide whether to skip a resource on a retry run.

    ``only_run=None`` is a full sync — never skip. Otherwise skip when the
    resource is outside the retry set, or when it's already ``complete``
    (race-safe: a user double-clicking Retry must not re-fetch a resource
    that finished while the second click was in flight).
    """
    if only_run is None:
        return False
    if resource not in only_run:
        return True
    return _resource_is_complete(tenant_item, resource)


def sync_data(tenant_id: str, operation_type: TenantStatus, oauth_token: dict[str, Any] | None = None, only_run_resources: set[str] | None = None, *, already_acquired: bool = False) -> None:
    """Sync all Xero datasets for a tenant and transition tenant status.

    Choreography (see contacts-first unlock plan for rationale):

    1. ``try_acquire_sync`` — atomically claim the tenant; return early if
       another sync is already in flight and its heartbeat is fresh. Callers
       that already acquired the lock (e.g. the retry-sync API endpoint that
       needs to return 409 on rejection) can skip this with
       ``already_acquired=True``.
    2. Contacts phase — once complete, flip ``LOADING → SYNCING`` so the user
       can use the app while the heavy phase runs.
    3. Heavy phase — invoices + credit notes + payments serial.
    4. Per-contact index build — only if every heavy-phase resource succeeded,
       otherwise the index would misrepresent partial data.
    5. On full success — ``mark_reconcile_ready`` + ``FREE`` with the start
       timestamp as ``LastSyncTime``.
    6. On any failure — ``LOAD_INCOMPLETE`` so the UI offers a Retry button,
       except for a manual ``SYNCING`` run on a tenant whose
       ``ReconcileReadyAt`` is already set — that stays ``FREE`` with
       ``LastSyncTime=None`` so reconcile access is not pulled out from under
       the user mid-session.

    Args:
        only_run_resources: When set, limit execution to the named resources
            (``contacts``, ``credit_notes``, ``invoices``, ``payments``) and
            treat the others as "already done" for index+reconcile-ready
            evaluation. Used by the Retry-sync endpoint to re-run only the
            failed subset without re-fetching all of Xero. ``None`` (default)
            runs a full sync as before.
        already_acquired: When ``True``, skip the ``try_acquire_sync`` call
            at step 1 and trust that the caller has already claimed the lock
            (and therefore owns the responsibility to release it on early
            failure). Set by the ``/sync`` and ``/retry-sync`` endpoints so
            they can observe the 409 synchronously instead of swallowing it
            in the worker thread.
    """
    if not already_acquired and not TenantDataRepository.try_acquire_sync(tenant_id, target_status=operation_type, stale_threshold_ms=SYNC_STALE_THRESHOLD_MS):
        logger.warning("Sync already in flight; skipping overlapping start", tenant_id=tenant_id, target_status=str(operation_type))
        return

    # Reset stale progress sub-maps so a fresh run doesn't carry FAILED /
    # IN_PROGRESS markers from an interrupted prior attempt. Scope to the
    # resources we're about to run so retry paths preserve COMPLETE markers
    # on the subset they're deliberately skipping.
    resources_to_reset = only_run_resources if only_run_resources is not None else set(ALL_SYNC_RESOURCES)
    TenantDataRepository.reset_resource_progress(tenant_id, resources_to_reset)

    tenant_record = TenantDataRepository.get_item(tenant_id)
    reconcile_ready_before = bool(tenant_record and tenant_record.get("ReconcileReadyAt"))
    modified_since: datetime | None = None
    if operation_type != TenantStatus.LOADING and tenant_record:
        modified_since = _resolve_modified_since(tenant_record)

    start_time_ms = int(time.time() * 1000)
    api = get_xero_api_client(oauth_token)

    # Contacts phase: run only on a full sync, or when explicitly retried.
    # Extracted from the heavy loop so sync_data can flip LOADING → SYNCING as
    # soon as contacts finish — unblocking navigation while the rest runs in
    # the background.
    if _should_skip_for_retry("contacts", only_run_resources, tenant_record):
        contacts_ok = True
        logger.info("Retry: skipping contacts phase (already complete or out of scope)", tenant_id=tenant_id)
    else:
        contacts_ok = sync_contacts(api, tenant_id, modified_since=modified_since)

    if not contacts_ok:
        # Heavy phase is skipped; partial contacts data would mislead the UI.
        # A manual sync on an already reconcile-ready tenant must not downgrade
        # them to LOAD_INCOMPLETE on a transient failure — that yanks the
        # reconcile gate shut on a user who was able to use the app before the
        # retry. Mirror the heavy-phase-failure rule: stay FREE with null
        # last_sync_time so telemetry flags the unclean sync.
        if operation_type == TenantStatus.SYNCING and reconcile_ready_before:
            update_tenant_status(tenant_id, TenantStatus.FREE, last_sync_time=None)
        else:
            update_tenant_status(tenant_id, TenantStatus.LOAD_INCOMPLETE)
        logger.warning("Contacts phase failed; sync aborted", tenant_id=tenant_id, reconcile_ready_before=reconcile_ready_before)
        return

    # Contacts done — unblock the app before starting the heavy phase.
    # Only fires on a full initial load; retry runs are already past LOADING.
    if operation_type == TenantStatus.LOADING and only_run_resources is None:
        update_tenant_status(tenant_id, TenantStatus.SYNCING)

    heavy_specs: list[tuple[str, Callable[..., bool]]] = [("credit_notes", sync_credit_notes), ("invoices", sync_invoices), ("payments", sync_payments)]
    heavy_results: dict[str, bool] = {}
    for resource, fetcher in heavy_specs:
        if _should_skip_for_retry(resource, only_run_resources, tenant_record):
            logger.info("Retry: skipping resource", tenant_id=tenant_id, resource=resource)
            continue
        heavy_results[resource] = fetcher(api, tenant_id, modified_since=modified_since)

    specified_ok = all(heavy_results.values()) if heavy_results else True

    # For a retry run we need to evaluate overall completeness across ALL
    # resources (including ones we skipped because they were already complete).
    # Re-read the tenant row so we don't rely on a stale snapshot.
    if only_run_resources is not None:
        fresh_record = TenantDataRepository.get_item(tenant_id) or {}
        all_resources_complete = all(_resource_is_complete(fresh_record, r) for r in ("contacts", "credit_notes", "invoices", "payments"))
        heavy_ok = specified_ok and all_resources_complete
    else:
        heavy_ok = specified_ok

    if heavy_ok:
        # Per-contact index is a view of the flat files — only safe when every
        # source file is current. Partial data would surface as missing rows
        # on the statement detail page.
        _safe_update_progress(tenant_id, "per_contact_index", ProgressStatus.IN_PROGRESS)
        try:
            build_per_contact_index(tenant_id)
            _safe_update_progress(tenant_id, "per_contact_index", ProgressStatus.COMPLETE)
        except Exception:
            logger.exception("Failed to build per-contact index", tenant_id=tenant_id)
            _safe_update_progress(tenant_id, "per_contact_index", ProgressStatus.FAILED)
            heavy_ok = False

    # Bump the tenant cache generation so any Redis-cached statement views
    # (which embed Xero reconciliation data) become unreachable. Runs on
    # both success and partial failure — stale data is never correct.
    try:
        bump_tenant_generation(tenant_id)
    except Exception:
        logger.exception("Failed to bump tenant cache generation after sync", tenant_id=tenant_id)

    if heavy_ok:
        # Only flip the gate on the first successful full load. A manual
        # incremental sync on an already-ready tenant must leave ReconcileReadyAt
        # (and LastFullLoadCompletedAt) untouched — rewriting them would look
        # like a fresh initial load in telemetry.
        if not reconcile_ready_before:
            TenantDataRepository.mark_reconcile_ready(tenant_id)
        update_tenant_status(tenant_id, TenantStatus.FREE, last_sync_time=start_time_ms)
        return

    if operation_type == TenantStatus.SYNCING and reconcile_ready_before:
        # Manual incremental sync on an already reconcile-ready tenant: a
        # partial failure mustn't yank the user out of the app. Keep them at
        # FREE but signal "not a clean sync" by clearing LastSyncTime.
        update_tenant_status(tenant_id, TenantStatus.FREE, last_sync_time=None)
        return

    update_tenant_status(tenant_id, TenantStatus.LOAD_INCOMPLETE)
