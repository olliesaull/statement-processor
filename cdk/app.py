#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.statement_processor import StatementProcessorStack


app = cdk.App()

statement_processor_stack = StatementProcessorStack(
    app, "StatementProcessorStack",
    stage="prod",
    env=cdk.Environment(account="747310139457", region="eu-west-1"),
    domain_name=""
    )
app.synth()
