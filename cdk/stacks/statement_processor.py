from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apprunner as apprunner
from aws_cdk import aws_apprunner_alpha as apprunner_alpha
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk.aws_ecr_assets import DockerImageAsset
from constructs import Construct


class StatementProcessorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, stage: str, domain_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        stage = stage.lower()
        is_production: bool = stage == "prod"
        log_retention=logs.RetentionDays.THREE_MONTHS if is_production else logs.RetentionDays.ONE_WEEK

        TENANT_STATEMENTS_TABLE_NAME = "TenantStatementsTable"
        TENANT_CONTACTS_CONFIG_TABLE_NAME = "TenantContactsConfigTable"
        TENANT_DATA_TABLE_NAME = "TenantDataTable"
        S3_BUCKET_NAME = f"dexero-statement-processor-{stage}"
        APP_RUNNER_SERVICE_NAME = f"statement-processor-{stage}"

        NOTIFICATION_EMAILS = ["ollie@dotelastic.com", "james@dotelastic.com"]

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

        tenant_statements_table = dynamodb.Table(
            self, TENANT_STATEMENTS_TABLE_NAME,
            table_name=TENANT_STATEMENTS_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="StatementID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )
        # Allows filtering statements on whether they are marked as completed or not
        tenant_statements_table.add_global_secondary_index(
            index_name="TenantIDCompletedIndex",
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="Completed", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # Allows storing data for each item on a given statement
        tenant_statements_table.add_global_secondary_index(
            index_name="TenantIDStatementItemIDIndex",
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="StatementItemID", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        tenant_contacts_config_table = dynamodb.Table(
            self, TENANT_CONTACTS_CONFIG_TABLE_NAME,
            table_name=TENANT_CONTACTS_CONFIG_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ContactID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        tenant_data_table = dynamodb.Table(
            self, TENANT_DATA_TABLE_NAME,
            table_name=TENANT_DATA_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        #endregion ---------- DynamoDB ----------

        #region ---------- S3 ----------

        s3_bucket = s3.Bucket(
            self, S3_BUCKET_NAME,
            bucket_name=S3_BUCKET_NAME,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        #endregion ---------- S3 ----------

        #region ---------- Lambda ----------

        textraction_lambda_image = _lambda.EcrImageCode.from_asset_image(directory="") # TODO: Create function code

        textraction_lambda =  _lambda.Function(
            self, 
            "TextractionLambda",
            description="Perform statement textraction using Textract and PDF Plumber",
            code=textraction_lambda_image,
            memory_size=2048,
            timeout=Duration.seconds(900),
            log_retention=log_retention
        )

        # TODO: Add Lambda function envars and DDB / S3 permissions

        #endregion ---------- Lambda ----------

        #region ---------- AppRunner ----------

        statement_processor_instance_role = iam.Role(
            self,
            "Statement Processor App Runner Instance Role",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )
        statement_processor_instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricData",
                    "textract:StartDocumentAnalysis",
                    "textract:GetDocumentAnalysis",
                ],
                resources=["*"]
            )
        )

        tenant_statements_table.grant_read_write_data(statement_processor_instance_role)
        tenant_contacts_config_table.grant_read_write_data(statement_processor_instance_role)
        tenant_data_table.grant_read_write_data(statement_processor_instance_role)
        s3_bucket.grant_read_write(statement_processor_instance_role)

        apprunner_asset = DockerImageAsset(self, "AppRunnerImage", directory="../service/")
        web = apprunner_alpha.Service(
            self,
            "Statement Processor Website",
            instance_role=statement_processor_instance_role,
            memory=apprunner_alpha.Memory.FOUR_GB,
            cpu=apprunner_alpha.Cpu.ONE_VCPU,
            service_name=APP_RUNNER_SERVICE_NAME,
            source=apprunner_alpha.Source.from_asset(
                asset=apprunner_asset,
                image_configuration=apprunner_alpha.ImageConfiguration(
                    port=8080,
                    environment_variables={
                        "STAGE": "prod" if is_production else "dev",
                        "DOMAIN_NAME": domain_name,
                        "POWERTOOLS_SERVICE_NAME": "StatementProcessor",
                        "LOG_LEVEL": "DEBUG",
                        "MAX_UPLOAD_MB": "10",
                        "S3_BUCKET_NAME": S3_BUCKET_NAME,
                        "TENANT_CONTACTS_CONFIG_TABLE_NAME": TENANT_CONTACTS_CONFIG_TABLE_NAME,
                        "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                        "TENANT_DATA_TABLE_NAME": TENANT_DATA_TABLE_NAME,
                        "XERO_CLIENT_ID_PATH": "/StatementProcessor/XERO_CLIENT_ID",
                        "XERO_CLIENT_SECRET_PATH": "/StatementProcessor/XERO_CLIENT_SECRET"
                    },
                )
            ),
        )

        web.add_to_role_policy(parameter_policy)

        #endregion ---------- AppRunner ----------

        # region ---------- CloudWatch ----------
        # Use the actual App Runner–managed log group:
        # /aws/apprunner/{service-name}/{service-id}/application

        cfn_service: apprunner.CfnService = web.node.default_child  # type: ignore[assignment]
        service_id = cfn_service.attr_service_id
        service_name = APP_RUNNER_SERVICE_NAME

        app_logs_group = logs.LogGroup.from_log_group_name(
            self,
            "StatementProcessorAppRunnerApplicationLogs",
            log_group_name=f"/aws/apprunner/{service_name}/{service_id}/application",
        )

        error_metric_filter = logs.MetricFilter(
            self,
            "StatementProcessorAppRunnerErrorMetricFilter",
            log_group=app_logs_group,
            filter_pattern=logs.FilterPattern.literal("ERROR"),
            metric_namespace="StatementProcessorAppRunner/ApplicationLogs",
            metric_name="ErrorCount",
            default_value=0,
        )
        # Ensure the filter is created after the service
        error_metric_filter.node.add_dependency(cfn_service)

        error_metric = cloudwatch.Metric(
            namespace="StatementProcessorAppRunner/ApplicationLogs",
            metric_name="ErrorCount",
            statistic="Sum",
            period=Duration.minutes(1),
        )

        error_alarm = cloudwatch.Alarm(
            self,
            "StatementProcessorAppRunnerErrorAlarm",
            metric=error_metric,
            threshold=1,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        topic = sns.Topic(
            self,
            "StatementProcessorAppRunnerErrorTopic",
            display_name=f"Statement Processor {stage} App Errors",
        )
        for email in NOTIFICATION_EMAILS:
            topic.add_subscription(subs.EmailSubscription(email))
        error_alarm.add_alarm_action(cw_actions.SnsAction(topic))

        # endregion ---------- CloudWatch ----------
