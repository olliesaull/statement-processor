"""Shared AWS clients and environment-derived settings."""

import os

import boto3
from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table
from mypy_boto3_s3 import S3Client

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
TENANT_DATA_TABLE_NAME = os.getenv("TENANT_DATA_TABLE_NAME", "")
TENANT_STATEMENTS_TABLE_NAME = os.getenv("TENANT_STATEMENTS_TABLE_NAME", "")

session = boto3.session.Session(region_name=AWS_REGION)
s3_client: S3Client = session.client("s3")
ddb: DynamoDBServiceResource = session.resource("dynamodb")

tenant_data_table: Table = ddb.Table(TENANT_DATA_TABLE_NAME)
tenant_statements_table: Table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
