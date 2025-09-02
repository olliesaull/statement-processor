from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_s3 as s3,
)

from constructs import Construct

class StatementProcessorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, stage: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        stage = stage.lower()
        is_production: bool = stage == "prod"

        SP_DDB_TABLE_NAME = "StatementProcessorTable"
        STATEMENTS_S3_BUCKET_NAME = f"dexero-statement-processor-{stage}"

        #region ---------- DynamoDB ----------

        review_table = dynamodb.Table(
            self, SP_DDB_TABLE_NAME,
            table_name=SP_DDB_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ContactID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        #endregion ---------- DynamoDB ----------

        #region ---------- S3 ----------

        review_s3_bucket = s3.Bucket(
            self, STATEMENTS_S3_BUCKET_NAME,
            bucket_name=STATEMENTS_S3_BUCKET_NAME,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        #endregion ---------- S3 ----------
