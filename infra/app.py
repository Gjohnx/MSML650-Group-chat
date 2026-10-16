#!/usr/bin/env python3
"""CDK entry point. Owner: role 5."""
import aws_cdk as cdk

from stack import GroupChatStack

app = cdk.App()
GroupChatStack(app, "GroupChatStack")
app.synth()
