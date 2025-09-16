from aws_cdk import (
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apprunner_alpha as apprunner_alpha
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk.aws_ecr_assets import DockerImageAsset
from constructs import Construct


class StatementProcessorStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        stage: str,
        domain_name: str,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        stage = stage.lower()
        is_production: bool = stage == "prod"

        TENANT_STATEMENTS_TABLE_NAME = "TenantStatementsTable"
        TENANT_CONTACTS_CONFIG_TABLE_NAME = "TenantContactsConfigTable"
        STATEMENTS_S3_BUCKET_NAME = f"dexero-statement-processor-{stage}"

        #region ---------- ParameterStore ----------

        # SSM Parameter Store Parameter ARNs, using wildcards to satisfy ssm:GetParametersByPath 
        parameter_arns = ["arn:aws:ssm:eu-west-1:747310139457:parameter/StatementProcessor/*"]

        # Create a policy statement to grant SSM Parameter Store access
        parameter_policy = iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=parameter_arns
        )

        # Grant SecureString decryption permission
        parameter_policy.add_actions("kms:Decrypt")

        #endregion ---------- ParameterStore ----------

        #region ---------- DynamoDB ----------

        dynamodb.Table(
            self, TENANT_STATEMENTS_TABLE_NAME,
            table_name=TENANT_STATEMENTS_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="StatementID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        dynamodb.Table(
            self, TENANT_CONTACTS_CONFIG_TABLE_NAME,
            table_name=TENANT_CONTACTS_CONFIG_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ContactID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        #endregion ---------- DynamoDB ----------

        #region ---------- S3 ----------

        s3.Bucket(
            self, STATEMENTS_S3_BUCKET_NAME,
            bucket_name=STATEMENTS_S3_BUCKET_NAME,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        #endregion ---------- S3 ----------

        #region ---------- AppRunner ----------

        statement_processor_instance_role = iam.Role(
            self,
            "Statement Processor App Runner Instance Role",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )
        statement_processor_instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricData"
                ],
                resources=[
                    "*"
                ]
            )
        )

        apprunner_asset = DockerImageAsset(self, "AppRunnerImage", directory="../service/")
        web = apprunner_alpha.Service(
            self,
            "Statement Processor Website",
            instance_role=statement_processor_instance_role,
            memory=apprunner_alpha.Memory.ONE_GB,
            cpu=apprunner_alpha.Cpu.QUARTER_VCPU,
            source=apprunner_alpha.Source.from_asset(
                asset=apprunner_asset,
                image_configuration=apprunner_alpha.ImageConfiguration(
                    port=5000,
                    environment_variables={
                        "STAGE": "prod" if is_production else "dev",
                        "DOMAIN_NAME": domain_name,
                        "POWERTOOLS_SERVICE_NAME": "StatementProcessor",
                        "LOG_LEVEL": "DEBUG",
                        "OPENAI_API_KEY_PATH": "/StatementProcessor/OPENAI_API_KEY",
                        "XERO_CLIENT_ID_PATH": "/StatementProcessor/XERO_CLIENT_ID",
                        "XERO_CLIENT_SECRET_PATH": "/StatementProcessor/XERO_CLIENT_SECRET",
                        "S3_BUCKET_NAME": f"dexero-statement-processor-{stage}",
                        "TENANT_CONTACTS_CONFIG_TABLE_NAME": TENANT_CONTACTS_CONFIG_TABLE_NAME,
                        "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                    },
                )
            ),
        )

        web.add_to_role_policy(parameter_policy)

        #endregion ---------- AppRunner ----------
