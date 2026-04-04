#!/usr/bin/env python3
"""Generate Nginx location blocks from Flask route definitions.

Introspects a Flask app's URL map and produces per-route location blocks
with method restrictions, query-string whitelisting, and optional
per-route directive overrides (e.g. client_max_body_size).

Usage:
    python3.13 nginx_route_config_generator.py \\
        --app app:app \\
        --upstream gunicorn \\
        --output nginx-routes.conf \\
        --route-params nginx_route_querystring_allow_list.json \\
        --route-overrides nginx_route_overrides.json
"""

import argparse
import importlib
import json
import re
import sys
from pathlib import Path


def import_flask_app(app_string: str):
    """Import a Flask app from a string like 'module:app' or 'module:create_app()'.

    Adds the current directory to sys.path so the module can be found
    when run from the service/ directory.
    """
    try:
        if "." not in sys.path:
            sys.path.insert(0, ".")

        if ":" not in app_string:
            raise ValueError("App string must be in format 'module:app'")

        module_name, app_name = app_string.split(":", 1)
        module = importlib.import_module(module_name)

        if "(" in app_name:
            # Factory function call
            func_name = app_name.replace("()", "")
            func = getattr(module, func_name)
            if callable(func):
                return func()
            raise ValueError(f"{func_name} is not callable")

        return getattr(module, app_name)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"Error importing Flask app '{app_string}': {exc}", file=sys.stderr)
        sys.exit(1)


def flask_to_nginx_pattern(flask_route: str) -> str:
    """Convert a Flask route pattern to an Nginx regex pattern.

    Handles typed converters (int, float, string, path, uuid) and the
    default converter.  Escapes dots for safe regex matching.
    """
    converters = {
        r"<int:([^>]+)>": r"\\d+",
        r"<float:([^>]+)>": r"\\d+(?:\\.\\d+)?",
        r"<string:([^>]+)>": r"[^/]+",
        r"<path:([^>]+)>": r".*",
        r"<uuid:([^>]+)>": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        r"<([^:>]+)>": r"[^/]+",  # Default converter
    }

    # Escape dots in literal path segments before converter substitution
    # to avoid corrupting regex patterns inside converters (e.g. .* or \.\d+)
    pattern = flask_route.replace(".", "\\.")
    for flask_conv, nginx_conv in converters.items():
        pattern = re.sub(flask_conv, nginx_conv, pattern)

    return f"^{pattern}$"


def is_static_route(route_path: str) -> bool:
    """Return True if this route should be excluded from generated config.

    Skips routes that are either handled directly by nginx.conf (static
    files, favicon, .well-known) or that must never appear in the
    generated config regardless of build environment (/test-login is
    only registered when STAGE=local but the generator may run locally).
    """
    skip_routes = [
        "/favicon.ico",
        "/static/<path:filename>",
        "/.well-known/<path:path>",
        "/test-login",  # Local-only dev route — must never appear in nginx config
    ]
    return route_path in skip_routes


def extract_flask_routes(app) -> list[dict]:
    """Extract application routes from a Flask app, excluding static routes.

    Merges routes with the same path (Flask may register the same URL
    under multiple endpoints, e.g. GET and POST separately) to avoid
    duplicate nginx location blocks.

    Returns a sorted list of dicts with keys: endpoint, original, pattern, methods.
    """
    merged: dict[str, dict] = {}
    skipped = []

    with app.app_context():
        for rule in app.url_map.iter_rules():
            if is_static_route(rule.rule):
                skipped.append(rule.rule)
                continue

            # Keep all methods except OPTIONS (Nginx handles OPTIONS implicitly)
            methods = set(rule.methods - {"OPTIONS"})

            if rule.rule in merged:
                # Same path registered under a different endpoint — merge methods
                merged[rule.rule]["methods"].update(methods)
            else:
                merged[rule.rule] = {
                    "endpoint": rule.endpoint,
                    "original": rule.rule,
                    "pattern": flask_to_nginx_pattern(rule.rule),
                    "methods": methods,
                }

    if skipped:
        print(f"INFO: Skipped {len(skipped)} static/handled routes:", file=sys.stderr)
        for route in skipped:
            print(f"  - {route}", file=sys.stderr)

    # Convert method sets to sorted lists for deterministic output
    routes = []
    for route in merged.values():
        route["methods"] = sorted(route["methods"])
        routes.append(route)

    return sorted(routes, key=lambda x: x["original"])


def get_route_query_params(custom_params_file: str | None = None) -> dict:
    """Load allowed query parameters per route from a JSON file.

    Returns a dict mapping route paths to lists of allowed parameter names.
    Comment fields (keys starting with 'comment') are filtered out.
    Returns an empty dict when no file is provided.
    """
    if not custom_params_file or not Path(custom_params_file).exists():
        return {}

    try:
        with open(custom_params_file, encoding="utf-8") as f:
            data = json.load(f)
        filtered = {k: v for k, v in data.items() if not k.startswith("comment") and isinstance(v, list)}
        print(f"Loaded custom route parameters from {custom_params_file}", file=sys.stderr)
        return filtered
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"Warning: Could not load custom params file: {exc}", file=sys.stderr)
        return {}


def load_route_overrides(overrides_file: str | None = None) -> dict:
    """Load per-route Nginx directive overrides from a JSON file.

    Returns a dict mapping route paths to dicts of {directive: value}.
    Comment fields are filtered out.  Returns empty dict when no file given.
    """
    if not overrides_file or not Path(overrides_file).exists():
        return {}

    try:
        with open(overrides_file, encoding="utf-8") as f:
            data = json.load(f)
        filtered = {k: v for k, v in data.items() if not k.startswith("comment") and isinstance(v, dict)}
        print(f"Loaded route overrides from {overrides_file}", file=sys.stderr)
        return filtered
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"Warning: Could not load overrides file: {exc}", file=sys.stderr)
        return {}


def generate_param_regex(allowed_params: list[str]) -> str:
    """Build a regex that matches only the allowed query parameters.

    Values must not contain &, <, >, double-quote, or single-quote
    (blocks common XSS vectors).  Returns empty string for empty input.
    """
    if not allowed_params:
        return ""

    param_patterns = []
    for param in allowed_params:
        # Allow any value except dangerous characters
        param_patterns.append(f"{param}=[^&<>\\x22\\x27]*")

    if len(param_patterns) == 1:
        return f"^{param_patterns[0]}$"

    # Multiple params: any combination in any order
    param_regex = "|".join(param_patterns)
    return f"^({param_regex})(&({param_regex}))*$"


def generate_single_location(route: dict, upstream_name: str, allowed_params: dict | None = None, route_overrides: dict | None = None) -> list[str]:
    """Generate a single Nginx location block for a Flask route.

    Applies method restrictions, query-string handling (strip, validate,
    or pass through), and optional per-route directive overrides.
    """
    lines = [f'location ~ "{route["pattern"]}" {{']
    lines.append("    access_log off;")

    # Public pages: strip query strings (UTMs logged by CloudFront)
    public_pages = ["/", "/about", "/cookies", "/instructions", "/pricing"]

    route_params = allowed_params.get(route["original"]) if allowed_params else None
    is_public = route["original"] in public_pages
    has_params = route_params is not None

    # Inject per-route directive overrides (e.g. client_max_body_size)
    overrides = (route_overrides or {}).get(route["original"], {})
    for directive, value in overrides.items():
        lines.append(f"    {directive} {value};")

    # Method restriction and HEAD handling
    lines.append(f"    limit_except {' '.join(route['methods'])} {{ deny all; }}")
    lines.append("    if ($request_method = HEAD) {return 200;}")

    if is_public:
        lines.extend(
            [
                "    # Public page - UTMs logged by CloudFront, drop query strings silently",
                '    if ($args != "") {',
                "        rewrite ^(.*)$ $1? last;",
                "    }",
                f"    proxy_pass http://{upstream_name};",
            ]
        )
    elif has_params:
        param_regex = generate_param_regex(route_params)
        params_str = ", ".join(route_params)
        lines.extend(
            [
                f"    # Allowed parameters: {params_str}",
                '    if ($args ~ ".+" ) {set $check_args 1;}',
                f'    if ($args !~ "{param_regex}") {{',
                '        set $check_args "${check_args}1";',
                "    }",
                '    if ($check_args = "11") {',
                "        return 404;",
                "    }",
                f"    proxy_pass http://{upstream_name};",
            ]
        )
    else:
        # Private routes: strip any query strings
        lines.extend(["    # Private route - no query strings allowed", '    if ($args != "") {', "        rewrite ^(.*)$ $1? last;", "    }", f"    proxy_pass http://{upstream_name};"])

    lines.extend(["}", ""])
    return lines


def generate_location_blocks(routes: list[dict], upstream_name: str = "gunicorn", custom_params_file: str | None = None, overrides_file: str | None = None) -> str:
    """Generate all Nginx location blocks for the given Flask routes.

    Loads query-parameter allow list and route overrides, then produces
    a complete nginx-routes.conf file body.
    """
    allowed_params = get_route_query_params(custom_params_file)
    route_overrides = load_route_overrides(overrides_file)

    lines = ["# Auto-generated Flask route location blocks", f"# Total routes: {len(routes)}", "# Container logging: Uses stderr for CloudWatch integration", ""]

    # Document routes with custom parameters
    routes_with_params = [r for r in routes if r["original"] in allowed_params]
    if routes_with_params:
        lines.append("# Routes with allowed query parameters:")
        for route in routes_with_params:
            params = allowed_params[route["original"]]
            lines.append(f"#   {route['original']}: {', '.join(params)}")
        lines.append("")

    # Document routes with overrides
    routes_with_overrides = [r for r in routes if r["original"] in route_overrides]
    if routes_with_overrides:
        lines.append("# Routes with directive overrides:")
        for route in routes_with_overrides:
            overrides = route_overrides[route["original"]]
            overrides_str = ", ".join(f"{k}={v}" for k, v in overrides.items())
            lines.append(f"#   {route['original']}: {overrides_str}")
        lines.append("")

    for route in routes:
        lines.extend(generate_single_location(route, upstream_name, allowed_params, route_overrides))

    return "\n".join(lines)


def main():  # pylint: disable=too-many-locals,too-many-statements
    """Entry point: parse args, import Flask app, generate nginx-routes.conf."""
    parser = argparse.ArgumentParser(description="Generate nginx location blocks from Flask routes")
    parser.add_argument("--app", "-a", default="app:app", help="Flask app in format 'module:app' or 'module:create_app()'")
    parser.add_argument("--upstream", "-u", default="gunicorn", help="Upstream name (default: gunicorn)")
    parser.add_argument("--output", "-o", default="nginx-routes.conf", help="Output file (default: nginx-routes.conf)")
    parser.add_argument("--route-params", default="nginx_route_querystring_allow_list.json", help="JSON file with route-specific allowed parameters")
    parser.add_argument("--route-overrides", default="nginx_route_overrides.json", help="JSON file with per-route Nginx directive overrides")
    parser.add_argument("--json", action="store_true", help="Also output routes as JSON")

    args = parser.parse_args()

    try:
        print(f"Importing Flask app: {args.app}", file=sys.stderr)
        app = import_flask_app(args.app)

        print("Extracting routes...", file=sys.stderr)
        routes = extract_flask_routes(app)

        print(f"Found {len(routes)} application routes:", file=sys.stderr)
        for route in routes:
            methods_str = ",".join(route["methods"])
            print(f"  {methods_str:15} {route['original']}", file=sys.stderr)

        location_config = generate_location_blocks(routes, args.upstream, args.route_params, args.route_overrides)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(location_config)
            print(f"Location blocks written to: {args.output}", file=sys.stderr)
        else:
            print(location_config)

        if args.json:
            json_file = args.output.replace(".conf", ".json") if args.output else "routes.json"
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(routes, f, indent=2)
            print(f"Routes JSON written to: {json_file}", file=sys.stderr)

    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
