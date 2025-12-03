import os

import boto3
from aws_lambda_powertools.logging import Logger

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
TENANT_CONTACTS_CONFIG_TABLE_NAME = os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME", "")
TENANT_STATEMENTS_TABLE_NAME = os.getenv("TENANT_STATEMENTS_TABLE_NAME", "")
TENANT_DATA_TABLE_NAME = os.getenv("TENANT_DATA_TABLE_NAME", "")
AWS_PROFILE = os.getenv("AWS_PROFILE")

logger: Logger = Logger()

if AWS_PROFILE:
    session = boto3.session.Session(region_name=AWS_REGION, profile_name=AWS_PROFILE)
else:
    session = boto3.session.Session(region_name=AWS_REGION)
s3_client = session.client("s3")
ddb = session.resource("dynamodb")

tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME) if TENANT_STATEMENTS_TABLE_NAME else None
tenant_contacts_config_table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME) if TENANT_CONTACTS_CONFIG_TABLE_NAME else None
tenant_data_table = ddb.Table(TENANT_DATA_TABLE_NAME) if TENANT_DATA_TABLE_NAME else None
