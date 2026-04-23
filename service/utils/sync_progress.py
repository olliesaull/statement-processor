"""View-model helpers for the sync-progress HTMX partials.

Translates the raw ``TenantData`` item shape (per-resource ``*Progress`` maps,
``ReconcileReadyAt``, ``TenantStatus``) into a flat, render-friendly structure
that the Jinja partials (``sync_progress_panel.html``,
``statement_wait_panel.html``) can iterate without additional logic.

Centralised here so both the multi-tenant ``/tenants/sync-progress`` endpoint
and the single-tenant ``/statement/<id>/wait`` endpoint (plus the
``/tenant_management`` initial render) share one code path — keeping the
"stop polling" rule consistent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from flask import render_template

from pricing_config import SUBSCRIPTION_TIERS
from tenant_billing_repository import SubscriptionState
from tenant_data_repository import ALL_SYNC_RESOURCES, SYNC_STALE_THRESHOLD_MS, ProgressStatus, TenantStatus, _progress_attribute_name

# Display order matches the sync order in ``sync.py`` — users see fetchers finish
# in the same sequence the backend runs them.
RESOURCE_ORDER: tuple[str, ...] = ("contacts", "credit_notes", "invoices", "payments")

_RESOURCE_DISPLAY_NAMES: dict[str, str] = {"contacts": "Contacts", "credit_notes": "Credit notes", "invoices": "Invoices", "payments": "Payments"}

_PER_CONTACT_INDEX_RESOURCE = "per_contact_index"


@dataclass(frozen=True)
class ResourceProgress:
    """Render-ready view of a single ``*Progress`` sub-map.

    ``percent`` is ``None`` whenever pagination totals are unknown — the
    partials render an indeterminate (striped) bar in that case rather than
    guessing a total.
    """

    resource: str
    display_name: str
    status: str
    records_fetched: int | None
    record_total: int | None
    percent: int | None

    @property
    def indeterminate(self) -> bool:
        """True when a fetcher is running but Xero didn't return a total.

        Drives the striped progress bar in the partials; see plan Step 3 for
        why ``record_total=None`` is preserved rather than coerced to 0.
        """
        return self.record_total is None and self.status == ProgressStatus.IN_PROGRESS

    @property
    def is_complete(self) -> bool:
        """True when the fetcher finished successfully."""
        return self.status == ProgressStatus.COMPLETE

    @property
    def is_failed(self) -> bool:
        """True when the fetcher raised — retry-sync picks this up."""
        return self.status == ProgressStatus.FAILED

    @property
    def is_active(self) -> bool:
        """True while the fetcher is running (``in_progress``)."""
        return self.status == ProgressStatus.IN_PROGRESS

    @property
    def is_pending(self) -> bool:
        """True before the fetcher has started (``pending``)."""
        return self.status == ProgressStatus.PENDING


@dataclass(frozen=True)
class TenantProgressView:
    """Render-ready view of a tenant row for the sync progress partials."""

    tenant_id: str
    tenant_name: str
    status: TenantStatus
    reconcile_ready: bool
    resources: list[ResourceProgress] = field(default_factory=list)
    per_contact_index_status: str = ProgressStatus.PENDING
    last_sync_time_ms: int | None = None

    @property
    def has_failure(self) -> bool:
        """True when any resource or the per-contact index is in ``failed`` state."""
        return any(r.is_failed for r in self.resources) or self.per_contact_index_status == ProgressStatus.FAILED

    @property
    def all_complete(self) -> bool:
        """True when reconcile is ready and every sub-component has completed.

        Used by the poll partial to decide whether to omit ``hx-trigger`` and
        stop polling — a reconcile-ready tenant with a failed sub-component
        still polls in case the user manages to drive a retry.
        """
        return self.reconcile_ready and self.per_contact_index_status == ProgressStatus.COMPLETE and all(r.is_complete for r in self.resources)

    @property
    def in_heavy_phase(self) -> bool:
        """True during initial post-contacts phase — drives the wait banner copy."""
        contacts = next((r for r in self.resources if r.resource == "contacts"), None)
        return (not self.reconcile_ready) and contacts is not None and contacts.is_complete

    @property
    def is_finalising(self) -> bool:
        """True when all four Xero fetchers are complete but per-contact index is still building.

        Distinguishes "waiting on Xero data" (Syncing) from "waiting on
        reconcile index build" (Finalising). Both remain syncing-family — the
        stripe stays blue; only the pill copy changes.
        """
        # Guard empty resources: all([]) is True, which would report Finalising
        # for a directly-constructed view with no resources — the invariant is
        # "all four Xero fetchers complete", so at least one must exist.
        return bool(self.resources) and all(r.is_complete for r in self.resources) and self.per_contact_index_status not in (ProgressStatus.COMPLETE, ProgressStatus.FAILED)


def _parse_tenant_status(raw: Any) -> TenantStatus:
    """Best-effort parse of the stored ``TenantStatus`` attribute.

    Defaults to ``FREE`` when the value is missing or unrecognised — matches
    the existing behaviour of ``TenantDataRepository._determine_status``.
    """
    if isinstance(raw, TenantStatus):
        return raw
    if isinstance(raw, str):
        candidate = raw.strip().upper()
        for status in TenantStatus:
            if candidate == status:
                return status
    return TenantStatus.FREE


def _resource_from_item(item: dict[str, Any], resource: str) -> ResourceProgress:
    """Build a render-ready ``ResourceProgress`` from the raw DDB tenant item.

    DynamoDB returns numeric attributes as ``Decimal``; those are normalised to
    plain ``int`` here so the template can compare percentages with ``<`` and
    ``>``. A missing or partial sub-map resolves to a ``pending`` row with null
    counts — the same shape the UI renders before a sync has ever run.
    """
    raw = item.get(_progress_attribute_name(resource)) or {}
    status = str(raw.get("status") or ProgressStatus.PENDING)
    records_fetched = raw.get("records_fetched")
    record_total = raw.get("record_total")

    # Normalise to int: DynamoDB stores numeric attributes as Decimal, which is
    # not a subclass of int/float, so the bare (int, float) tuple used to miss
    # every DDB-read value.
    records_fetched = int(records_fetched) if isinstance(records_fetched, (int, float, Decimal)) else None
    record_total = int(record_total) if isinstance(record_total, (int, float, Decimal)) else None

    percent: int | None = None
    if records_fetched is not None and record_total is not None and record_total > 0:
        percent = max(0, min(100, int(records_fetched / record_total * 100)))
    elif status == ProgressStatus.COMPLETE:
        # "complete" with unknown totals still reads as 100% visually.
        percent = 100

    return ResourceProgress(resource=resource, display_name=_RESOURCE_DISPLAY_NAMES[resource], status=status, records_fetched=records_fetched, record_total=record_total, percent=percent)


def build_tenant_progress_view(tenant_id: str, tenant_name: str, item: dict[str, Any] | None) -> TenantProgressView:
    """Compose a progress view for one tenant."""
    item = item or {}
    resources = [_resource_from_item(item, r) for r in RESOURCE_ORDER]
    per_index_raw = item.get(_progress_attribute_name(_PER_CONTACT_INDEX_RESOURCE))
    per_index_status = str(per_index_raw.get("status") or ProgressStatus.PENDING) if isinstance(per_index_raw, dict) else ProgressStatus.PENDING

    last_sync_raw = item.get("LastSyncTime")
    last_sync_ms = int(last_sync_raw) if isinstance(last_sync_raw, (int, float, Decimal)) else None

    return TenantProgressView(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        status=_parse_tenant_status(item.get("TenantStatus")),
        reconcile_ready=item.get("ReconcileReadyAt") is not None,
        resources=resources,
        per_contact_index_status=per_index_status,
        last_sync_time_ms=last_sync_ms,
    )


def build_progress_view(session_tenants: list[Any], tenant_rows: dict[str, dict[str, Any] | None]) -> list[TenantProgressView]:
    """Build progress views for every well-formed session tenant.

    ``session_tenants`` is the untyped ``session["xero_tenants"]`` list — we
    skip entries without a ``tenantId`` or that aren't dicts so a corrupted
    session can't 500 the endpoint.
    """
    views: list[TenantProgressView] = []
    for tenant in session_tenants:
        if not isinstance(tenant, dict):
            continue
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            continue
        name = tenant.get("tenantName") or tenant_id
        views.append(build_tenant_progress_view(tenant_id, name, tenant_rows.get(tenant_id)))
    return views


def should_poll(views: list[TenantProgressView]) -> bool:
    """True when the partial should keep polling for updates.

    Empty input resolves to ``False`` so the panel doesn't poll forever when
    the session has no tenants — the UI handles the empty state server-side.
    """
    if not views:
        return False
    return any(not view.all_complete for view in views)


def is_retry_recommended(tenant_item: Mapping[str, Any] | None, *, now_ms: int, stale_threshold_ms: int = SYNC_STALE_THRESHOLD_MS) -> bool:
    """Return True when the operator should see "Retry sync" instead of "Sync".

    Retry is recommended when:
    - ``TenantStatus == LOAD_INCOMPLETE`` — an earlier sync bailed before
      reconcile prep finished.
    - Any per-resource progress map is ``failed``.
    - Any per-resource progress map is ``in_progress`` AND ``LastHeartbeatAt``
      is older than ``stale_threshold_ms`` — the worker crashed mid-fetch.

    ``in_progress`` with a fresh heartbeat keeps the Sync button (no Retry
    noise while a live sync is still making progress). A missing
    ``LastHeartbeatAt`` also keeps Sync — without a stale signal we can't
    prove the sync is dead, so we don't flip the button speculatively.

    Args:
        tenant_item: DynamoDB row for the tenant (or ``None`` for legacy rows).
        now_ms: injected wall clock in epoch milliseconds so callers stay pure
            and tests don't need to monkeypatch ``time.time``.
        stale_threshold_ms: heartbeat age (ms) past which an ``in_progress``
            resource counts as crashed. Defaults to ``SYNC_STALE_THRESHOLD_MS``
            to match ``try_acquire_sync``'s lock-acquire gate.
    """
    if tenant_item is None:
        return False

    if _parse_tenant_status(tenant_item.get("TenantStatus")) == TenantStatus.LOAD_INCOMPLETE:
        return True

    heartbeat = tenant_item.get("LastHeartbeatAt")
    heartbeat_ms = int(heartbeat) if isinstance(heartbeat, (int, float, Decimal)) else None
    stale = heartbeat_ms is not None and (heartbeat_ms + stale_threshold_ms) < now_ms

    for resource in ALL_SYNC_RESOURCES:
        progress = tenant_item.get(_progress_attribute_name(resource))
        if not isinstance(progress, dict):
            continue
        status = str(progress.get("status") or "")
        if status == ProgressStatus.FAILED:
            return True
        if status == ProgressStatus.IN_PROGRESS and stale:
            return True

    return False


def _subscription_plan_display_name(subscription_state: SubscriptionState | None) -> str | None:
    """Return the display name of the current subscription tier, or ``None``.

    Pure formatter — does not gate on ``status``. Callers that need to
    distinguish "has an active subscription" from "has a tier_id from a
    cancelled/past_due row" must check ``subscription_state.status`` separately
    (see ``render_sync_progress_fragment``'s ``is_active_subscription`` kwarg
    and the summary-strip ``{% if subscription_state.status == 'active' %}``
    branch on ``tenant_management.html``).
    """
    if not subscription_state:
        return None
    tier = SUBSCRIPTION_TIERS.get(subscription_state.tier_id)
    return tier.display_name if tier else None


def render_sync_progress_fragment(
    session_tenants: list[Any],
    *,
    tenant_rows: dict[str, dict[str, Any]],
    current_tenant_id: str | None,
    tenant_token_balances: dict[str, int],
    is_active_subscription: bool,
    needs_retry_by_id: dict[str, bool],
) -> str:
    """Render the tenant-card list fragment.

    Callers pre-fetch ``tenant_rows`` (one DynamoDB BatchGetItem) and pass it
    in so ``needs_retry_by_id`` can be derived against the same row snapshot
    this fragment renders — avoids a second round-trip per poll.

    Args:
        session_tenants: ``session["xero_tenants"]``, validated inside.
        tenant_rows: ``{tenant_id: TenantData item}`` from a single
            ``TenantDataRepository.get_many`` call in the caller.
        current_tenant_id: ``session["xero_tenant_id"]``, used for the
            is-current card flag.
        tenant_token_balances: ``{tenant_id: balance}`` from
            ``TenantBillingRepository.get_tenant_token_balances``.
        is_active_subscription: True iff the current tenant has a
            subscription row with ``status == 'active'``. Drives the
            disconnect form's subscription-warning data attribute; kept as a
            pre-computed bool so the macro doesn't need to reach into
            ``SubscriptionState`` shape.
        needs_retry_by_id: ``{tenant_id: bool}`` from ``is_retry_recommended``.

    Returns:
        Rendered HTML string.
    """
    tenant_views = build_progress_view(session_tenants, tenant_rows)
    polling = should_poll(tenant_views)
    return render_template(
        "partials/sync_progress_panel.html",
        tenant_views=tenant_views,
        polling=polling,
        current_tenant_id=current_tenant_id,
        tenant_token_balances=tenant_token_balances,
        is_active_subscription=is_active_subscription,
        needs_retry_by_id=needs_retry_by_id,
        TenantStatus=TenantStatus,
    )
