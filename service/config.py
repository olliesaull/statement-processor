"""Configuration for the Statement Processor Flask service.

This module centralizes environment loading plus shared AWS client/resource
construction. Values are resolved once on import so the rest of the codebase
can rely on typed module-level constants instead of re-reading environment
variables inside request handlers.
"""

import os

import boto3
from dotenv import load_dotenv

load_dotenv()


def get_envar(envar: str, default_value: str = "") -> str:
    """Return an environment variable or raise when a required one is missing.

    Args:
        envar: Environment variable name to resolve.
        default_value: Optional fallback value. When omitted, the variable is
            treated as required.

    Returns:
        Environment value or the provided default.
    """
    value = os.environ.get(envar, "")
    if not value and not default_value:
        raise OSError(f"Missing environment variable: {envar}")
    return value or default_value


DOMAIN_NAME: str = get_envar("DOMAIN_NAME", "localhost")
S3_BUCKET_NAME: str = get_envar("S3_BUCKET_NAME")
STAGE: str = get_envar("STAGE", "prod")
TEXTRACTION_STATE_MACHINE_ARN: str = get_envar("TEXTRACTION_STATE_MACHINE_ARN")
VALKEY_URL: str = get_envar("VALKEY_URL", "redis://127.0.0.1:6379/0")
# Ignoring Bandit suggestion as tempfile.gettempdir returns /tmp anyways (B108:hardcoded_tmp_directory)
LOCAL_DATA_DIR: str = "./tmp/data" if STAGE in {"dev", "local"} else "/tmp/data"  # nosec B108

TENANT_CONTACTS_CONFIG_TABLE_NAME: str = get_envar("TENANT_CONTACTS_CONFIG_TABLE_NAME")
TENANT_STATEMENTS_TABLE_NAME: str = get_envar("TENANT_STATEMENTS_TABLE_NAME")
TENANT_DATA_TABLE_NAME: str = get_envar("TENANT_DATA_TABLE_NAME")
TENANT_BILLING_TABLE_NAME: str = get_envar("TENANT_BILLING_TABLE_NAME")
TENANT_TOKEN_LEDGER_TABLE_NAME: str = get_envar("TENANT_TOKEN_LEDGER_TABLE_NAME")

s3_client = boto3.client("s3")
stepfunctions_client = boto3.client("stepfunctions")
ddb_client = boto3.client("dynamodb")

ddb = boto3.resource("dynamodb")
tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_contacts_config_table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME)
tenant_data_table = ddb.Table(TENANT_DATA_TABLE_NAME)
tenant_billing_table = ddb.Table(TENANT_BILLING_TABLE_NAME)
tenant_token_ledger_table = ddb.Table(TENANT_TOKEN_LEDGER_TABLE_NAME)

# Required credentials are resolved on import so worker startup fails fast if
# deployment-time secret injection is incomplete.
CLIENT_ID: str = get_envar("XERO_CLIENT_ID")
CLIENT_SECRET: str = get_envar("XERO_CLIENT_SECRET")
FLASK_SECRET_KEY: str = get_envar("FLASK_SECRET_KEY")
