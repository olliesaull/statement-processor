"""Tests for statement JSON disk caching in fetch_json_statement."""

import json
import os
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import utils.storage as storage_module
from utils.storage import StatementJSONNotFoundError, fetch_json_statement

TENANT_ID = "tenant-abc"
BUCKET = "test-bucket"
JSON_KEY = "tenant-abc/statements/stmt-001.json"
STATEMENT_ID = "stmt-001"
SAMPLE_DATA = {"statement_items": [{"description": "Item 1"}]}


@pytest.fixture(autouse=True)
def _patch_s3_and_dirs(monkeypatch, tmp_path):
    """Patch S3 client and LOCAL_DATA_DIR for every test."""
    fake_s3 = MagicMock()
    monkeypatch.setattr(storage_module, "s3_client", fake_s3)
    monkeypatch.setattr(storage_module, "S3_BUCKET_NAME", BUCKET)
    monkeypatch.setattr(storage_module, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_module, "STATEMENT_CACHE_TTL_SECONDS", 900)
    return fake_s3


@pytest.fixture()
def fake_s3(_patch_s3_and_dirs):
    """Return the fake S3 client for assertions."""
    return _patch_s3_and_dirs


def _setup_s3_success(fake_s3):
    """Configure fake S3 to return SAMPLE_DATA."""
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(SAMPLE_DATA).encode("utf-8")
    fake_s3.head_object.return_value = {}
    fake_s3.get_object.return_value = {"Body": body_mock}


def _setup_s3_not_found(fake_s3):
    """Configure fake S3 to raise 404."""
    fake_s3.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


class TestFetchJsonStatementCacheMiss:
    """When no cached file exists, fetch from S3 and write cache."""

    def test_fetches_from_s3_and_returns_data(self, fake_s3):
        _setup_s3_success(fake_s3)
        result = fetch_json_statement(tenant_id=TENANT_ID, bucket=BUCKET, json_key=JSON_KEY)
        assert result == SAMPLE_DATA
        fake_s3.head_object.assert_called_once()
        fake_s3.get_object.assert_called_once()

    def test_writes_cache_file_after_s3_fetch(self, fake_s3, tmp_path):
        _setup_s3_success(fake_s3)
        fetch_json_statement(tenant_id=TENANT_ID, bucket=BUCKET, json_key=JSON_KEY)
        cache_path = tmp_path / TENANT_ID / "statements" / f"{STATEMENT_ID}.json"
        assert cache_path.exists()
        assert json.loads(cache_path.read_text()) == SAMPLE_DATA


class TestFetchJsonStatementCacheHit:
    """When a fresh cached file exists, skip S3."""

    def test_returns_cached_data_without_s3_call(self, fake_s3, tmp_path):
        cache_dir = tmp_path / TENANT_ID / "statements"
        cache_dir.mkdir(parents=True)
        cache_file = cache_dir / f"{STATEMENT_ID}.json"
        cache_file.write_text(json.dumps(SAMPLE_DATA))
        result = fetch_json_statement(tenant_id=TENANT_ID, bucket=BUCKET, json_key=JSON_KEY)
        assert result == SAMPLE_DATA
        fake_s3.head_object.assert_not_called()
        fake_s3.get_object.assert_not_called()


class TestFetchJsonStatementCacheExpiry:
    """When the cached file is older than the TTL, re-fetch from S3."""

    def test_refetches_from_s3_when_cache_is_stale(self, fake_s3, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_module, "STATEMENT_CACHE_TTL_SECONDS", 1)
        cache_dir = tmp_path / TENANT_ID / "statements"
        cache_dir.mkdir(parents=True)
        cache_file = cache_dir / f"{STATEMENT_ID}.json"
        cache_file.write_text(json.dumps({"old": True}))
        # Backdate the file modification time
        old_time = time.time() - 10
        os.utime(cache_file, (old_time, old_time))
        updated_data = {"statement_items": [{"description": "Updated"}]}
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(updated_data).encode("utf-8")
        fake_s3.head_object.return_value = {}
        fake_s3.get_object.return_value = {"Body": body_mock}
        result = fetch_json_statement(tenant_id=TENANT_ID, bucket=BUCKET, json_key=JSON_KEY)
        assert result == updated_data
        fake_s3.get_object.assert_called_once()


class TestFetchJsonStatementNotFound:
    """When S3 returns 404, raise StatementJSONNotFoundError."""

    def test_raises_not_found_error(self, fake_s3):
        _setup_s3_not_found(fake_s3)
        with pytest.raises(StatementJSONNotFoundError):
            fetch_json_statement(tenant_id=TENANT_ID, bucket=BUCKET, json_key=JSON_KEY)


# ---------------------------------------------------------------------------
# is_allowed_pdf
# ---------------------------------------------------------------------------

from io import BytesIO

from utils.storage import PDF_MAGIC, is_allowed_pdf


class TestIsAllowedPdf:
    """Validate uploads by extension, MIME type, and magic bytes."""

    def test_valid_pdf_without_stream(self):
        """Correct extension + MIME with no stream check passes."""
        assert is_allowed_pdf("report.pdf", "application/pdf") is True

    def test_valid_pdf_uppercase_extension(self):
        """Uppercase .PDF extension is also accepted."""
        assert is_allowed_pdf("report.PDF", "application/pdf") is True

    def test_rejects_wrong_extension(self):
        """Non-PDF extension is rejected even with correct MIME."""
        assert is_allowed_pdf("report.txt", "application/pdf") is False

    def test_rejects_wrong_mimetype(self):
        """Correct extension but wrong MIME is rejected."""
        assert is_allowed_pdf("report.pdf", "application/octet-stream") is False

    def test_rejects_both_wrong(self):
        """Both wrong extension and MIME are rejected."""
        assert is_allowed_pdf("image.png", "image/png") is False

    def test_valid_pdf_with_correct_magic_bytes(self):
        """Stream starting with %PDF- passes the magic byte check."""
        stream = BytesIO(PDF_MAGIC + b"-1.4 rest of file")
        assert is_allowed_pdf("doc.pdf", "application/pdf", stream=stream) is True
        # Stream position should be restored to 0.
        assert stream.tell() == 0

    def test_rejects_spoofed_pdf_with_wrong_magic_bytes(self):
        """PDF extension + MIME but wrong magic bytes is rejected."""
        stream = BytesIO(b"PK\x03\x04 this is a zip")
        assert is_allowed_pdf("fake.pdf", "application/pdf", stream=stream) is False
        # Stream position should still be restored.
        assert stream.tell() == 0

    def test_rejects_empty_stream(self):
        """Empty stream fails the magic byte check."""
        stream = BytesIO(b"")
        assert is_allowed_pdf("empty.pdf", "application/pdf", stream=stream) is False


# ---------------------------------------------------------------------------
# _clean_key_segment
# ---------------------------------------------------------------------------

from utils.storage import _clean_key_segment


class TestCleanKeySegment:
    """Validate and normalize S3 key segments."""

    def test_valid_segment_returned_stripped(self):
        assert _clean_key_segment("  tenant-abc  ", "tenant_id") == "tenant-abc"

    def test_raises_on_none(self):
        with pytest.raises(ValueError, match="tenant_id is required"):
            _clean_key_segment(None, "tenant_id")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError, match="statement_id is required"):
            _clean_key_segment("", "statement_id")

    def test_raises_on_whitespace_only(self):
        with pytest.raises(ValueError, match="tenant_id is required"):
            _clean_key_segment("   ", "tenant_id")

    def test_raises_on_forward_slash(self):
        with pytest.raises(ValueError, match="cannot contain path separators"):
            _clean_key_segment("ten/ant", "tenant_id")

    def test_raises_on_backslash(self):
        with pytest.raises(ValueError, match="cannot contain path separators"):
            _clean_key_segment("ten\\ant", "tenant_id")


# ---------------------------------------------------------------------------
# upload_statement_to_s3
# ---------------------------------------------------------------------------

from botocore.exceptions import BotoCoreError

from utils.storage import upload_statement_to_s3


class TestUploadStatementToS3:
    """Upload a file-like object to S3."""

    def test_successful_upload_returns_true(self, fake_s3):
        """Normal upload returns True and calls upload_fileobj."""
        stream = BytesIO(b"pdf content")
        result = upload_statement_to_s3(stream, "tenant/statements/stmt.pdf")
        assert result is True
        fake_s3.upload_fileobj.assert_called_once()
        call_kwargs = fake_s3.upload_fileobj.call_args[1]
        assert call_kwargs["Key"] == "tenant/statements/stmt.pdf"
        assert call_kwargs["Bucket"] == BUCKET

    def test_resets_stream_position_before_upload(self, fake_s3):
        """Stream is seeked to 0 before uploading."""
        stream = BytesIO(b"pdf content")
        stream.seek(5)  # Move away from start
        upload_statement_to_s3(stream, "key")
        # The stream should have been reset — verify upload_fileobj received it
        fake_s3.upload_fileobj.assert_called_once()

    def test_returns_false_on_client_error(self, fake_s3):
        """ClientError during upload returns False."""
        fake_s3.upload_fileobj.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "PutObject")
        stream = BytesIO(b"data")
        result = upload_statement_to_s3(stream, "key")
        assert result is False

    def test_returns_false_on_botocore_error(self, fake_s3):
        """BotoCoreError during upload returns False."""
        fake_s3.upload_fileobj.side_effect = BotoCoreError()
        stream = BytesIO(b"data")
        result = upload_statement_to_s3(stream, "key")
        assert result is False

    def test_uses_stream_attribute_if_present(self, fake_s3):
        """File-like objects with a .stream attribute use the inner stream."""
        inner_stream = BytesIO(b"inner content")
        wrapper = type("FsLike", (), {"stream": inner_stream})()
        upload_statement_to_s3(wrapper, "key")
        # The inner stream should have been passed
        call_kwargs = fake_s3.upload_fileobj.call_args[1]
        assert call_kwargs["Fileobj"] is inner_stream
