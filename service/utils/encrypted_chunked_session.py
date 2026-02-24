"""Encrypted chunked cookie session interface for Flask."""

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Request, Response
from flask import request as flask_request
from flask.sessions import SecureCookieSession, SessionInterface, TaggedJSONSerializer


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
    Stores Flask session state in encrypted, chunked browser cookies.

    This interface belongs to the service auth/session layer and replaces
    server-side session stores for Lambda-friendly stateless deployments.

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
            chunk_size: Max payload chars per cookie chunk.
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

    def open_session(self, app: Flask, request: Request) -> EncryptedChunkedSession:
        """
        Load, reassemble, decrypt, and deserialize session cookies.

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
            return self.session_class(needs_cleanup=True)

        try:
            plaintext = self._fernet.decrypt_at_time(encrypted_payload.encode("utf-8"), ttl=self._ttl_seconds, current_time=self._time_provider())
        except InvalidToken:
            return self.session_class(needs_cleanup=True)

        try:
            session_data = self.serializer.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            return self.session_class(needs_cleanup=True)

        if not isinstance(session_data, dict):
            return self.session_class(needs_cleanup=True)

        return self.session_class(session_data)

    def save_session(self, app: Flask, session: EncryptedChunkedSession, response: Response) -> None:
        """
        Encrypt and persist the session payload back into cookie chunks.

        Args:
            app: Flask application instance.
            session: Session data for the current request.
            response: Response to mutate with cookie headers.

        Returns:
            None.
        """
        if getattr(session, "needs_cleanup", False):
            self._delete_cookie_family(app, response)

        if not session:
            if session.modified or getattr(session, "needs_cleanup", False):
                self._delete_cookie_family(app, response)
            return

        serialized_payload = self.serializer.dumps(dict(session))
        encrypted_payload = self._fernet.encrypt(serialized_payload.encode("utf-8")).decode("utf-8")
        chunks = self._split_payload_into_chunks(encrypted_payload)
        chunk_count = len(chunks)

        if chunk_count > self._max_chunks:
            # Oversized sessions break requests due to header limits, so we fail closed and clear.
            app.logger.warning("Session payload exceeded maximum chunk count (chunk_count=%s, max_chunks=%s)", chunk_count, self._max_chunks)
            self._delete_cookie_family(app, response)
            return

        self._delete_stale_sibling_cookies(app, response, current_chunk_count=chunk_count)
        primary_cookie_name = self.get_cookie_name(app)
        primary_cookie_value = f"{self._cookie_format_version}.{chunk_count}.{chunks[0]}"
        self._set_cookie(response, primary_cookie_name, primary_cookie_value, app)
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
        response.set_cookie(
            key=cookie_name,
            value=value,
            max_age=self._ttl_seconds,
            expires=expires,
            path=self.get_cookie_path(app),
            domain=self.get_cookie_domain(app),
            secure=self.get_cookie_secure(app),
            httponly=self.get_cookie_httponly(app),
            samesite=self.get_cookie_samesite(app),
        )

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
        response.delete_cookie(key=cookie_name, path=self.get_cookie_path(app), domain=self.get_cookie_domain(app))

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
