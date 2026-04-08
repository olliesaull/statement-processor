"""Structured logger for the tenant erasure Lambda."""

from aws_lambda_powertools import Logger

logger = Logger(service="tenant-erasure-lambda")
