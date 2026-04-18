"""Tests for the sync-progress view-model helpers (utils/sync_progress.py).

Covers per-resource parsing, percent calculation, polling stop conditions,
and multi-tenant view composition from session tenants + DynamoDB rows.
"""

from __future__ import annotations

from tenant_data_repository import TenantStatus
from utils.sync_progress import RESOURCE_ORDER, build_progress_view, build_tenant_progress_view, should_poll


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
