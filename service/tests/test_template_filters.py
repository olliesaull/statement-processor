"""Tests for Jinja template filters registered in app.py."""

from __future__ import annotations

from decimal import Decimal

from utils.template_filters import format_last_sync, format_last_sync_iso


class TestFormatLastSync:
    """Format an epoch-ms timestamp as 'Mon D, HH:MM' (UTC)."""

    def test_none_returns_empty_string(self):
        assert format_last_sync(None) == ""

    def test_zero_returns_empty_string(self):
        assert format_last_sync(0) == ""

    def test_formats_epoch_ms_in_utc(self):
        # 2024-01-15 14:30:00 UTC -> epoch ms
        epoch_ms = 1_705_329_000_000
        assert format_last_sync(epoch_ms) == "Jan 15, 14:30"

    def test_accepts_decimal(self):
        # DynamoDB hands numbers back as Decimal.
        assert format_last_sync(Decimal("1705329000000")) == "Jan 15, 14:30"

    def test_pads_minutes_but_not_day(self):
        # 2024-03-05 09:07:00 UTC -> "Mar 5, 09:07" (unpadded day, padded minutes).
        epoch_ms = 1_709_629_620_000
        assert format_last_sync(epoch_ms) == "Mar 5, 09:07"


class TestFormatLastSyncIso:
    """Format an epoch-ms timestamp as an ISO-8601 UTC string."""

    def test_none_returns_empty_string(self):
        assert format_last_sync_iso(None) == ""

    def test_zero_returns_empty_string(self):
        assert format_last_sync_iso(0) == ""

    def test_formats_epoch_ms_as_iso(self):
        # 2024-01-15 14:30:00 UTC
        epoch_ms = 1_705_329_000_000
        assert format_last_sync_iso(epoch_ms) == "2024-01-15T14:30:00Z"

    def test_accepts_decimal(self):
        assert format_last_sync_iso(Decimal("1705329000000")) == "2024-01-15T14:30:00Z"
