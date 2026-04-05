"""Tests for the Nginx route configuration generator."""

from __future__ import annotations

import json
import re
import tempfile

from nginx_route_config_generator import flask_to_nginx_pattern, generate_param_regex, generate_single_location, get_route_query_params, is_static_route, load_route_overrides


class TestFlaskToNginxPattern:
    """Tests for Flask-to-Nginx route pattern conversion."""

    def test_simple_static_route(self) -> None:
        """Static route like /about becomes ^/about$."""
        assert flask_to_nginx_pattern("/about") == "^/about$"

    def test_uuid_converter(self) -> None:
        """Flask <uuid:id> becomes a UUID regex."""
        result = flask_to_nginx_pattern("/statement/<uuid:statement_id>")
        assert "^/statement/" in result
        assert "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" in result
        assert result.endswith("$")

    def test_int_converter(self) -> None:
        """Flask <int:id> becomes \\d+."""
        result = flask_to_nginx_pattern("/page/<int:num>")
        assert result == r"^/page/\d+$"

    def test_string_converter(self) -> None:
        """Flask <string:name> becomes [^/]+."""
        result = flask_to_nginx_pattern("/user/<string:name>")
        assert result == "^/user/[^/]+$"

    def test_default_converter(self) -> None:
        """Flask <name> (no type) becomes [^/]+."""
        result = flask_to_nginx_pattern("/user/<name>")
        assert result == "^/user/[^/]+$"

    def test_dot_in_route_is_escaped(self) -> None:
        """Dots in route paths are escaped."""
        result = flask_to_nginx_pattern("/robots.txt")
        assert result == r"^/robots\.txt$"


class TestIsStaticRoute:
    """Tests for static route detection."""

    def test_favicon_is_static(self) -> None:
        """/favicon.ico is handled by nginx directly."""
        assert is_static_route("/favicon.ico") is True

    def test_static_files_route_is_static(self) -> None:
        """/static/ files are served directly by nginx."""
        assert is_static_route("/static/<path:filename>") is True

    def test_well_known_is_static(self) -> None:
        """/.well-known/ is handled by nginx directly."""
        assert is_static_route("/.well-known/<path:path>") is True

    def test_test_login_is_skipped(self) -> None:
        """Local-only dev route must never appear in generated config."""
        assert is_static_route("/test-login") is True

    def test_normal_route_is_not_static(self) -> None:
        """Application routes are not static and should be included."""
        assert is_static_route("/statements") is False


class TestGenerateParamRegex:
    """Tests for query parameter regex generation."""

    def test_empty_params_returns_empty(self) -> None:
        """Empty allowed list returns empty string (no regex needed)."""
        assert generate_param_regex([]) == ""

    def test_single_param(self) -> None:
        """Single param produces anchored pattern."""
        regex = generate_param_regex(["session_id"])
        assert regex == r"^session_id=[^&<>\x22\x27]*$"

    def test_multiple_params(self) -> None:
        """Multiple params produce alternation pattern."""
        regex = generate_param_regex(["view", "sort"])
        assert "view=" in regex
        assert "sort=" in regex
        assert "|" in regex

    def test_blocks_angle_brackets(self) -> None:
        """Regex excludes < and > to prevent XSS."""
        regex = generate_param_regex(["q"])
        compiled = re.compile(regex)
        assert compiled.match("q=hello") is not None
        assert compiled.match("q=<script>") is None


class TestGetRouteQueryParams:
    """Tests for loading query parameters from JSON."""

    def test_loads_custom_file(self) -> None:
        """Custom params file overrides defaults."""
        data = {"comment": "test", "/test": ["foo", "bar"]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            result = get_route_query_params(f.name)

        assert "/test" in result
        assert result["/test"] == ["foo", "bar"]
        # Comment fields are filtered out
        assert "comment" not in result

    def test_returns_empty_when_no_file(self) -> None:
        """Returns empty dict when no file provided."""
        result = get_route_query_params(None)
        assert result == {}


class TestLoadRouteOverrides:
    """Tests for loading route overrides from JSON."""

    def test_loads_overrides_file(self) -> None:
        """Overrides file is loaded and comment fields filtered."""
        data = {"comment": "test", "/upload": {"client_max_body_size": "10m"}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            result = load_route_overrides(f.name)

        assert "/upload" in result
        assert result["/upload"] == {"client_max_body_size": "10m"}
        assert "comment" not in result

    def test_returns_empty_when_no_file(self) -> None:
        """Returns empty dict when no file provided."""
        result = load_route_overrides(None)
        assert result == {}


class TestGenerateSingleLocation:
    """Tests for individual location block generation."""

    def test_public_page_strips_query_strings(self) -> None:
        """Public pages rewrite to strip query strings."""
        route = {"endpoint": "about", "original": "/about", "pattern": "^/about$", "methods": ["GET", "HEAD"]}
        lines = generate_single_location(route, "gunicorn")
        block = "\n".join(lines)
        assert "rewrite ^(.*)$ $1? last;" in block
        assert "proxy_pass http://gunicorn" in block
        # Non-healthz routes should have access logging enabled (no suppression)
        assert "access_log off" not in block

    def test_healthz_suppresses_access_log(self) -> None:
        """Health check endpoint suppresses access logs to reduce noise."""
        route = {"endpoint": "healthz", "original": "/healthz", "pattern": "^/healthz$", "methods": ["GET", "HEAD"]}
        lines = generate_single_location(route, "gunicorn")
        block = "\n".join(lines)
        assert "access_log off;" in block

    def test_route_with_allowed_params(self) -> None:
        """Routes with allowed params get regex validation."""
        route = {"endpoint": "checkout_success", "original": "/checkout/success", "pattern": "^/checkout/success$", "methods": ["GET", "HEAD"]}
        lines = generate_single_location(route, "gunicorn", allowed_params={"/checkout/success": ["session_id"]})
        block = "\n".join(lines)
        assert "session_id=" in block
        assert "return 404" in block

    def test_private_route_strips_query_strings(self) -> None:
        """Private routes with no params strip query strings."""
        route = {"endpoint": "logout", "original": "/logout", "pattern": "^/logout$", "methods": ["GET", "HEAD"]}
        lines = generate_single_location(route, "gunicorn")
        block = "\n".join(lines)
        assert "rewrite ^(.*)$ $1? last;" in block

    def test_method_restriction(self) -> None:
        """Routes restrict to declared methods."""
        route = {"endpoint": "create_checkout", "original": "/api/checkout/create", "pattern": "^/api/checkout/create$", "methods": ["POST"]}
        lines = generate_single_location(route, "gunicorn")
        block = "\n".join(lines)
        assert "limit_except POST" in block

    def test_route_overrides_injected(self) -> None:
        """Route overrides (e.g. client_max_body_size) are injected."""
        route = {"endpoint": "upload_statements", "original": "/upload-statements", "pattern": "^/upload-statements$", "methods": ["GET", "HEAD", "POST"]}
        lines = generate_single_location(route, "gunicorn", route_overrides={"/upload-statements": {"client_max_body_size": "10m"}})
        block = "\n".join(lines)
        assert "client_max_body_size 10m;" in block
