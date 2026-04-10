"""Tests for per-contact Xero data loading and sync-time indexing.

Verifies that:
- get_xero_data_by_contact loads a per-contact JSON file from local/S3.
- Falls back to loading full datasets and filtering when per-contact file
  is missing (backward compatibility for pre-migration tenants).
- build_per_contact_index groups invoices, credit notes, and payments
  by contact_id and writes individual JSON files.
- Items without a contact_id are excluded from per-contact files.
- Malformed per-contact JSON triggers fallback to full datasets.
"""

import json
import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import sync as sync_module
import xero_repository as xero_module
from sync import build_per_contact_index
from xero_repository import get_xero_data_by_contact

TENANT_ID = "tenant-xbc-test"
CONTACT_ID = "contact-001"


def _mock_s3_with_nosuchkey():
    """Build a mock S3 client with a NoSuchKey exception class.

    Uses the same class for both the exceptions attribute and raising,
    so ``except s3_client.exceptions.NoSuchKey`` catches correctly.
    """
    no_such_key_cls = type("NoSuchKey", (Exception,), {})
    mock_s3 = MagicMock()
    mock_s3.exceptions = MagicMock()
    mock_s3.exceptions.NoSuchKey = no_such_key_cls
    return mock_s3, no_such_key_cls


@pytest.fixture()
def _data_dir(tmp_path, monkeypatch):
    """Point LOCAL_DATA_DIR at a temp directory and set up test data."""
    monkeypatch.setattr(xero_module, "LOCAL_DATA_DIR", str(tmp_path))
    return tmp_path


class TestGetXeroDataByContact:
    """Load combined Xero data for a single contact."""

    def test_returns_per_contact_file_when_present(self, _data_dir, monkeypatch):
        """When xero_by_contact/{contact_id}.json exists locally, use it."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})

        # Write a per-contact file.
        contact_dir = _data_dir / TENANT_ID / "xero_by_contact"
        contact_dir.mkdir(parents=True)
        per_contact_data = {"invoices": [{"invoice_id": "inv-1", "contact_id": CONTACT_ID}], "credit_notes": [], "payments": [{"payment_id": "pay-1", "contact_id": CONTACT_ID}]}
        (contact_dir / f"{CONTACT_ID}.json").write_text(json.dumps(per_contact_data))

        result = get_xero_data_by_contact(CONTACT_ID, tenant_id=TENANT_ID)
        assert result == per_contact_data

    def test_falls_back_to_full_datasets_when_per_contact_missing(self, _data_dir, monkeypatch):
        """When per-contact file is missing, load full datasets and filter."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})

        # Write full flat files with data for multiple contacts.
        tenant_dir = _data_dir / TENANT_ID
        tenant_dir.mkdir(parents=True)
        invoices = [{"invoice_id": "inv-1", "contact_id": CONTACT_ID}, {"invoice_id": "inv-2", "contact_id": "other-contact"}]
        credit_notes = [{"credit_note_id": "cn-1", "contact_id": CONTACT_ID}, {"credit_note_id": "cn-2", "contact_id": "other-contact"}]
        payments = [{"payment_id": "pay-1", "contact_id": CONTACT_ID}, {"payment_id": "pay-2", "contact_id": "other-contact"}]
        (tenant_dir / "invoices.json").write_text(json.dumps(invoices))
        (tenant_dir / "credit_notes.json").write_text(json.dumps(credit_notes))
        (tenant_dir / "payments.json").write_text(json.dumps(payments))

        mock_s3, no_such_key_cls = _mock_s3_with_nosuchkey()
        # Per-contact file not in S3 either.
        mock_s3.download_file.side_effect = no_such_key_cls("key not found")
        monkeypatch.setattr(xero_module, "s3_client", mock_s3)

        result = get_xero_data_by_contact(CONTACT_ID, tenant_id=TENANT_ID)

        # Should only contain data for the requested contact.
        assert len(result["invoices"]) == 1
        assert result["invoices"][0]["invoice_id"] == "inv-1"
        assert len(result["credit_notes"]) == 1
        assert result["credit_notes"][0]["credit_note_id"] == "cn-1"
        assert len(result["payments"]) == 1
        assert result["payments"][0]["payment_id"] == "pay-1"

    def test_downloads_from_s3_when_not_cached_locally(self, _data_dir, monkeypatch):
        """When local per-contact file is missing, download from S3."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})

        per_contact_data = {"invoices": [{"invoice_id": "inv-1", "contact_id": CONTACT_ID}], "credit_notes": [], "payments": []}

        def fake_download(bucket, key, path):
            """Simulate S3 download by writing the per-contact file."""
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(per_contact_data, f)

        mock_s3, _ = _mock_s3_with_nosuchkey()
        mock_s3.download_file = fake_download
        monkeypatch.setattr(xero_module, "s3_client", mock_s3)

        result = get_xero_data_by_contact(CONTACT_ID, tenant_id=TENANT_ID)
        assert result == per_contact_data

    def test_s3_nosuchkey_triggers_fallback(self, _data_dir, monkeypatch):
        """S3 NoSuchKey should return None from _load_per_contact_file, triggering fallback."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})

        # Write full flat files so the fallback has data.
        tenant_dir = _data_dir / TENANT_ID
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "invoices.json").write_text(json.dumps([{"invoice_id": "inv-1", "contact_id": CONTACT_ID}]))
        (tenant_dir / "credit_notes.json").write_text("[]")
        (tenant_dir / "payments.json").write_text("[]")

        mock_s3, no_such_key_cls = _mock_s3_with_nosuchkey()
        mock_s3.download_file.side_effect = no_such_key_cls("key not found")
        monkeypatch.setattr(xero_module, "s3_client", mock_s3)

        result = get_xero_data_by_contact(CONTACT_ID, tenant_id=TENANT_ID)
        assert len(result["invoices"]) == 1
        assert result["invoices"][0]["invoice_id"] == "inv-1"

    def test_returns_empty_when_no_tenant(self, monkeypatch):
        """Returns empty data when no tenant is selected."""
        monkeypatch.setattr(xero_module, "session", {})
        result = get_xero_data_by_contact(CONTACT_ID)
        assert result == {"invoices": [], "credit_notes": [], "payments": []}

    def test_returns_empty_when_no_contact_id(self, _data_dir, monkeypatch):
        """Returns empty data when contact_id is empty."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})
        result = get_xero_data_by_contact("")
        assert result == {"invoices": [], "credit_notes": [], "payments": []}

    def test_returns_empty_when_contact_id_is_none(self, _data_dir, monkeypatch):
        """Returns empty data when contact_id is None."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})
        result = get_xero_data_by_contact(None)
        assert result == {"invoices": [], "credit_notes": [], "payments": []}

    def test_falls_back_when_s3_download_raises_generic_error(self, _data_dir, monkeypatch):
        """S3 errors other than NoSuchKey should fall back to full datasets."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})

        # Write full flat files so the fallback has data.
        tenant_dir = _data_dir / TENANT_ID
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "invoices.json").write_text(json.dumps([{"invoice_id": "inv-1", "contact_id": CONTACT_ID}]))
        (tenant_dir / "credit_notes.json").write_text("[]")
        (tenant_dir / "payments.json").write_text("[]")

        # S3 download raises a generic error (network failure, etc.).
        mock_s3, _ = _mock_s3_with_nosuchkey()
        mock_s3.download_file.side_effect = OSError("network error")
        monkeypatch.setattr(xero_module, "s3_client", mock_s3)

        result = get_xero_data_by_contact(CONTACT_ID, tenant_id=TENANT_ID)
        assert len(result["invoices"]) == 1

    def test_falls_back_when_per_contact_file_is_corrupt(self, _data_dir, monkeypatch):
        """Malformed per-contact JSON must fall back to full datasets without raising."""
        monkeypatch.setattr(xero_module, "session", {"xero_tenant_id": TENANT_ID})

        # Write a corrupt per-contact file.
        contact_dir = _data_dir / TENANT_ID / "xero_by_contact"
        contact_dir.mkdir(parents=True)
        (contact_dir / f"{CONTACT_ID}.json").write_text("{not valid json")

        # Write full flat files so the fallback has data.
        tenant_dir = _data_dir / TENANT_ID
        (tenant_dir / "invoices.json").write_text(json.dumps([{"invoice_id": "inv-1", "contact_id": CONTACT_ID}]))
        (tenant_dir / "credit_notes.json").write_text("[]")
        (tenant_dir / "payments.json").write_text("[]")

        mock_s3, _ = _mock_s3_with_nosuchkey()
        monkeypatch.setattr(xero_module, "s3_client", mock_s3)

        result = get_xero_data_by_contact(CONTACT_ID, tenant_id=TENANT_ID)
        assert len(result["invoices"]) == 1
        assert result["invoices"][0]["invoice_id"] == "inv-1"


class TestBuildPerContactIndex:
    """Sync-time indexing: group Xero data by contact and write per-contact files."""

    def test_creates_per_contact_files_from_flat_datasets(self, tmp_path, monkeypatch):
        """Reads flat files, groups by contact_id, writes per-contact JSON files."""
        monkeypatch.setattr(sync_module, "LOCAL_DATA_DIR", str(tmp_path))
        mock_s3 = MagicMock()
        monkeypatch.setattr(sync_module, "s3_client", mock_s3)
        monkeypatch.setattr(sync_module, "S3_BUCKET_NAME", "test-bucket")

        # Write flat datasets with two contacts.
        tenant_dir = tmp_path / TENANT_ID
        tenant_dir.mkdir()
        invoices = [{"invoice_id": "inv-1", "contact_id": "c1"}, {"invoice_id": "inv-2", "contact_id": "c2"}, {"invoice_id": "inv-3", "contact_id": "c1"}]
        credit_notes = [{"credit_note_id": "cn-1", "contact_id": "c1"}]
        payments = [{"payment_id": "pay-1", "contact_id": "c2"}]
        (tenant_dir / "invoices.json").write_text(json.dumps(invoices))
        (tenant_dir / "credit_notes.json").write_text(json.dumps(credit_notes))
        (tenant_dir / "payments.json").write_text(json.dumps(payments))

        build_per_contact_index(TENANT_ID)

        # Verify per-contact files were created.
        contact_dir = tenant_dir / "xero_by_contact"
        assert contact_dir.exists()

        c1_data = json.loads((contact_dir / "c1.json").read_text())
        assert len(c1_data["invoices"]) == 2
        assert len(c1_data["credit_notes"]) == 1
        assert len(c1_data["payments"]) == 0

        c2_data = json.loads((contact_dir / "c2.json").read_text())
        assert len(c2_data["invoices"]) == 1
        assert len(c2_data["credit_notes"]) == 0
        assert len(c2_data["payments"]) == 1

    def test_uploads_per_contact_files_to_s3(self, tmp_path, monkeypatch):
        """Each per-contact file should be uploaded to S3."""
        monkeypatch.setattr(sync_module, "LOCAL_DATA_DIR", str(tmp_path))
        mock_s3 = MagicMock()
        monkeypatch.setattr(sync_module, "s3_client", mock_s3)
        monkeypatch.setattr(sync_module, "S3_BUCKET_NAME", "test-bucket")

        tenant_dir = tmp_path / TENANT_ID
        tenant_dir.mkdir()
        (tenant_dir / "invoices.json").write_text(json.dumps([{"invoice_id": "inv-1", "contact_id": "c1"}]))
        (tenant_dir / "credit_notes.json").write_text(json.dumps([]))
        (tenant_dir / "payments.json").write_text(json.dumps([]))

        build_per_contact_index(TENANT_ID)

        # Verify S3 upload was called for the contact file.
        mock_s3.upload_file.assert_called_once()
        call_args = mock_s3.upload_file.call_args[0]
        assert call_args[1] == "test-bucket"
        assert call_args[2] == f"{TENANT_ID}/data/xero_by_contact/c1.json"

    def test_handles_empty_datasets_gracefully(self, tmp_path, monkeypatch):
        """No per-contact files should be created when all datasets are empty."""
        monkeypatch.setattr(sync_module, "LOCAL_DATA_DIR", str(tmp_path))
        mock_s3 = MagicMock()
        monkeypatch.setattr(sync_module, "s3_client", mock_s3)
        monkeypatch.setattr(sync_module, "S3_BUCKET_NAME", "test-bucket")

        tenant_dir = tmp_path / TENANT_ID
        tenant_dir.mkdir()
        (tenant_dir / "invoices.json").write_text("[]")
        (tenant_dir / "credit_notes.json").write_text("[]")
        (tenant_dir / "payments.json").write_text("[]")

        build_per_contact_index(TENANT_ID)

        # No S3 uploads should happen.
        mock_s3.upload_file.assert_not_called()

    def test_handles_missing_flat_files(self, tmp_path, monkeypatch):
        """Should not crash if flat dataset files are missing."""
        monkeypatch.setattr(sync_module, "LOCAL_DATA_DIR", str(tmp_path))
        mock_s3 = MagicMock()
        monkeypatch.setattr(sync_module, "s3_client", mock_s3)
        monkeypatch.setattr(sync_module, "S3_BUCKET_NAME", "test-bucket")

        tenant_dir = tmp_path / TENANT_ID
        tenant_dir.mkdir()
        # No flat files written — should handle gracefully.

        build_per_contact_index(TENANT_ID)

        mock_s3.upload_file.assert_not_called()

    def test_items_without_contact_id_are_excluded(self, tmp_path, monkeypatch):
        """Items with missing or None contact_id must not appear in any per-contact file."""
        monkeypatch.setattr(sync_module, "LOCAL_DATA_DIR", str(tmp_path))
        mock_s3 = MagicMock()
        monkeypatch.setattr(sync_module, "s3_client", mock_s3)
        monkeypatch.setattr(sync_module, "S3_BUCKET_NAME", "test-bucket")

        tenant_dir = tmp_path / TENANT_ID
        tenant_dir.mkdir()
        invoices = [
            {"invoice_id": "inv-1", "contact_id": "c1"},
            {"invoice_id": "inv-2"},  # missing key entirely
            {"invoice_id": "inv-3", "contact_id": None},  # explicit None
            {"invoice_id": "inv-4", "contact_id": ""},  # empty string
        ]
        (tenant_dir / "invoices.json").write_text(json.dumps(invoices))
        (tenant_dir / "credit_notes.json").write_text("[]")
        (tenant_dir / "payments.json").write_text("[]")

        build_per_contact_index(TENANT_ID)

        contact_dir = tenant_dir / "xero_by_contact"
        created = list(contact_dir.iterdir())
        assert len(created) == 1, "Only one contact file should be created"
        assert created[0].name == "c1.json"
        c1 = json.loads(created[0].read_text())
        assert len(c1["invoices"]) == 1
        assert c1["invoices"][0]["invoice_id"] == "inv-1"
        # Only one S3 upload — not two or three.
        assert mock_s3.upload_file.call_count == 1
