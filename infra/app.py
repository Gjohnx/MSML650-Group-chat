#!/usr/bin/env python3
"""CDK entry point. Owner: role 5.

Each teammate deploys their own copy of the stack so nobody clobbers anyone:

    cdk deploy --profile msml650 -c owner=<yourname>

The demo stack is `owner=demo`, deployed only by role 5. Region is pinned to
us-east-1; the account comes from whichever profile you pass.
"""
import os
import re

import aws_cdk as cdk

from stack import GroupChatStack

app = cdk.App()

owner = app.node.try_get_context("owner") or "demo"
if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", owner):
    raise SystemExit(f"owner must be 1 to 16 lowercase letters or digits, got {owner!r}")

GroupChatStack(
    app,
    f"GroupChat-{owner}",
    owner=owner,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region="us-east-1",
    ),
)
cdk.Tags.of(app).add("project", "msml650-group-chat")
cdk.Tags.of(app).add("owner", owner)
app.synth()
