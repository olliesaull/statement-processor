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

from dataclasses import dataclass, field
from typing import Any

from tenant_data_repository import TenantStatus

# Display order matches the sync order in ``sync.py`` — users see fetchers finish
# in the same sequence the backend runs them.
RESOURCE_ORDER: tuple[str, ...] = ("contacts", "credit_notes", "invoices", "payments")

_RESOURCE_DISPLAY_NAMES: dict[str, str] = {"contacts": "Contacts", "credit_notes": "Credit notes", "invoices": "Invoices", "payments": "Payments"}

_RESOURCE_ATTRIBUTES: dict[str, str] = {"contacts": "ContactsProgress", "credit_notes": "CreditNotesProgress", "invoices": "InvoicesProgress", "payments": "PaymentsProgress"}

_PER_CONTACT_INDEX_ATTR = "PerContactIndexProgress"


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
        return self.record_total is None and self.status == "in_progress"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    @property
    def is_active(self) -> bool:
        return self.status == "in_progress"

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


@dataclass(frozen=True)
class TenantProgressView:
    """Render-ready view of a tenant row for the sync progress partials."""

    tenant_id: str
    tenant_name: str
    status: TenantStatus
    reconcile_ready: bool
    resources: list[ResourceProgress] = field(default_factory=list)
    per_contact_index_status: str = "pending"

    @property
    def has_failure(self) -> bool:
        return any(r.is_failed for r in self.resources) or self.per_contact_index_status == "failed"

    @property
    def all_complete(self) -> bool:
        return self.reconcile_ready and self.per_contact_index_status == "complete" and all(r.is_complete for r in self.resources)

    @property
    def in_heavy_phase(self) -> bool:
        """True during initial post-contacts phase — drives the wait banner copy."""
        contacts = next((r for r in self.resources if r.resource == "contacts"), None)
        return (not self.reconcile_ready) and contacts is not None and contacts.is_complete


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
    raw = item.get(_RESOURCE_ATTRIBUTES[resource]) or {}
    status = str(raw.get("status") or "pending")
    records_fetched = raw.get("records_fetched")
    record_total = raw.get("record_total")

    # Normalise ints; DynamoDB can store them as Decimal when read back.
    records_fetched = int(records_fetched) if isinstance(records_fetched, (int, float)) else None
    record_total = int(record_total) if isinstance(record_total, (int, float)) else None

    percent: int | None = None
    if records_fetched is not None and record_total is not None and record_total > 0:
        percent = max(0, min(100, int(records_fetched / record_total * 100)))
    elif status == "complete":
        # "complete" with unknown totals still reads as 100% visually.
        percent = 100

    return ResourceProgress(resource=resource, display_name=_RESOURCE_DISPLAY_NAMES[resource], status=status, records_fetched=records_fetched, record_total=record_total, percent=percent)


def build_tenant_progress_view(tenant_id: str, tenant_name: str, item: dict[str, Any] | None) -> TenantProgressView:
    """Compose a progress view for one tenant."""
    item = item or {}
    resources = [_resource_from_item(item, r) for r in RESOURCE_ORDER]
    per_index_raw = item.get(_PER_CONTACT_INDEX_ATTR)
    per_index_status = str(per_index_raw.get("status") or "pending") if isinstance(per_index_raw, dict) else "pending"

    return TenantProgressView(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        status=_parse_tenant_status(item.get("TenantStatus")),
        reconcile_ready=item.get("ReconcileReadyAt") is not None,
        resources=resources,
        per_contact_index_status=per_index_status,
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
