"""Configuration for the Statement Processor Flask service.

This module centralizes environment loading plus shared AWS client/resource
construction. Values are resolved once on import so the rest of the codebase
can rely on typed module-level constants instead of re-reading environment
variables inside request handlers.

Secrets (Xero OAuth credentials, Flask secret key) are fetched from AWS SSM
Parameter Store at startup via a single get_parameters call. This means they
are never embedded in CloudFormation templates or visible in the AppRunner
environment variable console — the AppRunner instance role carries the
ssm:GetParameters permission instead.

The SSM parameter paths are themselves stored as environment variables
(*_SSM_PATH) so they can be changed without a code redeploy.
"""

import os

import boto3
import redis as redis_lib
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


def _fetch_ssm_secrets() -> dict[str, str]:
    """Fetch Xero OAuth credentials, Flask secret key, and Stripe API key from SSM in one call.

    Parameter paths are read from *_SSM_PATH environment variables so they can
    be updated without a code change. Uses get_parameters (batch) to minimise
    API calls. The region is read from AWS_REGION (default eu-west-1). On
    AppRunner the instance role provides credentials; locally boto3 uses the
    AWS_PROFILE set in .env.

    Returns:
        Mapping of SSM parameter path to decrypted value.

    Raises:
        RuntimeError: If any parameter is missing or inaccessible.
        boto3 ClientError: If the SSM API call fails — propagates unmodified.
    """
    params = [get_envar("XERO_CLIENT_ID_SSM_PATH"), get_envar("XERO_CLIENT_SECRET_SSM_PATH"), get_envar("FLASK_SECRET_KEY_SSM_PATH"), get_envar("STRIPE_API_KEY_SSM_PATH")]
    region: str = os.environ.get("AWS_REGION", "eu-west-1")
    response = boto3.client("ssm", region_name=region).get_parameters(Names=params, WithDecryption=True)
    invalid: list[str] = response.get("InvalidParameters", [])
    if invalid:
        raise RuntimeError(f"SSM parameters not found or not accessible: {invalid}. Ensure the parameters exist and the caller has ssm:GetParameters permission.")
    return {p["Name"]: p["Value"] for p in response["Parameters"]}


_secrets = _fetch_ssm_secrets()

DOMAIN_NAME: str = get_envar("DOMAIN_NAME", "localhost")
S3_BUCKET_NAME: str = get_envar("S3_BUCKET_NAME")
STAGE: str = get_envar("STAGE", "prod")
EXTRACTION_STATE_MACHINE_ARN: str = get_envar("EXTRACTION_STATE_MACHINE_ARN")
VALKEY_URL: str = get_envar("VALKEY_URL", "redis://127.0.0.1:6379/0")
# Ignoring Bandit suggestion as tempfile.gettempdir returns /tmp anyways (B108:hardcoded_tmp_directory)
LOCAL_DATA_DIR: str = "./tmp/data" if STAGE in {"dev", "local"} else "/tmp/data"  # nosec B108

TENANT_STATEMENTS_TABLE_NAME: str = get_envar("TENANT_STATEMENTS_TABLE_NAME")
TENANT_DATA_TABLE_NAME: str = get_envar("TENANT_DATA_TABLE_NAME")
TENANT_BILLING_TABLE_NAME: str = get_envar("TENANT_BILLING_TABLE_NAME")
TENANT_TOKEN_LEDGER_TABLE_NAME: str = get_envar("TENANT_TOKEN_LEDGER_TABLE_NAME")
STRIPE_EVENT_STORE_TABLE_NAME: str = get_envar("STRIPE_EVENT_STORE_TABLE_NAME")

s3_client = boto3.client("s3")
stepfunctions_client = boto3.client("stepfunctions")
ddb_client = boto3.client("dynamodb")

ddb = boto3.resource("dynamodb")
tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_data_table = ddb.Table(TENANT_DATA_TABLE_NAME)
tenant_billing_table = ddb.Table(TENANT_BILLING_TABLE_NAME)
tenant_token_ledger_table = ddb.Table(TENANT_TOKEN_LEDGER_TABLE_NAME)
stripe_event_store_table = ddb.Table(STRIPE_EVENT_STORE_TABLE_NAME)

# Shared Redis/Valkey connection pool — used by Flask-Session and the
# statement view cache.  Creating a single pool avoids redundant connections
# per Gunicorn worker (each import creates its own idle connection budget).
redis_client: redis_lib.Redis = redis_lib.from_url(VALKEY_URL)

# Secrets fetched from SSM — paths are configured via *_SSM_PATH env vars.
CLIENT_ID: str = _secrets[get_envar("XERO_CLIENT_ID_SSM_PATH")]
CLIENT_SECRET: str = _secrets[get_envar("XERO_CLIENT_SECRET_SSM_PATH")]
FLASK_SECRET_KEY: str = _secrets[get_envar("FLASK_SECRET_KEY_SSM_PATH")]
STRIPE_API_KEY: str = _secrets[get_envar("STRIPE_API_KEY_SSM_PATH")]
