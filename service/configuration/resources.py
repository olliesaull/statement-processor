"""Module for configuring global resources"""

import boto3

from configuration.config import AWS_PROFILE, AWS_REGION

aws_session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)

s3_client = aws_session.client("s3")

ddb = aws_session.resource("dynamodb")
statement_processor_table = ddb.Table("StatementProcessorTable")
