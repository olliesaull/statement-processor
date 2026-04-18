"""Tests for per-endpoint page_size and pagination metadata logging in xero_repository.

Verifies that:
- Each fetcher uses its per-endpoint page_size constant.
- Pagination metadata is logged after the first page fetch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import xero_repository as xero_module
from xero_repository import CONTACTS_PAGE_SIZE, CREDIT_NOTES_PAGE_SIZE, INVOICES_PAGE_SIZE, PAYMENTS_PAGE_SIZE, get_contacts_from_xero, get_credit_notes, get_invoices, get_payments

TENANT_ID = "tenant-pagination-test"


def _make_pagination(item_count: int | None = 0, page_count: int | None = 0) -> SimpleNamespace | None:
    if item_count is None and page_count is None:
        return None
    return SimpleNamespace(item_count=item_count, page_count=page_count)


def _make_invoice_result(invoices: list | None = None, pagination: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(invoices=invoices or [], pagination=pagination)


def _make_credit_note_result(credit_notes: list | None = None, pagination: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(credit_notes=credit_notes or [], pagination=pagination)


def _make_payment_result(payments: list | None = None, pagination: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(payments=payments or [], pagination=pagination)


def _make_contacts_result(contacts: list | None = None, pagination: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(contacts=contacts or [], pagination=pagination)


class TestInvoicesPageSize:
    """Verify get_invoices uses INVOICES_PAGE_SIZE."""

    def test_page_size_constant_is_1000(self) -> None:
        """Invoices endpoint supports page_size up to 1000."""
        assert INVOICES_PAGE_SIZE == 1000

    def test_passes_invoices_page_size_to_xero_client(self) -> None:
        api = MagicMock()
        api.get_invoices.return_value = _make_invoice_result(invoices=[], pagination=_make_pagination(0, 0))

        get_invoices(tenant_id=TENANT_ID, api=api)

        assert api.get_invoices.called
        call_kwargs = api.get_invoices.call_args.kwargs
        assert call_kwargs["page_size"] == INVOICES_PAGE_SIZE


class TestCreditNotesPageSize:
    """Verify get_credit_notes uses CREDIT_NOTES_PAGE_SIZE."""

    def test_page_size_constant_is_1000(self) -> None:
        """CreditNotes endpoint supports page_size up to 1000."""
        assert CREDIT_NOTES_PAGE_SIZE == 1000

    def test_passes_credit_notes_page_size_to_xero_client(self) -> None:
        api = MagicMock()
        api.get_credit_notes.return_value = _make_credit_note_result(credit_notes=[], pagination=_make_pagination(0, 0))

        get_credit_notes(tenant_id=TENANT_ID, api=api)

        assert api.get_credit_notes.called
        call_kwargs = api.get_credit_notes.call_args.kwargs
        assert call_kwargs["page_size"] == CREDIT_NOTES_PAGE_SIZE


class TestPaymentsPageSize:
    """Verify get_payments uses PAYMENTS_PAGE_SIZE."""

    def test_page_size_constant_is_1000(self) -> None:
        """Payments endpoint supports page_size up to 1000."""
        assert PAYMENTS_PAGE_SIZE == 1000

    def test_passes_payments_page_size_to_xero_client(self) -> None:
        api = MagicMock()
        api.get_payments.return_value = _make_payment_result(payments=[], pagination=_make_pagination(0, 0))

        get_payments(tenant_id=TENANT_ID, api=api)

        assert api.get_payments.called
        call_kwargs = api.get_payments.call_args.kwargs
        assert call_kwargs["page_size"] == PAYMENTS_PAGE_SIZE


class TestContactsPageSize:
    """Verify get_contacts_from_xero uses CONTACTS_PAGE_SIZE."""

    def test_page_size_constant_is_100(self) -> None:
        """Contacts endpoint is kept at 100 (Xero historical cap; re-verify before bumping)."""
        assert CONTACTS_PAGE_SIZE == 100

    def test_passes_contacts_page_size_to_xero_client(self) -> None:
        api = MagicMock()
        api.get_contacts.return_value = _make_contacts_result(contacts=[], pagination=_make_pagination(0, 0))

        get_contacts_from_xero(tenant_id=TENANT_ID, api=api)

        assert api.get_contacts.called
        call_kwargs = api.get_contacts.call_args.kwargs
        assert call_kwargs["page_size"] == CONTACTS_PAGE_SIZE


class TestPaginationMetadataLogging:
    """Verify each fetcher logs pagination metadata observation after the first page."""

    def test_invoices_logs_pagination_metadata(self, caplog, monkeypatch) -> None:
        api = MagicMock()
        api.get_invoices.return_value = _make_invoice_result(invoices=[], pagination=_make_pagination(item_count=250, page_count=1))

        # Use stdlib logging so caplog captures — the module uses powertools logger
        # which doesn't route through caplog, so inject a spy instead.
        logged_calls: list[dict] = []

        def spy(msg: str, **kwargs):  # noqa: ARG001
            if msg == "Xero pagination metadata":
                logged_calls.append(kwargs)

        monkeypatch.setattr(xero_module.logger, "info", spy)

        get_invoices(tenant_id=TENANT_ID, api=api)

        assert logged_calls, "Expected 'Xero pagination metadata' info log"
        entry = logged_calls[0]
        assert entry["resource"] == "invoices"
        assert entry["has_pagination"] is True
        assert entry["item_count"] == 250

    def test_credit_notes_logs_pagination_metadata(self, monkeypatch) -> None:
        api = MagicMock()
        api.get_credit_notes.return_value = _make_credit_note_result(credit_notes=[], pagination=_make_pagination(item_count=7, page_count=1))

        logged_calls: list[dict] = []

        def spy(msg: str, **kwargs):  # noqa: ARG001
            if msg == "Xero pagination metadata":
                logged_calls.append(kwargs)

        monkeypatch.setattr(xero_module.logger, "info", spy)

        get_credit_notes(tenant_id=TENANT_ID, api=api)

        assert logged_calls
        entry = logged_calls[0]
        assert entry["resource"] == "credit_notes"
        assert entry["has_pagination"] is True
        assert entry["item_count"] == 7

    def test_payments_logs_pagination_metadata(self, monkeypatch) -> None:
        api = MagicMock()
        api.get_payments.return_value = _make_payment_result(payments=[], pagination=_make_pagination(item_count=99, page_count=1))

        logged_calls: list[dict] = []

        def spy(msg: str, **kwargs):  # noqa: ARG001
            if msg == "Xero pagination metadata":
                logged_calls.append(kwargs)

        monkeypatch.setattr(xero_module.logger, "info", spy)

        get_payments(tenant_id=TENANT_ID, api=api)

        assert logged_calls
        entry = logged_calls[0]
        assert entry["resource"] == "payments"
        assert entry["has_pagination"] is True
        assert entry["item_count"] == 99

    def test_contacts_logs_pagination_metadata(self, monkeypatch) -> None:
        api = MagicMock()
        api.get_contacts.return_value = _make_contacts_result(contacts=[], pagination=_make_pagination(item_count=5, page_count=1))

        logged_calls: list[dict] = []

        def spy(msg: str, **kwargs):  # noqa: ARG001
            if msg == "Xero pagination metadata":
                logged_calls.append(kwargs)

        monkeypatch.setattr(xero_module.logger, "info", spy)

        get_contacts_from_xero(tenant_id=TENANT_ID, api=api)

        assert logged_calls
        entry = logged_calls[0]
        assert entry["resource"] == "contacts"
        assert entry["has_pagination"] is True
        assert entry["item_count"] == 5

    def test_null_pagination_still_logs_with_has_pagination_false(self, monkeypatch) -> None:
        """When Xero returns no pagination info, has_pagination=False is logged.

        This is the signal that downstream callers (Step 3 callback, Step 7 UI)
        must render indeterminate progress because record_total is unknown.
        """
        api = MagicMock()
        api.get_invoices.return_value = _make_invoice_result(invoices=[], pagination=None)

        logged_calls: list[dict] = []

        def spy(msg: str, **kwargs):  # noqa: ARG001
            if msg == "Xero pagination metadata":
                logged_calls.append(kwargs)

        monkeypatch.setattr(xero_module.logger, "info", spy)

        get_invoices(tenant_id=TENANT_ID, api=api)

        assert logged_calls
        entry = logged_calls[0]
        assert entry["has_pagination"] is False
        assert entry["item_count"] is None


class TestFetcherProgressCallbacks:
    """Each fetcher fires progress_callback after every page with monotonic counts."""

    def _make_invoice_items(self, n: int, start: int = 1) -> list:
        return [
            SimpleNamespace(
                invoice_id=f"inv-{i}",
                type="ACCPAY",
                number=f"N{i}",
                status="AUTHORISED",
                date=None,
                due_date=None,
                reference=None,
                total=0,
                amount_due=0,
                amount_paid=0,
                amount_credited=0,
                updated_date_utc=None,
                contact=SimpleNamespace(contact_id="c1", name="c"),
            )
            for i in range(start, start + n)
        ]

    def test_invoices_fires_callback_with_monotonic_records_fetched(self) -> None:
        """Callback sees records_fetched grow across pages and record_total from pagination."""
        api = MagicMock()
        # Two pages: first full (INVOICES_PAGE_SIZE), second short.
        first_batch = self._make_invoice_items(INVOICES_PAGE_SIZE)
        second_batch = self._make_invoice_items(5, start=INVOICES_PAGE_SIZE + 1)
        api.get_invoices.side_effect = [
            _make_invoice_result(invoices=first_batch, pagination=_make_pagination(item_count=INVOICES_PAGE_SIZE + 5, page_count=2)),
            _make_invoice_result(invoices=second_batch, pagination=_make_pagination(item_count=INVOICES_PAGE_SIZE + 5, page_count=2)),
        ]

        calls: list[tuple[int, int | None]] = []
        get_invoices(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, total: calls.append((fetched, total)))

        assert len(calls) == 2
        fetched_counts = [c[0] for c in calls]
        totals = [c[1] for c in calls]
        # Counts must be strictly monotonic — downstream UI would glitch otherwise.
        assert fetched_counts[0] == INVOICES_PAGE_SIZE
        assert fetched_counts[1] == INVOICES_PAGE_SIZE + 5
        assert totals == [INVOICES_PAGE_SIZE + 5, INVOICES_PAGE_SIZE + 5]

    def test_invoices_callback_receives_none_when_pagination_absent(self) -> None:
        """record_total=None propagates through the callback unchanged."""
        api = MagicMock()
        api.get_invoices.return_value = _make_invoice_result(invoices=self._make_invoice_items(3), pagination=None)

        calls: list[tuple[int, int | None]] = []
        get_invoices(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, total: calls.append((fetched, total)))

        assert len(calls) == 1
        assert calls[0][0] == 3
        assert calls[0][1] is None

    def _make_credit_note_items(self, n: int, start: int = 1) -> list:
        return [
            SimpleNamespace(
                credit_note_id=f"cn-{i}",
                credit_note_number="N",
                type="ACCPAYCREDIT",
                status="AUTHORISED",
                date=None,
                due_date=None,
                reference=None,
                total=0,
                amount_credited=0,
                remaining_credit=0,
                contact=SimpleNamespace(contact_id="c1", name="c"),
            )
            for i in range(start, start + n)
        ]

    def _make_payment_items(self, n: int, start: int = 1) -> list:
        return [
            SimpleNamespace(
                payment_id=f"pay-{i}", reference=None, amount=0, date=None, status="AUTHORISED", invoice=SimpleNamespace(invoice_id=f"inv-{i}", contact=SimpleNamespace(contact_id="c1", name="c"))
            )
            for i in range(start, start + n)
        ]

    def test_credit_notes_fires_callback_with_monotonic_records_fetched(self) -> None:
        """Multi-page credit_notes callback grows records_fetched and keeps record_total stable."""
        api = MagicMock()
        first_batch = self._make_credit_note_items(CREDIT_NOTES_PAGE_SIZE)
        second_batch = self._make_credit_note_items(3, start=CREDIT_NOTES_PAGE_SIZE + 1)
        total = CREDIT_NOTES_PAGE_SIZE + 3
        api.get_credit_notes.side_effect = [
            _make_credit_note_result(credit_notes=first_batch, pagination=_make_pagination(item_count=total, page_count=2)),
            _make_credit_note_result(credit_notes=second_batch, pagination=_make_pagination(item_count=total, page_count=2)),
        ]

        calls: list[tuple[int, int | None]] = []
        get_credit_notes(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, t: calls.append((fetched, t)))

        assert [c[0] for c in calls] == [CREDIT_NOTES_PAGE_SIZE, total]
        assert [c[1] for c in calls] == [total, total]

    def test_payments_fires_callback_with_monotonic_records_fetched(self) -> None:
        """Multi-page payments callback grows records_fetched and keeps record_total stable."""
        api = MagicMock()
        first_batch = self._make_payment_items(PAYMENTS_PAGE_SIZE)
        second_batch = self._make_payment_items(7, start=PAYMENTS_PAGE_SIZE + 1)
        total = PAYMENTS_PAGE_SIZE + 7
        api.get_payments.side_effect = [
            _make_payment_result(payments=first_batch, pagination=_make_pagination(item_count=total, page_count=2)),
            _make_payment_result(payments=second_batch, pagination=_make_pagination(item_count=total, page_count=2)),
        ]

        calls: list[tuple[int, int | None]] = []
        get_payments(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, t: calls.append((fetched, t)))

        assert [c[0] for c in calls] == [PAYMENTS_PAGE_SIZE, total]
        assert [c[1] for c in calls] == [total, total]

    def test_credit_notes_fires_callback(self) -> None:
        api = MagicMock()
        batch = [
            SimpleNamespace(
                credit_note_id=f"cn-{i}",
                credit_note_number="N",
                type="ACCPAYCREDIT",
                status="AUTHORISED",
                date=None,
                due_date=None,
                reference=None,
                total=0,
                amount_credited=0,
                remaining_credit=0,
                contact=SimpleNamespace(contact_id="c1", name="c"),
            )
            for i in range(3)
        ]
        api.get_credit_notes.return_value = _make_credit_note_result(credit_notes=batch, pagination=_make_pagination(item_count=3, page_count=1))

        calls: list[tuple[int, int | None]] = []
        get_credit_notes(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, total: calls.append((fetched, total)))

        assert calls == [(3, 3)]

    def test_payments_fires_callback(self) -> None:
        api = MagicMock()
        batch = [
            SimpleNamespace(
                payment_id=f"pay-{i}", reference=None, amount=0, date=None, status="AUTHORISED", invoice=SimpleNamespace(invoice_id=f"inv-{i}", contact=SimpleNamespace(contact_id="c1", name="c"))
            )
            for i in range(4)
        ]
        api.get_payments.return_value = _make_payment_result(payments=batch, pagination=_make_pagination(item_count=4, page_count=1))

        calls: list[tuple[int, int | None]] = []
        get_payments(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, total: calls.append((fetched, total)))

        assert calls == [(4, 4)]

    def test_contacts_fires_callback(self) -> None:
        api = MagicMock()
        batch = [SimpleNamespace(contact_id=f"c-{i}", name=f"Contact {i}", updated_date_utc=None, contact_status="ACTIVE") for i in range(2)]
        api.get_contacts.return_value = _make_contacts_result(contacts=batch, pagination=_make_pagination(item_count=2, page_count=1))

        calls: list[tuple[int, int | None]] = []
        get_contacts_from_xero(tenant_id=TENANT_ID, api=api, progress_callback=lambda fetched, total: calls.append((fetched, total)))

        assert calls == [(2, 2)]

    def test_callback_not_fired_when_none(self) -> None:
        """Default behaviour (no callback passed) must not crash."""
        api = MagicMock()
        api.get_invoices.return_value = _make_invoice_result(invoices=self._make_invoice_items(1), pagination=_make_pagination(item_count=1, page_count=1))

        # Should not raise.
        get_invoices(tenant_id=TENANT_ID, api=api)
