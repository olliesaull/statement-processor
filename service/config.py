"""
Configuration module for Statement Processor.

This module loads environment variables, initializes AWS clients/resources,
and fetches required SSM parameters at import time.
"""

import logging
import os
from typing import Optional, Tuple

import boto3
from aws_lambda_powertools.logging import Logger
from dotenv import load_dotenv
from mypy_boto3_ssm import SSMClient

load_dotenv()

AWS_PROFILE: Optional[str] = os.getenv("AWS_PROFILE")
AWS_REGION: Optional[str] = os.getenv("AWS_REGION")
S3_BUCKET_NAME: Optional[str] = os.getenv("S3_BUCKET_NAME")
STAGE: Optional[str] = os.getenv("STAGE")
TEXTRACTION_STATE_MACHINE_ARN: Optional[str] = os.getenv("TEXTRACTION_STATE_MACHINE_ARN")

TENANT_CONTACTS_CONFIG_TABLE_NAME: Optional[str] = os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME")
TENANT_STATEMENTS_TABLE_NAME: Optional[str] = os.getenv("TENANT_STATEMENTS_TABLE_NAME")
TENANT_DATA_TABLE_NAME: Optional[str] = os.getenv("TENANT_DATA_TABLE_NAME")

if STAGE == "dev":
    session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
else:
    session = boto3.session.Session()  # Use the default session (e.g., in AppRunner)

s3_client = session.client("s3")
stepfunctions_client = session.client("stepfunctions")

ddb = session.resource("dynamodb")
tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_contacts_config_table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME)
tenant_data_table = ddb.Table(TENANT_DATA_TABLE_NAME)

logger: Logger = Logger()

_SUPPRESSED_LOGGERS: Tuple[str, ...] = ("boto", "urllib3", "s3transfer", "boto3", "botocore", "nose")
for name in _SUPPRESSED_LOGGERS:
    logging.getLogger(name).setLevel(logging.CRITICAL)

ssm_client: SSMClient = session.client("ssm")


def fetch_parameter(name: str) -> str:
    """Fetch a single parameter from AWS SSM Parameter Store."""
    try:
        response = ssm_client.get_parameter(Name=name, WithDecryption=True)
        return response["Parameter"]["Value"]
    except ssm_client.exceptions.ParameterNotFound as e:
        logger.error("Parameter not found in SSM.", parameter=name)
        raise ValueError("Parameter not found in SSM.") from e
    except ssm_client.exceptions.ClientError as e:
        logger.error("Error fetching parameter", parameter=name)
        raise RuntimeError("Error fetching parameter") from e


# Required Xero credentials are resolved on import.
CLIENT_ID = fetch_parameter(os.environ.get("XERO_CLIENT_ID_PATH"))
CLIENT_SECRET = fetch_parameter(os.environ.get("XERO_CLIENT_SECRET_PATH"))
