"""Shared AWS clients and environment-derived settings."""

import os

import boto3
from botocore.config import Config as BotoConfig
from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table
from mypy_boto3_s3 import S3Client

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
TENANT_STATEMENTS_TABLE_NAME = os.getenv("TENANT_STATEMENTS_TABLE_NAME", "")
TENANT_DATA_TABLE_NAME = os.getenv("TENANT_DATA_TABLE_NAME", "")
TENANT_BILLING_TABLE_NAME = os.getenv("TENANT_BILLING_TABLE_NAME", "")
TENANT_TOKEN_LEDGER_TABLE_NAME = os.getenv("TENANT_TOKEN_LEDGER_TABLE_NAME", "")
AWS_PROFILE = os.getenv("AWS_PROFILE")

session = boto3.session.Session(region_name=AWS_REGION, profile_name=AWS_PROFILE) if AWS_PROFILE else boto3.session.Session(region_name=AWS_REGION)
s3_client: S3Client = session.client("s3")
ddb_client = session.client("dynamodb")
ddb: DynamoDBServiceResource = session.resource("dynamodb")

# 600s read timeout — resets on each data chunk received from Bedrock.
# Covers worst-case dense multi-page PDFs without premature socket timeout.
_bedrock_config = BotoConfig(read_timeout=600, retries={"max_attempts": 0})
bedrock_runtime_client = session.client("bedrock-runtime", config=_bedrock_config)

tenant_statements_table: Table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_data_table: Table = ddb.Table(TENANT_DATA_TABLE_NAME)
tenant_billing_table: Table = ddb.Table(TENANT_BILLING_TABLE_NAME)
tenant_token_ledger_table: Table = ddb.Table(TENANT_TOKEN_LEDGER_TABLE_NAME)
