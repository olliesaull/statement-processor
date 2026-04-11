"""Shared structured logger for all statement processor components.

Used by the service, extraction lambda, and tenant erasure lambda.
Each component sets ``POWERTOOLS_SERVICE_NAME`` in its environment
to identify itself in CloudWatch logs.
"""

import logging

from aws_lambda_powertools.logging import Logger

# Suppress noisy AWS SDK loggers that clutter structured output.
_SUPPRESSED_LOGGERS: tuple[str, ...] = ("boto", "urllib3", "s3transfer", "boto3", "botocore", "nose")
for name in _SUPPRESSED_LOGGERS:
    logging.getLogger(name).setLevel(logging.CRITICAL)

logger: Logger = Logger()
