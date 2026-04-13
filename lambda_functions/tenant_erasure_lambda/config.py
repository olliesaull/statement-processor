"""Shared AWS clients and environment-derived settings."""

import os

import boto3
import stripe
from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table
from mypy_boto3_s3 import S3Client

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
TENANT_DATA_TABLE_NAME = os.getenv("TENANT_DATA_TABLE_NAME", "")
TENANT_STATEMENTS_TABLE_NAME = os.getenv("TENANT_STATEMENTS_TABLE_NAME", "")
TENANT_BILLING_TABLE_NAME = os.getenv("TENANT_BILLING_TABLE_NAME", "")
STRIPE_API_KEY_SSM_PATH = os.getenv("STRIPE_API_KEY_SSM_PATH", "")

session = boto3.session.Session(region_name=AWS_REGION)
s3_client: S3Client = session.client("s3")
ddb: DynamoDBServiceResource = session.resource("dynamodb")

tenant_data_table: Table = ddb.Table(TENANT_DATA_TABLE_NAME)
tenant_statements_table: Table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_billing_table: Table = ddb.Table(TENANT_BILLING_TABLE_NAME)

# Fetch Stripe API key from SSM at cold start.
if STRIPE_API_KEY_SSM_PATH:
    _ssm = session.client("ssm")
    _stripe_key_response = _ssm.get_parameter(Name=STRIPE_API_KEY_SSM_PATH, WithDecryption=True)
    stripe.api_key = _stripe_key_response["Parameter"]["Value"]
else:
    # Import logger lazily to avoid circular dependency at module load time.
    import logging
    logging.getLogger("TenantErasureLambda").warning("STRIPE_API_KEY_SSM_PATH not set — Stripe subscription cancellation will be skipped on tenant erasure")
