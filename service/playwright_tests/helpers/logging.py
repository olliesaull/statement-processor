"""Logging helpers for Playwright tests."""


def log_step(prefix: str, message: str) -> None:
    """Log a Playwright step to stdout.

    Args:
        prefix: Prefix label for the log line.
        message: Step message to emit.

    Returns:
        None.
    """
    print(f"[{prefix}] {message}", flush=True)
