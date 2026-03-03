import aws_cdk as cdk
from stacks.statement_processor import StatementProcessorStack

app = cdk.App()
PROD_DOMAIN_NAME = "cloudcathode.com"

statement_processor_dev = StatementProcessorStack(
    app,
    "StatementProcessorStackDev",
    stage="dev",
    env=cdk.Environment(account="137288644766", region="eu-west-1"),
    domain_name="",
)

statement_processor_prod = StatementProcessorStack(
    app,
    "StatementProcessorStackProd",
    stage="prod",
    env=cdk.Environment(account="747310139457", region="eu-west-1"),
    domain_name=PROD_DOMAIN_NAME,
)


app.synth()
