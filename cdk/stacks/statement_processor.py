import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
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
        log_retention = logs.RetentionDays.THREE_MONTHS if is_production else logs.RetentionDays.ONE_WEEK

        TENANT_STATEMENTS_TABLE_NAME = "TenantStatementsTable"
        TENANT_CONTACTS_CONFIG_TABLE_NAME = "TenantContactsConfigTable"
        TENANT_DATA_TABLE_NAME = "TenantDataTable"
        S3_BUCKET_NAME = f"dexero-statement-processor-{stage}"
        WEB_LAMBDA_FUNCTION_NAME = f"statement-processor-web-{stage}"

        NOTIFICATION_EMAILS = ["ollie@dotelastic.com", "james@dotelastic.com"]

        # region ---------- ParameterStore ----------

        # SSM Parameter Store Parameter ARNs, using wildcards to satisfy ssm:GetParametersByPath
        parameter_arns = [f"arn:aws:ssm:eu-west-1:{env.account}:parameter/StatementProcessor/*"]

        # Create a policy statement to grant SSM Parameter Store access and SecureString decryption permission
        parameter_policy = iam.PolicyStatement(actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "kms:Decrypt"], resources=parameter_arns)

        # endregion ---------- ParameterStore ----------

        # region ---------- DynamoDB ----------

        tenant_statements_table = dynamodb.Table(
            self,
            TENANT_STATEMENTS_TABLE_NAME,
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
            self,
            TENANT_CONTACTS_CONFIG_TABLE_NAME,
            table_name=TENANT_CONTACTS_CONFIG_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ContactID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        tenant_data_table = dynamodb.Table(
            self,
            TENANT_DATA_TABLE_NAME,
            table_name=TENANT_DATA_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        # endregion ---------- DynamoDB ----------

        # region ---------- S3 ----------

        s3_bucket = s3.Bucket(
            self,
            S3_BUCKET_NAME,
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

        # endregion ---------- S3 ----------

        # region ---------- Lambda ----------

        textraction_log_group = logs.LogGroup(
            self,
            "TextractionLambdaLogGroup",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY if not is_production else RemovalPolicy.RETAIN,
        )

        textraction_lambda_image = _lambda.EcrImageCode.from_asset_image(directory="../lambda_functions/textraction_lambda")

        textraction_lambda = _lambda.Function(
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

        # endregion ---------- Lambda ----------

        # region ---------- StepFunctions ----------

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

        # endregion ---------- StepFunctions ----------

        # region ---------- WebLambda ----------

        web_lambda_log_group = logs.LogGroup(
            self,
            "StatementProcessorWebLambdaLogGroup",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY if not is_production else RemovalPolicy.RETAIN,
        )

        web_lambda = _lambda.DockerImageFunction(
            self,
            "StatementProcessorWebLambda",
            function_name=WEB_LAMBDA_FUNCTION_NAME,
            description="Statement processor web app served through Lambda Function URL",
            code=_lambda.DockerImageCode.from_image_asset(directory="../service"),
            memory_size=1024,
            timeout=Duration.seconds(30),
            log_group=web_lambda_log_group,
            environment={
                "STAGE": "prod" if is_production else "dev",
                "DOMAIN_NAME": domain_name,
                "POWERTOOLS_SERVICE_NAME": "StatementProcessor",
                "LOG_LEVEL": "DEBUG",
                "MAX_UPLOAD_MB": "6",
                "S3_BUCKET_NAME": S3_BUCKET_NAME,
                "TENANT_CONTACTS_CONFIG_TABLE_NAME": TENANT_CONTACTS_CONFIG_TABLE_NAME,
                "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                "TENANT_DATA_TABLE_NAME": TENANT_DATA_TABLE_NAME,
                "TEXTRACTION_STATE_MACHINE_ARN": state_machine.state_machine_arn,
                "XERO_CLIENT_ID_PATH": "/StatementProcessor/XERO_CLIENT_ID",
                "XERO_CLIENT_SECRET_PATH": "/StatementProcessor/XERO_CLIENT_SECRET",
                "SESSION_FERNET_KEY_PATH": "/StatementProcessor/SESSION_FERNET_KEY",
                "FLASK_SECRET_KEY_PATH": "/StatementProcessor/FLASK_SECRET_KEY",
                "XERO_REDIRECT_URI": "https://cloudcathode.com/callback",
            },
        )

        web_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricData",
                    "textract:StartDocumentAnalysis",
                    "textract:GetDocumentAnalysis",
                    "states:StartExecution",
                ],
                resources=["*"],
            )
        )
        web_lambda.add_to_role_policy(parameter_policy)

        tenant_statements_table.grant_read_write_data(web_lambda)
        tenant_contacts_config_table.grant_read_write_data(web_lambda)
        tenant_data_table.grant_read_write_data(web_lambda)
        s3_bucket.grant_read_write(web_lambda)

        # TODO: Does Function URL Auth Type need updating?
        web_lambda.add_function_url(auth_type=_lambda.FunctionUrlAuthType.NONE)
        web_lambda.add_permission(
            "StatementProcessorWebLambdaInvokeFunctionPermission",
            principal=iam.AnyPrincipal(),
            action="lambda:InvokeFunction",
        )

        # endregion ---------- WebLambda ----------

        # region ---------- CloudWatch ----------

        lambda_error_topic = sns.Topic(
            self,
            "StatementProcessorLambdaErrorTopic",
            display_name=f"Statement Processor {stage} Lambda Errors",
        )
        for email in NOTIFICATION_EMAILS:
            lambda_error_topic.add_subscription(subs.EmailSubscription(email))

        lambda_alarm_targets: list[tuple[str, logs.ILogGroup]] = [
            ("StatementProcessorWebLambda", web_lambda_log_group),
            ("TextractionLambda", textraction_log_group),
        ]

        for lambda_name, lambda_log_group in lambda_alarm_targets:
            metric_name = f"{lambda_name}ErrorCount"
            metric_namespace = "StatementProcessor/LambdaApplicationLogs"

            logs.MetricFilter(
                self,
                f"{lambda_name}ErrorMetricFilter",
                log_group=lambda_log_group,
                filter_pattern=logs.FilterPattern.any_term("ERROR", "Task timed out", "Process exited before completing request"),
                metric_namespace=metric_namespace,
                metric_name=metric_name,
                metric_value="1",
                default_value=0,
            )

            error_metric = cloudwatch.Metric(
                namespace=metric_namespace,
                metric_name=metric_name,
                statistic="Sum",
                period=Duration.minutes(1),
            )

            error_alarm = cloudwatch.Alarm(
                self,
                f"{lambda_name}ErrorAlarm",
                alarm_name=f"{lambda_name}ErrorAlarm-{stage}",
                metric=error_metric,
                threshold=1,
                evaluation_periods=1,
                datapoints_to_alarm=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            error_alarm.add_alarm_action(cw_actions.SnsAction(lambda_error_topic))

        # endregion ---------- CloudWatch ----------
