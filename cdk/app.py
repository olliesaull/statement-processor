#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.statement_processor import StatementProcessorStack


app = cdk.App()

review_replier_stack = StatementProcessorStack(
    app, "StatementProcessorStack",
    stage="prod",
    env=cdk.Environment(account="747310139457", region="eu-west-1"),
    )
app.synth()
