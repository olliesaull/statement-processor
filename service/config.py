"""
Configuration module for Statement Processor.

This module loads environment variables, initializes AWS clients/resources,
and fetches required SSM parameters at import time.
"""

import os

import boto3
from aws_lambda_powertools.utilities.parameters import get_parameter
from dotenv import load_dotenv
from mypy_boto3_stepfunctions import SFNClient

load_dotenv()

AWS_PROFILE: str | None = os.getenv("AWS_PROFILE")
AWS_REGION: str | None = os.getenv("AWS_REGION")
S3_BUCKET_NAME: str | None = os.getenv("S3_BUCKET_NAME")
STAGE: str | None = os.getenv("STAGE")
TEXTRACTION_STATE_MACHINE_ARN: str | None = os.getenv("TEXTRACTION_STATE_MACHINE_ARN")
# Valkey configuration used by both Flask-Session and Flask-Caching.
VALKEY_URL: str = os.getenv("VALKEY_URL", "redis://127.0.0.1:6379")
VALKEY_DB: int = int(os.getenv("VALKEY_DB", "0"))
VALKEY_CACHE_KEY_PREFIX: str = os.getenv("VALKEY_CACHE_KEY_PREFIX", "statement_processor:")
VALKEY_CACHE_DEFAULT_TIMEOUT: int = int(os.getenv("VALKEY_CACHE_DEFAULT_TIMEOUT", "0"))
# Ignoring Bandit suggestion as tempfile.gettempdir returns /tmp anyways (B108:hardcoded_tmp_directory)
LOCAL_DATA_DIR: str = "./tmp/data" if STAGE in {"dev", "local"} else "/tmp/data"  # nosec B108

TENANT_CONTACTS_CONFIG_TABLE_NAME: str | None = os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME")
TENANT_STATEMENTS_TABLE_NAME: str | None = os.getenv("TENANT_STATEMENTS_TABLE_NAME")
TENANT_DATA_TABLE_NAME: str | None = os.getenv("TENANT_DATA_TABLE_NAME")

session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION) if STAGE == "dev" else boto3.session.Session()  # Use the default session (e.g., in AppRunner)

s3_client = session.client("s3")
stepfunctions_client: SFNClient = session.client("stepfunctions")

ddb = session.resource("dynamodb")
tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_contacts_config_table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME)
tenant_data_table = ddb.Table(TENANT_DATA_TABLE_NAME)

# Required Xero credentials are resolved on import.
CLIENT_ID = get_parameter(os.environ.get("XERO_CLIENT_ID_PATH"), decrypt=True)
CLIENT_SECRET = get_parameter(os.environ.get("XERO_CLIENT_SECRET_PATH"), decrypt=True)
