"""Module for configuring global resources"""

import boto3

from configuration.config import (
    AWS_PROFILE,
    AWS_REGION,
    TENANT_CONTACTS_CONFIG_TABLE_NAME,
    TENANT_STATEMENTS_TABLE_NAME,
)

aws_session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)

s3_client = aws_session.client("s3")

ddb = aws_session.resource("dynamodb")
tenant_statements_table = ddb.Table(TENANT_STATEMENTS_TABLE_NAME)
tenant_contacts_config_table = ddb.Table(TENANT_CONTACTS_CONFIG_TABLE_NAME)
