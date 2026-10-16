"""Single stack for the group chat platform. Owner: role 5.

Tables are defined here first because they are the contract everyone codes
against. Cognito, the WebSocket API, Lambdas, and the client bucket follow.
"""
from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from constructs import Construct


class GroupChatStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.connections = ddb.Table(
            self, "Connections",
            partition_key=ddb.Attribute(name="connectionId", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiresAt",
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.connections.add_global_secondary_index(
            index_name="roomId-index",
            partition_key=ddb.Attribute(name="roomId", type=ddb.AttributeType.STRING),
        )

        self.messages = ddb.Table(
            self, "Messages",
            partition_key=ddb.Attribute(name="roomId", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sortKey", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # TODO (role 5): Cognito user pool + client, WebSocket API + routes,
        # Lambda functions wired to the tables, S3 + CloudFront for the client,
        # CfnOutputs for WS URL / user pool id / client id.
