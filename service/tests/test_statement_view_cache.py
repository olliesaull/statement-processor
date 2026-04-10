"""Tests for statement view cache — Redis caching of precomputed statement detail data.

Verifies that:
- Cache operations (get/set/invalidate) work correctly with Redis.
- Redis errors are handled gracefully without crashing.
- The statement route skips the pipeline on cache hit and rebuilds on miss.
- POST actions invalidate the cache.
"""

import json
import tempfile
from unittest.mock import MagicMock, call, patch

import pytest
from cachelib import FileSystemCache
from flask_session import Session

import app as app_module
import statement_view_cache as cache_module
import utils.auth
from statement_view_cache import _cache_key, cache_statement_view, get_cached_statement_view, invalidate_statement_view_cache

TENANT_ID = "tenant-cache-test"
STATEMENT_ID = "stmt-cache-001"
SAMPLE_VIEW_DATA = {"statement_rows": [{"statement_item_id": "item-1", "is_completed": False, "item_type": "invoice"}], "display_headers": ["Number", "Date", "Amount"]}
SAMPLE_ITEMS = [{"statement_item_id": "item-1", "columns": {"Number": "INV-001", "Date": "2025-01-15", "Amount": "100.00"}}]
SAMPLE_STATEMENT_JSON = {"statement_items": SAMPLE_ITEMS, "header_mapping": {"Number": "number", "Date": "date", "Amount": "amount"}}
SAMPLE_RECORD = {"TenantID": TENANT_ID, "StatementID": STATEMENT_ID, "ContactName": "Test Contact", "ContactID": "contact-001", "Completed": "false", "TokenReservationStatus": "released"}


class TestCacheKey:
    """Verify cache key format follows the spec convention."""

    def test_key_format(self):
        """Key must be stmt_view:{tenant_id}:{statement_id}."""
        key = _cache_key(TENANT_ID, STATEMENT_ID)
        assert key == f"stmt_view:{TENANT_ID}:{STATEMENT_ID}"


class TestGetCachedStatementView:
    """Retrieve cached statement view data from Redis."""

    def test_returns_none_on_cache_miss(self):
        """Cache miss returns None so the caller runs the full pipeline."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch.object(cache_module, "_redis", mock_redis):
            result = get_cached_statement_view(TENANT_ID, STATEMENT_ID)
        assert result is None

    def test_returns_parsed_data_on_cache_hit(self):
        """Cache hit returns the deserialized view data dict."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps(SAMPLE_VIEW_DATA).encode()
        with patch.object(cache_module, "_redis", mock_redis):
            result = get_cached_statement_view(TENANT_ID, STATEMENT_ID)
        assert result == SAMPLE_VIEW_DATA

    def test_returns_none_on_redis_error(self):
        """Redis errors must not crash — return None and let the pipeline run."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = ConnectionError("Redis down")
        with patch.object(cache_module, "_redis", mock_redis):
            result = get_cached_statement_view(TENANT_ID, STATEMENT_ID)
        assert result is None


class TestCacheStatementView:
    """Store statement view data in Redis with a TTL."""

    def test_stores_serialized_data_with_ttl(self):
        """Data must be JSON-serialized and stored with 120s TTL."""
        mock_redis = MagicMock()
        with patch.object(cache_module, "_redis", mock_redis):
            cache_statement_view(TENANT_ID, STATEMENT_ID, SAMPLE_VIEW_DATA)
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert args[0] == _cache_key(TENANT_ID, STATEMENT_ID)
        assert args[1] == 120
        assert json.loads(args[2]) == SAMPLE_VIEW_DATA

    def test_does_not_raise_on_redis_error(self):
        """Redis errors must not crash — cache write failure is non-fatal."""
        mock_redis = MagicMock()
        mock_redis.setex.side_effect = ConnectionError("Redis down")
        with patch.object(cache_module, "_redis", mock_redis):
            # Should not raise
            cache_statement_view(TENANT_ID, STATEMENT_ID, SAMPLE_VIEW_DATA)


class TestInvalidateStatementViewCache:
    """Remove cached statement view data from Redis."""

    def test_deletes_cache_key(self):
        """Invalidation must delete the exact cache key."""
        mock_redis = MagicMock()
        with patch.object(cache_module, "_redis", mock_redis):
            invalidate_statement_view_cache(TENANT_ID, STATEMENT_ID)
        mock_redis.delete.assert_called_once_with(_cache_key(TENANT_ID, STATEMENT_ID))

    def test_does_not_raise_on_redis_error(self):
        """Redis errors must not crash — invalidation failure is non-fatal."""
        mock_redis = MagicMock()
        mock_redis.delete.side_effect = ConnectionError("Redis down")
        with patch.object(cache_module, "_redis", mock_redis):
            # Should not raise
            invalidate_statement_view_cache(TENANT_ID, STATEMENT_ID)


# ---------------------------------------------------------------------------
# Integration tests: verify the statement route uses the cache correctly.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _app():
    """Module-scoped Flask app with file-based sessions (no real Redis)."""
    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_cache_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-key-cache")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Test client with auth bypass, stubbed data layer, and mocked cache."""
    from tenant_data_repository import TenantStatus

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "get_tenant_status", lambda tenant_id: TenantStatus.FREE)

    # Stub data layer — same pattern as test_statement_htmx.py.
    monkeypatch.setattr(app_module, "get_statement_record", lambda *a, **kw: SAMPLE_RECORD)
    monkeypatch.setattr(app_module, "fetch_json_statement", lambda *a, **kw: SAMPLE_STATEMENT_JSON)
    monkeypatch.setattr(app_module, "get_xero_data_by_contact", lambda *a, **kw: {"invoices": [], "credit_notes": [], "payments": []})
    monkeypatch.setattr(app_module, "get_statement_item_status_map", lambda *a, **kw: {})
    monkeypatch.setattr(app_module, "_persist_classification_updates", lambda **kw: None)

    # Default: cache miss (no cached view data).
    monkeypatch.setattr(app_module, "get_cached_statement_view", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "cache_statement_view", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "invalidate_statement_view_cache", lambda *a, **kw: None)

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = TENANT_ID
            sess["xero_user_email"] = "test@example.com"
            sess["xero_tenant_name"] = "Test Org Ltd"
        yield c


class TestStatementRouteCacheHit:
    """When the cache returns view data, the pipeline should be skipped."""

    def test_htmx_swap_skips_pipeline_on_cache_hit(self, client, monkeypatch):
        """HTMX GET with warm cache should not call fetch_json_statement."""
        # Provide cached view data so the pipeline is skipped.
        cached_data = {
            "statement_rows": [
                {
                    "statement_item_id": "item-1",
                    "is_completed": False,
                    "item_type": "invoice",
                    "cell_comparisons": [],
                    "matches": False,
                    "flags": {},
                    "item_type_label": "Invoice",
                    "xero_invoice_id": None,
                    "xero_credit_note_id": None,
                }
            ],
            "display_headers": ["Number", "Date", "Amount"],
        }
        monkeypatch.setattr(app_module, "get_cached_statement_view", lambda *a, **kw: cached_data)

        # Track whether the expensive pipeline function was called.
        pipeline_called = False
        original_fetch = app_module.fetch_json_statement

        def tracking_fetch(*args, **kwargs):
            nonlocal pipeline_called
            pipeline_called = True
            return original_fetch(*args, **kwargs)

        monkeypatch.setattr(app_module, "fetch_json_statement", tracking_fetch)

        response = client.get(f"/statement/{STATEMENT_ID}", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert not pipeline_called, "Pipeline should be skipped on cache hit"

    def test_cache_hit_renders_correct_content(self, client, monkeypatch):
        """Cache hit should still render the statement content partial."""
        cached_data = {
            "statement_rows": [
                {
                    "statement_item_id": "item-1",
                    "is_completed": False,
                    "item_type": "invoice",
                    "cell_comparisons": [],
                    "matches": False,
                    "flags": {},
                    "item_type_label": "Invoice",
                    "xero_invoice_id": None,
                    "xero_credit_note_id": None,
                }
            ],
            "display_headers": ["Number", "Date", "Amount"],
        }
        monkeypatch.setattr(app_module, "get_cached_statement_view", lambda *a, **kw: cached_data)

        response = client.get(f"/statement/{STATEMENT_ID}", headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="statement-content"' in html


class TestStatementRouteCacheMiss:
    """When the cache returns None, the pipeline should run and cache the result."""

    def test_cache_miss_stores_view_data(self, client, monkeypatch):
        """Cache miss should call cache_statement_view with the built data."""
        cache_calls = []
        monkeypatch.setattr(app_module, "cache_statement_view", lambda *a, **kw: cache_calls.append((a, kw)))

        response = client.get(f"/statement/{STATEMENT_ID}")
        assert response.status_code == 200
        assert len(cache_calls) == 1, "cache_statement_view should be called once on cache miss"
        # Verify the cached data includes statement_rows and display_headers.
        cached_args = cache_calls[0][0]
        assert cached_args[0] == TENANT_ID
        assert cached_args[1] == STATEMENT_ID
        view_data = cached_args[2]
        assert "statement_rows" in view_data
        assert "display_headers" in view_data


class TestStatementRouteCacheInvalidation:
    """POST actions that change item status should invalidate the cache."""

    def test_post_complete_item_invalidates_cache(self, client, monkeypatch):
        """POST complete_item should call invalidate_statement_view_cache."""
        monkeypatch.setattr(app_module, "set_statement_item_completed", lambda *a, **kw: None)

        invalidate_calls = []
        monkeypatch.setattr(app_module, "invalidate_statement_view_cache", lambda *a, **kw: invalidate_calls.append((a, kw)))

        response = client.post(
            f"/statement/{STATEMENT_ID}",
            data={"action": "complete_item", "statement_item_id": "item-1", "items_view": "incomplete", "show_payments": "true", "page": "1"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert len(invalidate_calls) == 1, "Cache should be invalidated on POST"
        assert invalidate_calls[0][0] == (TENANT_ID, STATEMENT_ID)
