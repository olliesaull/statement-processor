"""Module for configuring global vars"""

import os

from dotenv import load_dotenv

load_dotenv()

AWS_PROFILE = os.getenv("AWS_PROFILE")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

TENANT_CONTACTS_CONFIG_TABLE_NAME=os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME")
TENANT_STATEMENTS_TABLE_NAME=os.getenv("TENANT_STATEMENTS_TABLE_NAME")

CLIENT_ID = os.environ.get("XERO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("XERO_CLIENT_SECRET")
