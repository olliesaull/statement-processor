import aws_cdk as cdk
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
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from aws_cdk.aws_ecr_assets import DockerImageAsset
from aws_cdk.aws_lambda import (
    Handler,
    Runtime,
)
from constructs import Construct


class StatementProcessorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, env: cdk.Environment, stage: str, domain_name: str, **kwargs) -> None:
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
        parameter_arns = [f"arn:aws:ssm:eu-west-1:{env.account}:parameter/StatementProcessor/*"]

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
        s3_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowTextractReadStatements",
                principals=[iam.ServicePrincipal("textract.amazonaws.com")],
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[s3_bucket.arn_for_objects("*")],
                conditions={
                    "StringEquals": {"AWS:SourceAccount": env.account},
                    "ArnLike": {"AWS:SourceArn": f"arn:aws:textract:{env.region}:{env.account}:*"},
                },
            )
        )

        #endregion ---------- S3 ----------

        #region ---------- Lambda ----------

        textraction_log_group = logs.LogGroup(
            self,
            "TextractionLambdaLogGroup",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY if not is_production else RemovalPolicy.RETAIN,
        )

        textraction_lambda_image = _lambda.EcrImageCode.from_asset_image(directory="../lambda_functions/textraction_lambda")

        textraction_lambda =  _lambda.Function(
            self, 
            "TextractionLambda",
            description="Perform statement textraction using Textract and PDF Plumber",
            code=textraction_lambda_image,
            memory_size=2048,
            handler=Handler.FROM_IMAGE,
            runtime=Runtime.FROM_IMAGE,
            timeout=Duration.seconds(60),
            log_group=textraction_log_group,
            environment={
                "STAGE": "prod" if is_production else "dev",
                "S3_BUCKET_NAME": S3_BUCKET_NAME,
                "TENANT_CONTACTS_CONFIG_TABLE_NAME": TENANT_CONTACTS_CONFIG_TABLE_NAME,
                "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                "TENANT_DATA_TABLE_NAME": TENANT_DATA_TABLE_NAME,
            },
        )

        textraction_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["textract:GetDocumentAnalysis"],
                resources=["*"],
            )
        )

        tenant_statements_table.grant_read_write_data(textraction_lambda)
        tenant_contacts_config_table.grant_read_write_data(textraction_lambda)
        tenant_data_table.grant_read_write_data(textraction_lambda)
        s3_bucket.grant_read_write(textraction_lambda)

        #endregion ---------- Lambda ----------

        #region ---------- StepFunctions ----------

        start_textract = tasks.CallAwsService(
            self,
            "StartTextractDocumentAnalysis",
            service="textract",
            action="startDocumentAnalysis",
            iam_resources=["*"],
            parameters={
                "DocumentLocation": {
                    "S3Object": {
                        "Bucket": sfn.JsonPath.string_at("$.s3Bucket"),
                        "Name": sfn.JsonPath.string_at("$.pdfKey"),
                    }
                },
                "FeatureTypes": ["TABLES"],
            },
            result_path="$.textractJob",
        )

        wait_for_textract = sfn.Wait(
            self,
            "WaitForTextract",
            time=sfn.WaitTime.duration(Duration.seconds(10)),
        )

        get_textract_status = tasks.CallAwsService(
            self,
            "GetTextractStatus",
            service="textract",
            action="getDocumentAnalysis",
            iam_resources=["*"],
            parameters={
                "JobId": sfn.JsonPath.string_at("$.textractJob.JobId"),
                "MaxResults": 1,
            },
            result_selector={
                "JobStatus": sfn.JsonPath.string_at("$.JobStatus"),
            },
            result_path="$.textractStatus",
        )

        process_statement = tasks.LambdaInvoke(
            self,
            "ProcessStatement",
            lambda_function=textraction_lambda,
            payload=sfn.TaskInput.from_object(
                {
                    "jobId": sfn.JsonPath.string_at("$.textractJob.JobId"),
                    "tenantId": sfn.JsonPath.string_at("$.tenant_id"),
                    "contactId": sfn.JsonPath.string_at("$.contact_id"),
                    "statementId": sfn.JsonPath.string_at("$.statement_id"),
                    "s3Bucket": sfn.JsonPath.string_at("$.s3Bucket"),
                    "pdfKey": sfn.JsonPath.string_at("$.pdfKey"),
                    "jsonKey": sfn.JsonPath.string_at("$.jsonKey"),
                }
            ),
            result_path="$.lambdaResult",
        )

        textract_finished = sfn.Choice(self, "IsTextractFinished?")
        textract_finished.when(
            sfn.Condition.string_equals("$.textractStatus.JobStatus", "SUCCEEDED"),
            process_statement,
        )
        textract_finished.when(
            sfn.Condition.string_equals("$.textractStatus.JobStatus", "PARTIAL_SUCCESS"),
            process_statement,
        )
        textract_finished.when(
            sfn.Condition.string_equals("$.textractStatus.JobStatus", "FAILED"),
            sfn.Fail(self, "TextractFailed"),
        )
        textract_finished.otherwise(wait_for_textract)

        wait_for_textract.next(get_textract_status)
        get_textract_status.next(textract_finished)
        start_textract.next(wait_for_textract)

        state_machine = sfn.StateMachine(
            self,
            "TextractionStateMachine",
            state_machine_name=f"TextractionStateMachine-{stage}",
            definition_body=sfn.DefinitionBody.from_chainable(start_textract),
            timeout=Duration.minutes(30),
        )

        state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis"],
                resources=["*"],
            )
        )
        s3_bucket.grant_read(state_machine.role)

        textraction_lambda.grant_invoke(state_machine.role)

        #endregion ---------- StepFunctions ----------

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
                    "states:StartExecution",
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
                        "TEXTRACTION_STATE_MACHINE_ARN": state_machine.state_machine_arn,
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
