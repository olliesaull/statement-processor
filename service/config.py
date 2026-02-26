"""
Configuration module for Statement Processor.

This module loads environment variables, initializes AWS clients/resources,
and resolves required application secrets from environment variables.
"""

import os

import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_PROFILE: str | None = os.getenv("AWS_PROFILE")
AWS_REGION: str | None = os.getenv("AWS_REGION")
S3_BUCKET_NAME: str | None = os.getenv("S3_BUCKET_NAME")
STAGE: str | None = os.getenv("STAGE")
TEXTRACTION_STATE_MACHINE_ARN: str | None = os.getenv("TEXTRACTION_STATE_MACHINE_ARN")
# Ignoring Bandit suggestion as tempfile.gettempdir returns /tmp anyways (B108:hardcoded_tmp_directory)
LOCAL_DATA_DIR: str = "./tmp/data" if STAGE in {"dev", "local"} else "/tmp/data"  # nosec B108

TENANT_CONTACTS_CONFIG_TABLE_NAME: str | None = os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME")
TENANT_STATEMENTS_TABLE_NAME: str | None = os.getenv("TENANT_STATEMENTS_TABLE_NAME")
TENANT_DATA_TABLE_NAME: str | None = os.getenv("TENANT_DATA_TABLE_NAME")

session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION) if STAGE == "dev" else boto3.session.Session()  # Use the default session (e.g., in AppRunner)

s3_client = session.client("s3")
stepfunctions_client = session.client("stepfunctions")

ddb = session.resource("dynamodb")
tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_contacts_config_table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME)
tenant_data_table = ddb.Table(TENANT_DATA_TABLE_NAME)

# Required credentials are resolved on import from direct environment variables.
CLIENT_ID: str | None = os.getenv("XERO_CLIENT_ID")
CLIENT_SECRET: str | None = os.getenv("XERO_CLIENT_SECRET")
SESSION_FERNET_KEY: str | None = os.getenv("SESSION_FERNET_KEY")
FLASK_SECRET_KEY: str | None = os.getenv("FLASK_SECRET_KEY")
