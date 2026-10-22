"""Single stack for the group chat platform. Owner: role 5.

Everything the team deploys lives here: the two DynamoDB tables that form the
data contract, the Cognito user pool, the WebSocket API with one Lambda per
route, and the S3 + CloudFront site for the client. See docs/CONTRACTS.md.
"""
from pathlib import Path

from aws_cdk import BundlingOptions, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

from bundling import LocalPipBundler

LAMBDAS_DIR = Path(__file__).resolve().parent.parent / "lambdas"
RUNTIME = lambda_.Runtime.PYTHON_3_12


class GroupChatStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, owner: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.owner = owner

        # ---- Tables (docs/CONTRACTS.md section 4) -------------------------
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

        # ---- Cognito (role 1 owns the authorizer, role 5 owns the pool) ----
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"group-chat-{owner}",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8, require_symbols=False, require_uppercase=False, require_digits=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.app_client = self.user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            generate_secret=False,
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # ---- Lambdas, one per route (docs/CONTRACTS.md section 1) ---------
        common_env = {
            "CONNECTIONS_TABLE": self.connections.table_name,
            "MESSAGES_TABLE": self.messages.table_name,
        }
        self.fns = {
            name: self._lambda(name, common_env)
            for name in ("authorizer", "connect", "disconnect", "join_room", "send_message", "get_history")
        }
        self.fns["authorizer"].add_environment("USER_POOL_ID", self.user_pool.user_pool_id)
        self.fns["authorizer"].add_environment("APP_CLIENT_ID", self.app_client.user_pool_client_id)

        self.connections.grant_read_write_data(self.fns["connect"])
        self.connections.grant_read_write_data(self.fns["disconnect"])
        self.connections.grant_read_write_data(self.fns["join_room"])
        self.connections.grant_read_write_data(self.fns["send_message"])  # delete on GoneException
        self.connections.grant_read_data(self.fns["get_history"])
        self.messages.grant_read_data(self.fns["join_room"])
        self.messages.grant_read_write_data(self.fns["send_message"])
        self.messages.grant_read_data(self.fns["get_history"])

        # ---- WebSocket API -------------------------------------------------
        authorizer = authorizers.WebSocketLambdaAuthorizer(
            "ConnectAuthorizer",
            self.fns["authorizer"],
            identity_source=["route.request.querystring.token"],
        )
        self.api = apigw.WebSocketApi(
            self, "WebSocketApi",
            api_name=f"group-chat-{owner}",
            connect_route_options=apigw.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration("ConnectIntegration", self.fns["connect"]),
                authorizer=authorizer,
            ),
            disconnect_route_options=apigw.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration("DisconnectIntegration", self.fns["disconnect"]),
            ),
        )
        for route, fn_name in (("joinRoom", "join_room"), ("sendMessage", "send_message"), ("getHistory", "get_history")):
            self.api.add_route(
                route,
                integration=integrations.WebSocketLambdaIntegration(f"{fn_name}Integration", self.fns[fn_name]),
            )
        self.stage = apigw.WebSocketStage(self, "ProdStage", web_socket_api=self.api, stage_name="prod", auto_deploy=True)

        for name in ("join_room", "send_message", "get_history"):
            self.fns[name].add_environment("WS_ENDPOINT", self.stage.callback_url)
            self.stage.grant_management_api_access(self.fns[name])

        # ---- Client hosting (role 5b builds what goes in the bucket) -------
        self.client_bucket = s3.Bucket(
            self, "ClientBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.distribution = cloudfront.Distribution(
            self, "ClientDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.client_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )

        # ---- Outputs the client and tests read ----------------------------
        CfnOutput(self, "WebSocketUrl", value=self.stage.url)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "AppClientId", value=self.app_client.user_pool_client_id)
        CfnOutput(self, "ClientBucketName", value=self.client_bucket.bucket_name)
        CfnOutput(self, "ClientUrl", value=f"https://{self.distribution.distribution_domain_name}")

    def _lambda(self, name: str, env: dict) -> lambda_.Function:
        source = LAMBDAS_DIR / name
        return lambda_.Function(
            self, f"{name}Fn",
            function_name=f"group-chat-{self.owner}-{name}",
            runtime=RUNTIME,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                str(source),
                bundling=BundlingOptions(
                    image=RUNTIME.bundling_image,
                    local=LocalPipBundler(source),
                    command=["bash", "-c", "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"],
                ),
            ),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment=env,
        )
