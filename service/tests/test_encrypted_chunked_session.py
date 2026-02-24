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
