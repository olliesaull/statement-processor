"""
Shared pytest setup for unit tests.

We stub the global ``config`` module so imports do not trigger AWS SSM lookups (agents do not have AWS profile etc).
The production logger accepts structured keyword arguments (e.g. ``raw_value=...``),
which the standard library logger rejects. We use a LoggerAdapter to capture those
kwargs and reattach them as ``extra`` metadata so test logging stays informative
without re-implementing the logger API.
"""

import logging
import os
import sys
import types


class _StructuredLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that tolerates structured keyword arguments."""

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        extra = kwargs.get("extra", {})
        for key, value in list(kwargs.items()):
            if key in {"exc_info", "stack_info", "stacklevel", "extra"}:
                continue
            extra[key] = value
            kwargs.pop(key)
        kwargs["extra"] = extra
        if extra:
            msg = f"{msg} | {extra}"
        return msg, kwargs


def _build_test_logger() -> logging.LoggerAdapter:
    level_name = os.getenv("TEST_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger("statement-processor.tests")
    if not logger.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.setLevel(level)
    return _StructuredLoggerAdapter(logger, {})


fake_config = types.ModuleType("config")
fake_config.logger = _build_test_logger()
sys.modules["config"] = fake_config
