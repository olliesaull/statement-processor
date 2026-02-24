"""
Encrypted chunked cookie session interface for Flask.

A Flask SessionInterface implementation that transparently splits encrypted,
compressed session data across multiple browser cookies, working around the 4 KB
per-cookie limit imposed by all major browsers (RFC 6265).

Motivation
----------
The default Flask session (SecureCookieSessionInterface) encodes the entire
session payload into a single signed cookie. Once that cookie exceeds ~4 KB the
browser silently drops it, which typically manifests as users being logged out
or losing session state without explanation.

A common trigger is storing large OAuth tokens (e.g. Xero, Google, Salesforce)
directly in the session. A Xero auth token is typically ~4 KB uncompressed and
~2 KB compressed, which already consumes half the cookie budget before any other
session data is stored.

Design Overview
---------------
1. The whole session dict is serialised to JSON, compressed with zlib, then
   encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
2. The resulting ciphertext string is split into chunk_size-sized slices.
3. Slices are stored in sequentially named cookies:
   - session      ← chunk 0 (always present when a session exists)
   - session.1    ← chunk 1
   - session.2    ← chunk 2
   - ...
4. On the next request all cookies are reassembled in order, decrypted, and
   deserialised back into an EncryptedChunkedSession object.

Security Properties
-------------------
* Single MAC covers the whole payload. Because we encrypt *then* split (rather
  than splitting then encrypting each chunk separately), Fernet's HMAC-SHA256
  authentication tag covers the complete session. A missing, reordered, or
  tampered chunk will cause InvalidToken to be raised during reassembly.
* Fernet provides authenticated encryption. Clients cannot read or forge
  session data without the server-side secret key.
* Compression is applied before encryption, which is safe here because the
  server controls both compression and encryption and no chosen-plaintext oracle
  is exposed to clients (contrast with CRIME/BREACH attacks on TLS).
"""

import logging
import time
import zlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Request, Response
from flask import request as flask_request
from flask.sessions import SecureCookieSession, SessionInterface, TaggedJSONSerializer

logger = logging.getLogger(__name__)


class EncryptedChunkedSession(SecureCookieSession):
    """
    Represents a decrypted Flask session with cleanup metadata.

    This model belongs to the web session subsystem and tracks whether the
    incoming cookie family should be deleted because it was invalid.

    Attributes:
        needs_cleanup: True when the incoming cookie data was malformed or failed decryption.
    """

    def __init__(self, initial: dict[str, Any] | None = None, needs_cleanup: bool = False) -> None:
        """
        Initialize the session payload and cleanup marker.

        Args:
            initial: Session key/value payload to hydrate.
            needs_cleanup: Whether existing cookies should be removed on save.

        Returns:
            None.
        """
        super().__init__(initial)
        self.needs_cleanup = needs_cleanup


class EncryptedChunkedSessionInterface(SessionInterface):
    """
    Stores Flask session state in encrypted, compressed, chunked browser cookies.

    This interface belongs to the service auth/session layer and replaces
    server-side session stores for Lambda-friendly stateless deployments.

    The session payload is JSON-serialised, zlib-compressed (level 9), then
    Fernet-encrypted before being split into cookie-sized chunks. Compression
    typically reduces OAuth tokens from ~4KB to ~2KB, significantly reducing
    the number of cookies required.

    Attributes:
        serializer: JSON serializer for Flask session payloads.
    """

    session_class = EncryptedChunkedSession

    def __init__(self, fernet_key: str, ttl_seconds: int = 900, chunk_size: int = 3700, max_chunks: int = 8, time_provider: Callable[[], int] | None = None) -> None:
        """
        Configure the encrypted cookie session interface.

        Args:
            fernet_key: Fernet key used for encryption and decryption.
            ttl_seconds: Session TTL applied both to Fernet decrypt checks and cookie max-age.
            chunk_size: Max payload chars per cookie chunk (conservative headroom under 4096-byte limit).
            max_chunks: Upper bound for chunk count to cap header growth.
            time_provider: Optional UNIX-time provider for deterministic tests.

        Returns:
            None.

        Raises:
            ValueError: When keys are missing or chunk settings are invalid.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero.")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero.")
        if max_chunks <= 0:
            raise ValueError("max_chunks must be greater than zero.")
        if not fernet_key or not fernet_key.strip():
            raise ValueError("A non-empty Fernet key is required.")

        self.serializer = TaggedJSONSerializer()
        self._fernet = self._build_fernet(fernet_key)
        self._ttl_seconds = ttl_seconds
        self._chunk_size = chunk_size
        self._max_chunks = max_chunks
        self._time_provider = time_provider or (lambda: int(time.time()))
        self._cookie_format_version = "v1"

    def open_session(self, app: Flask, request: Request) -> EncryptedChunkedSession:  # pylint: disable=too-many-return-statements
        """
        Load, reassemble, decrypt, decompress, and deserialize session cookies.

        Args:
            app: Flask application instance.
            request: Current request object.

        Returns:
            Parsed session object, or an empty cleanup-marked session when invalid.
        """
        cookie_name = self.get_cookie_name(app)
        primary_cookie_value = request.cookies.get(cookie_name)
        if not primary_cookie_value:
            return self.session_class()

        encrypted_payload = self._extract_encrypted_payload(cookie_name, primary_cookie_value, request)
        if encrypted_payload is None:
            logger.warning("EncryptedChunkedSession: failed to extract encrypted payload from cookies (possible tampering or malformed cookie structure)")
            return self.session_class(needs_cleanup=True)

        try:
            # Decrypt the reassembled ciphertext
            compressed_plaintext = self._fernet.decrypt_at_time(encrypted_payload.encode("utf-8"), ttl=self._ttl_seconds, current_time=self._time_provider())
            # Decompress the plaintext
            plaintext = zlib.decompress(compressed_plaintext)
        except InvalidToken:
            logger.warning("EncryptedChunkedSession: failed to decrypt session cookies (possible tampering, key rotation, or expired session)")
            return self.session_class(needs_cleanup=True)
        except zlib.error:
            logger.warning("EncryptedChunkedSession: failed to decompress session data (possible corruption)")
            return self.session_class(needs_cleanup=True)

        try:
            session_data = self.serializer.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            logger.warning("EncryptedChunkedSession: failed to deserialize session data (possible corruption)")
            return self.session_class(needs_cleanup=True)

        if not isinstance(session_data, dict):
            logger.warning("EncryptedChunkedSession: session data is not a dict (possible corruption)")
            return self.session_class(needs_cleanup=True)

        return self.session_class(session_data)

    def save_session(self, app: Flask, session: EncryptedChunkedSession, response: Response) -> None:
        """
        Compress, encrypt, and persist the session payload back into cookie chunks.

        Args:
            app: Flask application instance.
            session: Session data for the current request.
            response: Response to mutate with cookie headers.

        Returns:
            None.
        """
        # Always clean up invalid cookies first
        if getattr(session, "needs_cleanup", False):
            self._delete_cookie_family(app, response)

        # Empty session → delete all cookies
        if not session:
            if session.modified or getattr(session, "needs_cleanup", False):
                self._delete_cookie_family(app, response)
            return

        # Session unchanged → skip write to avoid unnecessary cookie churn
        if not self.should_set_cookie(app, session):
            return

        # Serialize, compress, encrypt, and chunk the session payload
        serialized_payload = self.serializer.dumps(dict(session))
        compressed_payload = zlib.compress(serialized_payload.encode("utf-8"), level=9)
        encrypted_payload = self._fernet.encrypt(compressed_payload).decode("utf-8")
        chunks = self._split_payload_into_chunks(encrypted_payload)
        chunk_count = len(chunks)

        if chunk_count > self._max_chunks:
            # Oversized sessions break requests due to header limits, so we fail closed and clear.
            logger.warning("EncryptedChunkedSession: session payload exceeded maximum chunk count (chunk_count=%d, max_chunks=%d); clearing session", chunk_count, self._max_chunks)
            self._delete_cookie_family(app, response)
            return

        # Delete stale overflow cookies from previously larger sessions
        self._delete_stale_sibling_cookies(app, response, current_chunk_count=chunk_count)

        # Write the primary cookie with version and chunk count metadata
        primary_cookie_name = self.get_cookie_name(app)
        primary_cookie_value = f"{self._cookie_format_version}.{chunk_count}.{chunks[0]}"
        self._set_cookie(response, primary_cookie_name, primary_cookie_value, app)

        # Write overflow cookies
        for index, chunk in enumerate(chunks[1:], start=1):
            self._set_cookie(response, f"{primary_cookie_name}.{index}", chunk, app)

    @staticmethod
    def _build_fernet(fernet_key: str) -> Fernet:
        """
        Build a Fernet instance from a key string.

        Args:
            fernet_key: Fernet key string.

        Returns:
            Fernet instance.
        """
        return Fernet(fernet_key.strip().encode("utf-8"))

    def _cookie_name(self, index: int) -> str:
        """
        Return the cookie name for chunk at given index.

        The naming convention is:
        - index 0  →  "session"
        - index 1  →  "session.1"
        - index 2  →  "session.2"

        Args:
            index: Zero-based chunk index (must be >= 0).

        Returns:
            Cookie name for the given chunk index.
        """
        base_name = self.get_cookie_name(None)  # Use Flask's default "session"
        return base_name if index == 0 else f"{base_name}.{index}"

    def _build_cookie_kwargs(self, app: Flask) -> dict[str, Any]:
        """
        Return a dict of cookie attributes shared by all chunk cookies.

        Centralizes the extraction of cookie settings from the Flask app config
        so that set_cookie and delete_cookie operations use identical attributes,
        avoiding mismatches that would leave orphaned cookies behind.

        Args:
            app: The current Flask application.

        Returns:
            Keyword arguments suitable for passing to Response.set_cookie or Response.delete_cookie.
        """
        return {
            "domain": self.get_cookie_domain(app),
            "path": self.get_cookie_path(app),
            "secure": self.get_cookie_secure(app),
            "httponly": self.get_cookie_httponly(app),
            "samesite": self.get_cookie_samesite(app),
        }

    def _extract_encrypted_payload(self, cookie_name: str, primary_cookie_value: str, request_obj: Request) -> str | None:
        """
        Parse and reassemble the encrypted payload from chunk cookies.

        Args:
            cookie_name: Base session cookie name.
            primary_cookie_value: Raw value stored in the primary cookie.
            request_obj: Current request with all cookie values.

        Returns:
            Reassembled encrypted payload string, or None when structure is invalid.
        """
        parts = primary_cookie_value.split(".", 2)
        if len(parts) != 3:
            return None

        version, raw_chunk_count, first_chunk = parts
        if version != self._cookie_format_version:
            return None

        try:
            chunk_count = int(raw_chunk_count)
        except ValueError:
            return None

        if chunk_count < 1 or chunk_count > self._max_chunks:
            return None

        chunks = [first_chunk]
        for index in range(1, chunk_count):
            sibling_cookie_name = f"{cookie_name}.{index}"
            sibling_value = request_obj.cookies.get(sibling_cookie_name)
            if not sibling_value:
                return None
            chunks.append(sibling_value)

        return "".join(chunks)

    def _split_payload_into_chunks(self, encrypted_payload: str) -> list[str]:
        """
        Split encrypted payload into fixed-size cookie-safe chunks.

        Args:
            encrypted_payload: Encrypted session token.

        Returns:
            Ordered chunk list.
        """
        return [encrypted_payload[index : index + self._chunk_size] for index in range(0, len(encrypted_payload), self._chunk_size)]

    def _set_cookie(self, response: Response, cookie_name: str, value: str, app: Flask) -> None:
        """
        Set a single secure session cookie with rolling expiry.

        Args:
            response: Response object to mutate.
            cookie_name: Cookie key to set.
            value: Cookie payload value.
            app: Flask application instance for cookie config lookups.

        Returns:
            None.
        """
        expires = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
        cookie_kwargs = self._build_cookie_kwargs(app)
        response.set_cookie(key=cookie_name, value=value, max_age=self._ttl_seconds, expires=expires, **cookie_kwargs)

    def _delete_stale_sibling_cookies(self, app: Flask, response: Response, current_chunk_count: int) -> None:
        """
        Delete no-longer-needed sibling chunk cookies from previous larger sessions.

        Args:
            app: Flask application instance for cookie settings.
            response: Response object to mutate.
            current_chunk_count: Number of chunks required by the current payload.

        Returns:
            None.
        """
        base_cookie_name = self.get_cookie_name(app)
        prefix = f"{base_cookie_name}."
        for cookie_name in flask_request.cookies:
            if not cookie_name.startswith(prefix):
                continue
            sibling_index_str = cookie_name[len(prefix) :]
            if not sibling_index_str.isdigit():
                self._delete_cookie(app, response, cookie_name)
                continue
            sibling_index = int(sibling_index_str)
            if sibling_index >= current_chunk_count:
                self._delete_cookie(app, response, cookie_name)

    def _delete_cookie_family(self, app: Flask, response: Response) -> None:
        """
        Delete the base session cookie and any sibling chunk cookies.

        Args:
            app: Flask application instance for cookie settings.
            response: Response object to mutate.

        Returns:
            None.
        """
        base_cookie_name = self.get_cookie_name(app)
        cookie_names = {base_cookie_name}
        cookie_names.update(self._iter_sibling_cookie_names(base_cookie_name, flask_request.cookies.keys()))
        for cookie_name in cookie_names:
            self._delete_cookie(app, response, cookie_name)

    def _delete_cookie(self, app: Flask, response: Response, cookie_name: str) -> None:
        """
        Delete a single cookie using the configured path/domain pair.

        Args:
            app: Flask application instance for cookie settings.
            response: Response object to mutate.
            cookie_name: Cookie key to remove.

        Returns:
            None.
        """
        cookie_kwargs = self._build_cookie_kwargs(app)
        response.delete_cookie(key=cookie_name, **cookie_kwargs)

    @staticmethod
    def _iter_sibling_cookie_names(base_cookie_name: str, cookie_names: Iterable[str]) -> set[str]:
        """
        Return all sibling cookie names matching `<base>.<index>`.

        Args:
            base_cookie_name: Base session cookie name.
            cookie_names: Available cookie keys.

        Returns:
            Matching sibling cookie names.
        """
        prefix = f"{base_cookie_name}."
        return {cookie_name for cookie_name in cookie_names if cookie_name.startswith(prefix)}
