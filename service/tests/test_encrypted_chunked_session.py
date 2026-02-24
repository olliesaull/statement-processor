"""Unit tests for the encrypted chunked cookie session interface."""

import time
from dataclasses import dataclass

from cryptography.fernet import Fernet
from flask import Flask, jsonify, request, session
from flask.testing import FlaskClient

from utils.encrypted_chunked_session import EncryptedChunkedSessionInterface


@dataclass
class SessionHarness:
    """
    Bundle the app and mutable clock for session-interface tests.

    This helper keeps per-test setup compact while still exposing the
    deterministic time source used for TTL checks.

    Attributes:
        app: Flask app configured with the encrypted chunked session interface.
        clock: Mutable integer clock value used by the test interface.
    """

    app: Flask
    clock: dict[str, int]


def _build_test_harness(ttl_seconds: int = 900, chunk_size: int = 3700, max_chunks: int = 8) -> SessionHarness:
    """
    Build a Flask test app configured with encrypted chunked sessions.

    Args:
        ttl_seconds: TTL used for Fernet decrypt checks and cookie max-age.
        chunk_size: Cookie chunk size to force/avoid overflow in tests.
        max_chunks: Maximum allowed chunk count.

    Returns:
        Configured test harness with mutable clock state.
    """
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="unit-test-secret", SESSION_COOKIE_NAME="session", SESSION_COOKIE_SECURE=False, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    clock: dict[str, int] = {"now": int(time.time())}
    app.session_interface = EncryptedChunkedSessionInterface(
        fernet_key=Fernet.generate_key().decode("utf-8"), ttl_seconds=ttl_seconds, chunk_size=chunk_size, max_chunks=max_chunks, time_provider=lambda: clock["now"]
    )

    @app.get("/session/set")
    def set_session_value():
        session["payload"] = request.args.get("value", "")
        return jsonify({"ok": True})

    @app.get("/session/get")
    def get_session_value():
        return jsonify({"payload": session.get("payload")})

    @app.get("/session/clear")
    def clear_session_value():
        session.clear()
        return jsonify({"ok": True})

    return SessionHarness(app=app, clock=clock)


def _set_cookie_headers(response) -> list[str]:
    """
    Return all Set-Cookie header values from a response.

    Args:
        response: Flask response object.

    Returns:
        Set-Cookie header list.
    """
    return response.headers.getlist("Set-Cookie")


def test_single_cookie_round_trip() -> None:
    harness = _build_test_harness(chunk_size=4000)
    client: FlaskClient = harness.app.test_client()

    set_response = client.get("/session/set", query_string={"value": "small-payload"})
    set_headers = _set_cookie_headers(set_response)
    assert any("session=v1.1." in header for header in set_headers)
    assert all("session.1=" not in header for header in set_headers)

    get_response = client.get("/session/get")
    assert get_response.get_json() == {"payload": "small-payload"}


def test_overflow_uses_numbered_sibling_cookies() -> None:
    harness = _build_test_harness(chunk_size=80, max_chunks=50)
    client: FlaskClient = harness.app.test_client()
    long_payload = "x" * 1000

    set_response = client.get("/session/set", query_string={"value": long_payload})
    set_headers = _set_cookie_headers(set_response)
    assert any("session=v1." in header for header in set_headers)
    assert any("session.1=" in header for header in set_headers)

    get_response = client.get("/session/get")
    assert get_response.get_json() == {"payload": long_payload}


def test_missing_sibling_cookie_invalidates_session_and_clears_family() -> None:
    harness = _build_test_harness(chunk_size=70, max_chunks=50)
    client: FlaskClient = harness.app.test_client()
    long_payload = "y" * 1200
    client.get("/session/set", query_string={"value": long_payload})

    assert client.get_cookie("session.1") is not None
    client.delete_cookie("session.1")

    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_tampered_primary_cookie_invalidates_session() -> None:
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()
    client.get("/session/set", query_string={"value": "original"})

    raw_cookie = client.get_cookie("session")
    assert raw_cookie is not None
    replacement_char = "A" if raw_cookie.value[-1] != "A" else "B"
    client.set_cookie("session", f"{raw_cookie.value[:-1]}{replacement_char}")

    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_expired_cookie_is_rejected_by_ttl() -> None:
    harness = _build_test_harness(ttl_seconds=900)
    client: FlaskClient = harness.app.test_client()
    client.get("/session/set", query_string={"value": "ttl-value"})

    harness.clock["now"] += 901
    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_empty_session_clears_all_cookies() -> None:
    """Test that clearing session removes all cookie chunks."""
    harness = _build_test_harness(chunk_size=70, max_chunks=50)
    client: FlaskClient = harness.app.test_client()
    long_payload = "z" * 1200
    client.get("/session/set", query_string={"value": long_payload})

    assert client.get_cookie("session") is not None
    assert client.get_cookie("session.1") is not None

    response = client.get("/session/clear")
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)
    assert any(header.startswith("session.1=;") for header in headers)


def test_invalid_cookie_version_invalidates_session() -> None:
    """Test that tampering with cookie version invalidates session."""
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()
    client.get("/session/set", query_string={"value": "test-value"})
    # Tamper with version
    raw_cookie = client.get_cookie("session")
    assert raw_cookie is not None
    parts = raw_cookie.value.split(".", 2)
    tampered_cookie = f"v2.{parts[1]}.{parts[2]}"
    client.set_cookie("session", tampered_cookie)
    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_invalid_chunk_count_invalidates_session() -> None:
    """Test that non-numeric chunk count invalidates session."""
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()
    client.get("/session/set", query_string={"value": "test-value"})

    # Tamper with chunk count to non-numeric value
    raw_cookie = client.get_cookie("session")
    assert raw_cookie is not None
    parts = raw_cookie.value.split(".", 2)
    tampered_cookie = f"{parts[0]}.abc.{parts[2]}"
    client.set_cookie("session", tampered_cookie)

    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_chunk_count_exceeds_max_invalidates_session() -> None:
    """Test that chunk count exceeding max_chunks invalidates session."""
    harness = _build_test_harness(max_chunks=3)
    client: FlaskClient = harness.app.test_client()
    client.get("/session/set", query_string={"value": "test-value"})

    # Tamper with chunk count to exceed max_chunks
    raw_cookie = client.get_cookie("session")
    assert raw_cookie is not None
    parts = raw_cookie.value.split(".", 2)
    tampered_cookie = f"{parts[0]}.999.{parts[2]}"
    client.set_cookie("session", tampered_cookie)

    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_malformed_primary_cookie_invalidates_session() -> None:
    """Test that malformed cookie structure invalidates session."""
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()

    # Set malformed cookie without proper structure
    client.set_cookie("session", "malformed-cookie-value")

    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}
    headers = _set_cookie_headers(response)
    assert any(header.startswith("session=;") for header in headers)


def test_session_with_multiple_keys() -> None:
    """Test that sessions with multiple keys are preserved correctly."""
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()

    # Manually set multiple session keys
    with client.session_transaction() as sess:
        sess["user_id"] = "user123"
        sess["username"] = "testuser"
        sess["roles"] = ["admin", "editor"]
        sess["preferences"] = {"theme": "dark", "language": "en"}

    # Verify all keys are preserved
    with client.session_transaction() as sess:
        assert sess["user_id"] == "user123"
        assert sess["username"] == "testuser"
        assert sess["roles"] == ["admin", "editor"]
        assert sess["preferences"] == {"theme": "dark", "language": "en"}


def test_invalid_sibling_cookie_name_is_cleaned_up() -> None:
    """Test that invalid sibling cookie names are cleaned up."""
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()
    client.get("/session/set", query_string={"value": "test-value"})

    # Add invalid sibling cookie with non-numeric suffix
    client.set_cookie("session.abc", "invalid-sibling")

    # Trigger save_session which should clean up invalid siblings
    response = client.get("/session/set", query_string={"value": "updated-value"})
    headers = _set_cookie_headers(response)

    # Invalid sibling should be deleted
    assert any(header.startswith("session.abc=;") for header in headers)


def test_no_primary_cookie_returns_empty_session() -> None:
    """Test that missing primary cookie returns empty session."""
    harness = _build_test_harness()
    client: FlaskClient = harness.app.test_client()

    # Request without any session cookie
    response = client.get("/session/get")
    assert response.get_json() == {"payload": None}

    # No Set-Cookie headers should be present for empty unmodified session
    headers = _set_cookie_headers(response)
    assert len(headers) == 0


def test_constructor_rejects_invalid_ttl() -> None:
    """Test that constructor validates TTL parameter."""
    try:
        EncryptedChunkedSessionInterface(fernet_key=Fernet.generate_key().decode("utf-8"), ttl_seconds=0)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "ttl_seconds must be greater than zero" in str(e)

    try:
        EncryptedChunkedSessionInterface(fernet_key=Fernet.generate_key().decode("utf-8"), ttl_seconds=-100)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "ttl_seconds must be greater than zero" in str(e)


def test_constructor_rejects_invalid_chunk_size() -> None:
    """Test that constructor validates chunk_size parameter."""
    try:
        EncryptedChunkedSessionInterface(fernet_key=Fernet.generate_key().decode("utf-8"), chunk_size=0)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "chunk_size must be greater than zero" in str(e)

    try:
        EncryptedChunkedSessionInterface(fernet_key=Fernet.generate_key().decode("utf-8"), chunk_size=-100)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "chunk_size must be greater than zero" in str(e)


def test_constructor_rejects_invalid_max_chunks() -> None:
    """Test that constructor validates max_chunks parameter."""
    try:
        EncryptedChunkedSessionInterface(fernet_key=Fernet.generate_key().decode("utf-8"), max_chunks=0)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "max_chunks must be greater than zero" in str(e)

    try:
        EncryptedChunkedSessionInterface(fernet_key=Fernet.generate_key().decode("utf-8"), max_chunks=-100)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "max_chunks must be greater than zero" in str(e)


def test_constructor_rejects_empty_fernet_key() -> None:
    """Test that constructor validates fernet_key parameter."""
    try:
        EncryptedChunkedSessionInterface(fernet_key="")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "non-empty Fernet key is required" in str(e)

    try:
        EncryptedChunkedSessionInterface(fernet_key="   ")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "non-empty Fernet key is required" in str(e)
