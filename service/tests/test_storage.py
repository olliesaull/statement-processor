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
