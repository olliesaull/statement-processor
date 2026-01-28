"""Shared structured logger for the textraction Lambda."""

import logging

from aws_lambda_powertools.logging import Logger

_SUPPRESSED_LOGGERS: tuple[str, ...] = ("boto", "urllib3", "s3transfer", "boto3", "botocore", "nose")
for name in _SUPPRESSED_LOGGERS:
    logging.getLogger(name).setLevel(logging.CRITICAL)

logger: Logger = Logger()
