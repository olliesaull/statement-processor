import aws_cdk as cdk

from stacks.statement_processor import StatementProcessorStack


app = cdk.App()

statement_processor_dev = StatementProcessorStack(
    app, "StatementProcessorStackDev",
    stage="dev",
    env=cdk.Environment(account="137288644766", region="eu-west-1"),
    domain_name=""
)

statement_processor_prod = StatementProcessorStack(
    app, "StatementProcessorStackProd",
    stage="prod",
    env=cdk.Environment(account="747310139457", region="eu-west-1"),
    domain_name=""
)


app.synth()
