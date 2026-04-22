"""Tests for the sync-progress view-model helpers (utils/sync_progress.py).

Covers per-resource parsing, percent calculation, polling stop conditions,
and multi-tenant view composition from session tenants + DynamoDB rows.
"""

from __future__ import annotations

from tenant_data_repository import SYNC_STALE_THRESHOLD_MS, TenantStatus
from utils.sync_progress import RESOURCE_ORDER, build_progress_view, build_tenant_progress_view, is_retry_recommended, should_poll


class TestBuildTenantProgressView:
    """Translate a raw TenantData item into a render-friendly progress view."""

    def test_empty_item_yields_pending_resources(self):
        """Legacy rows with no progress attributes render as pending."""
        view = build_tenant_progress_view("tenant-1", "Acme", {})

        assert view.tenant_id == "tenant-1"
        assert view.tenant_name == "Acme"
        assert view.status == TenantStatus.FREE
        assert view.reconcile_ready is False
        assert [r.resource for r in view.resources] == list(RESOURCE_ORDER)
        for resource in view.resources:
            assert resource.status == "pending"
            assert resource.records_fetched is None
            assert resource.record_total is None
            assert resource.percent is None

    def test_none_item_yields_pending_resources(self):
        """A missing row is treated the same as an empty item."""
        view = build_tenant_progress_view("tenant-1", "Acme", None)

        assert view.status == TenantStatus.FREE
        assert view.reconcile_ready is False
        assert all(r.status == "pending" for r in view.resources)
        assert view.per_contact_index_status == "pending"

    def test_progress_in_flight_sets_percent_and_counts(self):
        """Active resource with known total must compute a 0-100 percent."""
        item = {"TenantStatus": "SYNCING", "InvoicesProgress": {"status": "in_progress", "records_fetched": 250, "record_total": 1000}}

        view = build_tenant_progress_view("tenant-1", "Acme", item)
        invoices = next(r for r in view.resources if r.resource == "invoices")

        assert view.status == TenantStatus.SYNCING
        assert invoices.status == "in_progress"
        assert invoices.records_fetched == 250
        assert invoices.record_total == 1000
        assert invoices.percent == 25
        assert invoices.indeterminate is False
        assert invoices.is_active is True

    def test_missing_record_total_is_indeterminate(self):
        """record_total=null should not compute a percent; UI shows striped bar."""
        item = {"InvoicesProgress": {"status": "in_progress", "records_fetched": 50, "record_total": None}}

        view = build_tenant_progress_view("tenant-1", "Acme", item)
        invoices = next(r for r in view.resources if r.resource == "invoices")

        assert invoices.records_fetched == 50
        assert invoices.record_total is None
        assert invoices.percent is None
        assert invoices.indeterminate is True

    def test_complete_resource_reports_is_complete(self):
        """Status=complete must flag through is_complete and show 100% bar."""
        item = {"ContactsProgress": {"status": "complete", "records_fetched": 100, "record_total": 100}}

        view = build_tenant_progress_view("tenant-1", "Acme", item)
        contacts = next(r for r in view.resources if r.resource == "contacts")

        assert contacts.is_complete is True
        assert contacts.percent == 100

    def test_failed_resource_propagates_to_has_failure(self):
        """Any failed resource must flag the tenant-level has_failure banner trigger."""
        item = {"CreditNotesProgress": {"status": "failed", "records_fetched": 25, "record_total": 100}}

        view = build_tenant_progress_view("tenant-1", "Acme", item)

        assert view.has_failure is True

    def test_reconcile_ready_requires_timestamp_and_all_complete(self):
        """all_complete is only true when every resource, the index, and reconcile_ready line up."""
        complete_payload = {"status": "complete", "records_fetched": 10, "record_total": 10}
        item = {
            "TenantStatus": "FREE",
            "ReconcileReadyAt": 1_700_000_000_000,
            "ContactsProgress": complete_payload,
            "InvoicesProgress": complete_payload,
            "CreditNotesProgress": complete_payload,
            "PaymentsProgress": complete_payload,
            "PerContactIndexProgress": {"status": "complete"},
        }

        view = build_tenant_progress_view("tenant-1", "Acme", item)

        assert view.reconcile_ready is True
        assert view.all_complete is True
        assert view.per_contact_index_status == "complete"
        assert view.has_failure is False

    def test_reconcile_ready_alone_is_not_all_complete(self):
        """Unwritten resource progress must keep all_complete false even when reconcile-ready is set."""
        item = {"ReconcileReadyAt": 1_700_000_000_000}

        view = build_tenant_progress_view("tenant-1", "Acme", item)

        assert view.reconcile_ready is True
        assert view.all_complete is False

    def test_reads_last_sync_time_ms(self):
        """LastSyncTime is surfaced on the view for the card's Last sync metric."""
        item = {"LastSyncTime": 1_712_000_000_000}

        view = build_tenant_progress_view("tenant-1", "Acme", item)

        assert view.last_sync_time_ms == 1_712_000_000_000

    def test_last_sync_time_ms_is_none_when_missing(self):
        """First-ever sync: LastSyncTime absent -> field is None (UI renders muted 'First sync...')."""
        view = build_tenant_progress_view("tenant-1", "Acme", {})

        assert view.last_sync_time_ms is None

    def test_last_sync_time_ms_normalises_decimal(self):
        """DynamoDB numeric attributes come back as Decimal; coerce to int so the filter can format it."""
        from decimal import Decimal

        view = build_tenant_progress_view("tenant-1", "Acme", {"LastSyncTime": Decimal("1712000000000")})

        assert view.last_sync_time_ms == 1_712_000_000_000
        assert isinstance(view.last_sync_time_ms, int)

    def test_is_finalising_when_fetchers_done_but_index_incomplete(self):
        """All four Xero fetchers done + per_contact_index still in_progress = 'Finalising'."""
        done = {"status": "complete", "records_fetched": 10, "record_total": 10}
        item = {
            "ContactsProgress": done,
            "CreditNotesProgress": done,
            "InvoicesProgress": done,
            "PaymentsProgress": done,
            "PerContactIndexProgress": {"status": "in_progress"},
        }

        view = build_tenant_progress_view("t", "n", item)

        assert view.is_finalising is True

    def test_is_not_finalising_when_any_fetcher_incomplete(self):
        """Any active Xero fetcher means we're still 'Syncing', not 'Finalising'."""
        done = {"status": "complete", "records_fetched": 10, "record_total": 10}
        active = {"status": "in_progress", "records_fetched": 5, "record_total": 10}
        item = {
            "ContactsProgress": done,
            "CreditNotesProgress": done,
            "InvoicesProgress": active,
            "PaymentsProgress": done,
            "PerContactIndexProgress": {"status": "in_progress"},
        }

        view = build_tenant_progress_view("t", "n", item)

        assert view.is_finalising is False

    def test_is_not_finalising_when_index_complete(self):
        """All five complete -> Ready, not Finalising."""
        done = {"status": "complete", "records_fetched": 10, "record_total": 10}
        item = {
            "ContactsProgress": done,
            "CreditNotesProgress": done,
            "InvoicesProgress": done,
            "PaymentsProgress": done,
            "PerContactIndexProgress": {"status": "complete"},
        }

        view = build_tenant_progress_view("t", "n", item)

        assert view.is_finalising is False

    def test_is_not_finalising_when_index_failed(self):
        """Failed index is a failure state, not a finalising state."""
        done = {"status": "complete", "records_fetched": 10, "record_total": 10}
        item = {
            "ContactsProgress": done,
            "CreditNotesProgress": done,
            "InvoicesProgress": done,
            "PaymentsProgress": done,
            "PerContactIndexProgress": {"status": "failed"},
        }

        view = build_tenant_progress_view("t", "n", item)

        assert view.is_finalising is False


class TestBuildProgressView:
    """Assemble a list of tenant views from session tenants + BatchGetItem rows."""

    def test_composes_views_for_session_tenants(self):
        session_tenants = [{"tenantId": "t1", "tenantName": "Acme"}, {"tenantId": "t2", "tenantName": "Other"}]
        rows = {"t1": {"TenantStatus": "SYNCING", "InvoicesProgress": {"status": "in_progress", "records_fetched": 1, "record_total": 2}}, "t2": None}

        views = build_progress_view(session_tenants, rows)

        assert len(views) == 2
        assert views[0].tenant_id == "t1"
        assert views[0].tenant_name == "Acme"
        assert views[0].status == TenantStatus.SYNCING
        assert views[1].tenant_id == "t2"
        assert views[1].status == TenantStatus.FREE

    def test_skips_malformed_session_tenants(self):
        """Defensive: ignore entries without tenantId or non-dict entries."""
        session_tenants = [{"tenantId": "t1", "tenantName": "Acme"}, {"tenantName": "Missing Id"}, "nonsense", None]

        views = build_progress_view(session_tenants, {"t1": {}})

        assert len(views) == 1
        assert views[0].tenant_id == "t1"

    def test_tenant_name_falls_back_to_id(self):
        """Without tenantName we should not crash; show the id instead."""
        views = build_progress_view([{"tenantId": "t1"}], {"t1": None})

        assert views[0].tenant_name == "t1"


class TestShouldPoll:
    """Polling stops only when every view is fully complete."""

    def test_polls_when_any_view_incomplete(self):
        in_progress = {"ContactsProgress": {"status": "in_progress", "records_fetched": 1, "record_total": 10}}
        complete = {
            "ReconcileReadyAt": 1_700_000_000_000,
            "ContactsProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "InvoicesProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "CreditNotesProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "PaymentsProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "PerContactIndexProgress": {"status": "complete"},
        }
        views = build_progress_view([{"tenantId": "t1"}, {"tenantId": "t2"}], {"t1": in_progress, "t2": complete})

        assert should_poll(views) is True

    def test_stops_polling_when_all_complete(self):
        complete = {
            "ReconcileReadyAt": 1_700_000_000_000,
            "ContactsProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "InvoicesProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "CreditNotesProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "PaymentsProgress": {"status": "complete", "records_fetched": 1, "record_total": 1},
            "PerContactIndexProgress": {"status": "complete"},
        }
        views = build_progress_view([{"tenantId": "t1"}], {"t1": complete})

        assert should_poll(views) is False

    def test_polls_on_empty_list(self):
        """No session tenants shouldn't keep polling forever — stop immediately."""
        assert should_poll([]) is False


class TestIsRetryRecommended:
    """Surface Retry sync when (and only when) the tenant can't progress without operator intervention.

    ``is_retry_recommended`` gates the Sync vs Retry-sync button on
    ``/tenant_management``. Heartbeat staleness is the same gate used by
    ``try_acquire_sync`` — keeping them aligned means the button the user
    sees maps directly to whether retry-sync will succeed.
    """

    NOW_MS = 1_700_000_000_000
    COMPLETE = {"status": "complete", "records_fetched": 10, "record_total": 10}

    def _fully_complete_row(self) -> dict:
        return {
            "TenantStatus": "FREE",
            "ReconcileReadyAt": 1,
            "ContactsProgress": self.COMPLETE,
            "CreditNotesProgress": self.COMPLETE,
            "InvoicesProgress": self.COMPLETE,
            "PaymentsProgress": self.COMPLETE,
            "PerContactIndexProgress": {"status": "complete"},
        }

    def test_reconcile_ready_tenant_returns_false(self):
        """A FREE tenant that finished reconcile prep must not suggest a retry."""
        assert is_retry_recommended(self._fully_complete_row(), now_ms=self.NOW_MS) is False

    def test_load_incomplete_returns_true(self):
        """LOAD_INCOMPLETE is the canonical retry signal — always recommend Retry."""
        item = {"TenantStatus": "LOAD_INCOMPLETE"}
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is True

    def test_any_failed_progress_returns_true(self):
        """A failed resource map should surface Retry even when TenantStatus looks benign."""
        item = {"TenantStatus": "SYNCING", "InvoicesProgress": {"status": "failed"}}
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is True

    def test_in_progress_with_fresh_heartbeat_returns_false(self):
        """A live sync with a recent heartbeat must keep the Sync button (no Retry noise)."""
        item = {
            "TenantStatus": "SYNCING",
            "LastHeartbeatAt": self.NOW_MS - 1_000,  # 1s ago
            "InvoicesProgress": {"status": "in_progress"},
        }
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is False

    def test_in_progress_with_stale_heartbeat_returns_true(self):
        """Stale heartbeat is the crashed-worker signal — retry is safe and recommended."""
        item = {
            "TenantStatus": "SYNCING",
            "LastHeartbeatAt": self.NOW_MS - (SYNC_STALE_THRESHOLD_MS + 1_000),
            "PaymentsProgress": {"status": "in_progress", "records_fetched": 34000, "record_total": 36219},
        }
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is True

    def test_in_progress_without_heartbeat_returns_false(self):
        """Defensive: no LastHeartbeatAt means we can't be sure the sync is dead — don't flip the button."""
        item = {"TenantStatus": "SYNCING", "InvoicesProgress": {"status": "in_progress"}}
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is False

    def test_erased_returns_false(self):
        """ERASED is a terminal state; offering Retry is nonsensical."""
        item = {"TenantStatus": "ERASED"}
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is False

    def test_none_item_returns_false(self):
        """A missing row is a legacy tenant — render Sync, not Retry."""
        assert is_retry_recommended(None, now_ms=self.NOW_MS) is False

    def test_custom_stale_threshold_respected(self):
        """Callers can override the staleness cut-off (used in tests / future tuning)."""
        item = {
            "TenantStatus": "SYNCING",
            "LastHeartbeatAt": self.NOW_MS - 90_000,  # 90s old
            "InvoicesProgress": {"status": "in_progress"},
        }
        # Default threshold (5 min) treats this as fresh.
        assert is_retry_recommended(item, now_ms=self.NOW_MS) is False
        # Caller-provided 60s threshold treats this as stale.
        assert is_retry_recommended(item, now_ms=self.NOW_MS, stale_threshold_ms=60_000) is True
