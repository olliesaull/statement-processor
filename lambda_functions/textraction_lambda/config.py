"""Shared AWS clients and environment-derived settings."""

import os

import boto3
from aws_lambda_powertools.logging import Logger
from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table
from mypy_boto3_s3 import S3Client
from mypy_boto3_textract import TextractClient

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
TENANT_CONTACTS_CONFIG_TABLE_NAME = os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME", "")
TENANT_STATEMENTS_TABLE_NAME = os.getenv("TENANT_STATEMENTS_TABLE_NAME", "")
TENANT_DATA_TABLE_NAME = os.getenv("TENANT_DATA_TABLE_NAME", "")
AWS_PROFILE = os.getenv("AWS_PROFILE")

logger: Logger = Logger()

session = boto3.session.Session(region_name=AWS_REGION, profile_name=AWS_PROFILE) if AWS_PROFILE else boto3.session.Session(region_name=AWS_REGION)
s3_client: S3Client = session.client("s3")
textract_client: TextractClient = session.client("textract")
ddb: DynamoDBServiceResource = session.resource("dynamodb")

tenant_statements_table: Table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_contacts_config_table: Table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME)
tenant_data_table: Table = ddb.Table(TENANT_DATA_TABLE_NAME)
