"""Jinja template filters — registered in service/app.py via add_template_filter."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


def format_last_sync(epoch_ms: int | float | Decimal | None) -> str:
    """Format an epoch-ms timestamp as 'Mon D, HH:MM' in UTC.

    Returns '' for None / 0 so the template can render a muted 'First sync...'
    placeholder inline without needing Jinja None-safety scaffolding.

    Args:
        epoch_ms: Milliseconds since the Unix epoch. Accepts ``Decimal`` so
            values read straight out of DynamoDB numeric attributes work.

    Returns:
        Formatted string like ``"Apr 22, 09:32"``, or ``""`` for empty input.

    Note:
        Uses ``%-d`` to strip the leading zero on the day, which is a
        Linux/macOS-only strftime directive. Matches the documented dev
        and deployment environment (Ubuntu on AppRunner); will produce
        zero-padded days on Windows if the code is ever run there.
    """
    if not epoch_ms:
        return ""
    ts = int(epoch_ms) / 1000
    # %-d strips the day's leading zero. Linux/macOS only, which matches the
    # documented dev environment (Ubuntu; see .claude/rules/project.md).
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%b %-d, %H:%M")
