import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apprunner as apprunner
from aws_cdk import aws_apprunner_alpha as apprunner_alpha
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
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
        configured_domain = (domain_name or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
        apex_domain = configured_domain[4:] if configured_domain.startswith("www.") else configured_domain
        cloudfront_aliases: list[str] = [apex_domain, f"www.{apex_domain}"] if apex_domain else []
        if is_production and not apex_domain:
            raise ValueError("domain_name must be set for prod deployments so CloudFront aliases and OAuth callback routing are configured for the public site.")
        TENANT_STATEMENTS_TABLE_NAME = "TenantStatementsTable"
        TENANT_DATA_TABLE_NAME = "TenantDataTable"
        TENANT_BILLING_TABLE_NAME = "TenantBillingTable"
        TENANT_TOKEN_LEDGER_TABLE_NAME = "TenantTokenLedgerTable"
        STRIPE_EVENT_STORE_TABLE_NAME = "StripeEventStoreTable"
        S3_BUCKET_NAME = f"dexero-statement-processor-{stage}"
        STATIC_ASSETS_BUCKET_NAME = f"dexero-statement-processor-{stage}-assets"
        APP_RUNNER_SERVICE_NAME = f"statement-processor-{stage}"
        CLOUDFRONT_CERTIFICATE_ARN = "arn:aws:acm:us-east-1:747310139457:certificate/1e702711-0bd2-4806-b60d-c7ec45b93eac"
        CLOUDFRONT_CACHE_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
        CLOUDFRONT_ORIGIN_REQUEST_POLICY_ID = "27f26a87-73c7-4734-9f02-b10dbda0774c"

        NOTIFICATION_EMAILS = ["ollie@dotelastic.com", "james@dotelastic.com"]

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

        tenant_data_table = dynamodb.Table(
            self,
            TENANT_DATA_TABLE_NAME,
            table_name=TENANT_DATA_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        tenant_billing_table = dynamodb.Table(
            self,
            TENANT_BILLING_TABLE_NAME,
            table_name=TENANT_BILLING_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        tenant_token_ledger_table = dynamodb.Table(
            self,
            TENANT_TOKEN_LEDGER_TABLE_NAME,
            table_name=TENANT_TOKEN_LEDGER_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="LedgerEntryID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )

        stripe_event_store_table = dynamodb.Table(
            self,
            STRIPE_EVENT_STORE_TABLE_NAME,
            table_name=STRIPE_EVENT_STORE_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="StripeEventID", type=dynamodb.AttributeType.STRING),
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
        static_assets_bucket = s3.Bucket(
            self,
            STATIC_ASSETS_BUCKET_NAME,
            bucket_name=STATIC_ASSETS_BUCKET_NAME,
            removal_policy=RemovalPolicy.RETAIN if is_production else RemovalPolicy.DESTROY,
        )
        # endregion ---------- S3 ----------

        # region ---------- Lambda ----------

        extraction_log_group = logs.LogGroup(
            self,
            "ExtractionLambdaLogGroup",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY if not is_production else RemovalPolicy.RETAIN,
        )

        extraction_lambda_image = _lambda.EcrImageCode.from_asset_image(
            directory="../lambda_functions/extraction_lambda",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        extraction_lambda = _lambda.Function(
            self,
            "ExtractionLambda",
            description="Perform statement extraction using Bedrock Haiku",
            code=extraction_lambda_image,
            memory_size=2048,
            handler=Handler.FROM_IMAGE,
            runtime=Runtime.FROM_IMAGE,
            architecture=_lambda.Architecture.ARM_64,
            timeout=Duration.seconds(660),
            log_group=extraction_log_group,
            environment={
                "STAGE": "prod" if is_production else "dev",
                "S3_BUCKET_NAME": S3_BUCKET_NAME,
                "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                "TENANT_DATA_TABLE_NAME": TENANT_DATA_TABLE_NAME,
                "TENANT_BILLING_TABLE_NAME": TENANT_BILLING_TABLE_NAME,
                "TENANT_TOKEN_LEDGER_TABLE_NAME": TENANT_TOKEN_LEDGER_TABLE_NAME,
                "POWERTOOLS_SERVICE_NAME": "ExtractionLambda",
            },
        )

        extraction_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/eu.anthropic.claude-haiku-4-5-*",
                ],
            )
        )

        tenant_statements_table.grant_read_write_data(extraction_lambda)
        tenant_data_table.grant_read_write_data(extraction_lambda)
        tenant_billing_table.grant_read_write_data(extraction_lambda)
        tenant_token_ledger_table.grant_read_write_data(extraction_lambda)
        s3_bucket.grant_read_write(extraction_lambda)

        # endregion ---------- Lambda ----------

        # region ---------- Tenant Erasure Lambda ----------

        tenant_erasure_log_group = logs.LogGroup(
            self,
            "TenantErasureLambdaLogGroup",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY if not is_production else RemovalPolicy.RETAIN,
        )

        tenant_erasure_lambda_image = _lambda.EcrImageCode.from_asset_image(
            directory="../lambda_functions/tenant_erasure_lambda",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        tenant_erasure_lambda = _lambda.Function(
            self,
            "TenantErasureLambda",
            description="Daily scan and erasure of disconnected tenant data",
            code=tenant_erasure_lambda_image,
            memory_size=512,
            handler=Handler.FROM_IMAGE,
            runtime=Runtime.FROM_IMAGE,
            architecture=_lambda.Architecture.ARM_64,
            timeout=Duration.seconds(300),
            log_group=tenant_erasure_log_group,
            environment={
                "STAGE": "prod" if is_production else "dev",
                "S3_BUCKET_NAME": S3_BUCKET_NAME,
                "TENANT_DATA_TABLE_NAME": TENANT_DATA_TABLE_NAME,
                "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                "TENANT_BILLING_TABLE_NAME": TENANT_BILLING_TABLE_NAME,
                "STRIPE_API_KEY_SSM_PATH": "/StatementProcessor/STRIPE_API_KEY",
                "POWERTOOLS_SERVICE_NAME": "TenantErasureLambda",
            },
        )

        tenant_data_table.grant_read_write_data(tenant_erasure_lambda)
        tenant_statements_table.grant_read_write_data(tenant_erasure_lambda)
        tenant_billing_table.grant_read_data(tenant_erasure_lambda)
        s3_bucket.grant_read_write(tenant_erasure_lambda)
        # Allow erasure lambda to fetch Stripe API key for subscription cancellation.
        tenant_erasure_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=["arn:aws:ssm:eu-west-1:747310139457:parameter/StatementProcessor/STRIPE_API_KEY"],
            )
        )

        # Daily at 02:00 UTC.
        events.Rule(
            self,
            "TenantErasureSchedule",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            targets=[events_targets.LambdaFunction(tenant_erasure_lambda)],
        )

        # endregion ---------- Tenant Erasure Lambda ----------

        # region ---------- StepFunctions ----------

        process_statement = tasks.LambdaInvoke(
            self,
            "ProcessStatement",
            lambda_function=extraction_lambda,
            payload=sfn.TaskInput.from_object(
                {
                    "tenantId": sfn.JsonPath.string_at("$.tenant_id"),
                    "contactId": sfn.JsonPath.string_at("$.contact_id"),
                    "statementId": sfn.JsonPath.string_at("$.statement_id"),
                    "s3Bucket": sfn.JsonPath.string_at("$.s3Bucket"),
                    "pdfKey": sfn.JsonPath.string_at("$.pdfKey"),
                    "jsonKey": sfn.JsonPath.string_at("$.jsonKey"),
                    "pageCount": sfn.JsonPath.number_at("$.pageCount"),
                }
            ),
            result_path="$.lambdaResult",
        )

        workflow_succeeded = sfn.Succeed(self, "StatementProcessed")
        workflow_failed = sfn.Fail(self, "StatementProcessingFailed")
        process_statement_outcome = sfn.Choice(self, "DidStatementProcessingSucceed?")
        process_statement_outcome.when(sfn.Condition.string_equals("$.lambdaResult.Payload.status", "ok"), workflow_succeeded)
        process_statement_outcome.otherwise(workflow_failed)

        process_statement.next(process_statement_outcome)

        state_machine = sfn.StateMachine(
            self,
            "ExtractionStateMachine",
            state_machine_name=f"ExtractionStateMachine-{stage}",
            definition_body=sfn.DefinitionBody.from_chainable(process_statement),
            timeout=Duration.seconds(720),
        )

        extraction_lambda.grant_invoke(state_machine.role)

        # endregion ---------- StepFunctions ----------

        # region ---------- AppRunner ----------

        statement_processor_instance_role = iam.Role(
            self,
            "Statement Processor App Runner Instance Role",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )
        statement_processor_instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricData",
                    "states:StartExecution",
                ],
                resources=["*"],
            )
        )
        tenant_statements_table.grant_read_write_data(statement_processor_instance_role)
        tenant_data_table.grant_read_write_data(statement_processor_instance_role)
        tenant_billing_table.grant_read_write_data(statement_processor_instance_role)
        tenant_token_ledger_table.grant_read_write_data(statement_processor_instance_role)
        stripe_event_store_table.grant_read_write_data(statement_processor_instance_role)
        s3_bucket.grant_read_write(statement_processor_instance_role)
        # Allow the running container to fetch Xero OAuth credentials and the Flask
        # secret key from SSM at startup. Secrets are no longer injected as CloudFormation
        # environment variables so they are never stored in the stack template.
        statement_processor_instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameters"],
                resources=["arn:aws:ssm:eu-west-1:747310139457:parameter/StatementProcessor/*"],
            )
        )
        # Allow the web app to send login notification emails via SES.
        statement_processor_instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[f"arn:aws:ses:eu-west-1:{env.account}:identity/*"],
            )
        )

        auto_scaling_configuration = apprunner_alpha.AutoScalingConfiguration(
            self,
            "AutoScalingConfiguration",
            auto_scaling_configuration_name="SingleInstance",
            max_concurrency=200,
            max_size=1,
        )

        apprunner_asset = ecr_assets.DockerImageAsset(self, "AppRunnerImage", directory="../service/")
        web = apprunner_alpha.Service(
            self,
            "Statement Processor Website",
            instance_role=statement_processor_instance_role,
            memory=apprunner_alpha.Memory.ONE_GB,
            cpu=apprunner_alpha.Cpu.HALF_VCPU,
            service_name=APP_RUNNER_SERVICE_NAME,
            auto_scaling_configuration=auto_scaling_configuration,
            health_check=apprunner_alpha.HealthCheck.http(
                path="/healthz",
                interval=Duration.seconds(10),
                timeout=Duration.seconds(5),
                healthy_threshold=1,
                unhealthy_threshold=5,
            ),
            source=apprunner_alpha.Source.from_asset(
                asset=apprunner_asset,
                image_configuration=apprunner_alpha.ImageConfiguration(
                    port=8080,
                    environment_variables={
                        "STAGE": "prod" if is_production else "dev",
                        "DOMAIN_NAME": apex_domain,
                        "POWERTOOLS_SERVICE_NAME": "StatementProcessor",
                        "LOG_LEVEL": "DEBUG",
                        "MAX_UPLOAD_MB": "10",
                        "S3_BUCKET_NAME": S3_BUCKET_NAME,
                        "TENANT_STATEMENTS_TABLE_NAME": TENANT_STATEMENTS_TABLE_NAME,
                        "TENANT_DATA_TABLE_NAME": TENANT_DATA_TABLE_NAME,
                        "TENANT_BILLING_TABLE_NAME": TENANT_BILLING_TABLE_NAME,
                        "TENANT_TOKEN_LEDGER_TABLE_NAME": TENANT_TOKEN_LEDGER_TABLE_NAME,
                        "STRIPE_EVENT_STORE_TABLE_NAME": STRIPE_EVENT_STORE_TABLE_NAME,
                        "EXTRACTION_STATE_MACHINE_ARN": state_machine.state_machine_arn,
                        # SSM paths for secrets — read at container startup. Storing paths
                        # (not values) here means changing a secret only requires an SSM
                        # update, not a CDK redeploy.
                        "XERO_CLIENT_ID_SSM_PATH": "/StatementProcessor/XERO_CLIENT_ID",
                        "XERO_CLIENT_SECRET_SSM_PATH": "/StatementProcessor/XERO_CLIENT_SECRET",
                        "FLASK_SECRET_KEY_SSM_PATH": "/StatementProcessor/FLASK_SECRET_KEY",
                        # Stripe — secret keys fetched from SSM at startup
                        "STRIPE_API_KEY_SSM_PATH": "/StatementProcessor/STRIPE_API_KEY",
                        "STRIPE_WEBHOOK_SECRET_SSM_PATH": "/StatementProcessor/STRIPE_WEBHOOK_SECRET",
                        # Stripe — non-secret config (plain env vars)
                        "STRIPE_PRODUCT_ID": "prod_UBMoFkqStKFcjg",
                        "STRIPE_PRICE_PER_TOKEN_PENCE": "10",
                        "STRIPE_CURRENCY": "gbp",
                        "STRIPE_MIN_TOKENS": "10",
                        "STRIPE_MAX_TOKENS": "10000",
                        # Stripe subscription tier Price IDs
                        "STRIPE_PRICE_ID_TIER_50": "price_1TLnqeEGVTFRqrO3kJaMmw2H",
                        "STRIPE_PRICE_ID_TIER_200": "price_1TLnqfEGVTFRqrO3TsiaazKs",
                        "STRIPE_PRICE_ID_TIER_500": "price_1TLnqgEGVTFRqrO3DLZHXuTt",
                        # CloudFront-only access — Nginx rejects requests missing this header
                        # when STAGE=prod (see start.sh configure_nginx).
                        "X_STATEMENT_CF": "R7K92M3XWPQ4J",
                    },
                ),
            ),
        )

        cfn_service: apprunner.CfnService = web.node.default_child  # type: ignore[assignment]
        app_runner_service_domain = cfn_service.attr_service_url

        cloudfront_cache_policy = cloudfront.CachePolicy.from_cache_policy_id(self, "StatementProcessorCloudFrontCachePolicy", CLOUDFRONT_CACHE_POLICY_ID)
        cloudfront_origin_request_policy = cloudfront.OriginRequestPolicy.from_origin_request_policy_id(self, "StatementProcessorCloudFrontOriginRequestPolicy", CLOUDFRONT_ORIGIN_REQUEST_POLICY_ID)
        cloudfront_default_behavior = cloudfront.BehaviorOptions(
            # App Runner is a custom HTTPS origin; CloudFront OAC is not supported for this origin type.
            origin=origins.HttpOrigin(
                app_runner_service_domain,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                custom_headers={"X-Statement-CF": "R7K92M3XWPQ4J"},
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            compress=True,
            cache_policy=cloudfront_cache_policy,
            origin_request_policy=cloudfront_origin_request_policy,
        )
        # Keep the /static prefix in S3 so the object keys match local Flask static URLs.
        cloudfront_static_behavior = cloudfront.BehaviorOptions(
            origin=origins.S3BucketOrigin.with_origin_access_control(static_assets_bucket),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            compress=True,
            cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
        )

        cloudfront_distribution_props: dict[str, object] = {
            "default_behavior": cloudfront_default_behavior,
            "additional_behaviors": {
                "/static/*": cloudfront_static_behavior,
            },
            "price_class": cloudfront.PriceClass.PRICE_CLASS_ALL,
            "http_version": cloudfront.HttpVersion.HTTP2_AND_3,
            "enable_ipv6": True,
            "minimum_protocol_version": cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            "comment": f"Statement Processor {stage} distribution",
        }
        if is_production:
            cloudfront_certificate = acm.Certificate.from_certificate_arn(self, "StatementProcessorCloudFrontCertificate", CLOUDFRONT_CERTIFICATE_ARN)
            cloudfront_distribution_props["certificate"] = cloudfront_certificate
            cloudfront_distribution_props["domain_names"] = cloudfront_aliases

        cloudfront.Distribution(self, "StatementProcessorDistribution", **cloudfront_distribution_props)

        # endregion ---------- AppRunner ----------

        # region ---------- CloudWatch ----------

        runtime_error_topic = sns.Topic(
            self,
            "StatementProcessorRuntimeErrorTopic",
            display_name=f"Statement Processor {stage} Runtime Errors",
        )
        for email in NOTIFICATION_EMAILS:
            runtime_error_topic.add_subscription(subs.EmailSubscription(email))

        service_id = cfn_service.attr_service_id
        app_logs_group = logs.LogGroup.from_log_group_name(
            self,
            "StatementProcessorAppRunnerApplicationLogs",
            log_group_name=f"/aws/apprunner/{APP_RUNNER_SERVICE_NAME}/{service_id}/application",
        )

        app_error_metric_filter = logs.MetricFilter(
            self,
            "StatementProcessorAppRunnerErrorMetricFilter",
            log_group=app_logs_group,
            filter_pattern=logs.FilterPattern.literal("ERROR"),
            metric_namespace="StatementProcessorAppRunner/ApplicationLogs",
            metric_name="ErrorCount",
            metric_value="1",
            default_value=0,
        )
        app_error_metric_filter.node.add_dependency(cfn_service)

        app_error_metric = cloudwatch.Metric(
            namespace="StatementProcessorAppRunner/ApplicationLogs",
            metric_name="ErrorCount",
            statistic="Sum",
            period=Duration.minutes(1),
        )

        app_error_alarm = cloudwatch.Alarm(
            self,
            "StatementProcessorAppRunnerErrorAlarm",
            alarm_name=f"StatementProcessorAppRunnerErrorAlarm-{stage}",
            metric=app_error_metric,
            threshold=1,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        app_error_alarm.add_alarm_action(cw_actions.SnsAction(runtime_error_topic))

        lambda_alarm_targets: list[tuple[str, logs.ILogGroup]] = [
            ("ExtractionLambda", extraction_log_group),
            ("TenantErasureLambda", tenant_erasure_log_group),
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
            error_alarm.add_alarm_action(cw_actions.SnsAction(runtime_error_topic))

        # endregion ---------- CloudWatch ----------
