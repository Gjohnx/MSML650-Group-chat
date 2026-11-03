"""CloudWatch dashboard for the group chat stack. Owner: role 5a.

One row per success criterion, using only metrics AWS already emits, so no
Lambda needs extra code for it to work. Built per owner, so everyone gets a
dashboard for their own stack and the demo stack gets the one we present.

What each row can and cannot prove:
- Criterion 1: API Gateway ConnectCount next to ClientError. A rejected
  connect shows up as a 4xx ClientError, not as a connection.
- Criterion 2: server-side time only (integration latency and Lambda
  duration). True end-to-end latency includes the network to each client and
  comes from the client readout and the load test report. Use this row to show
  the server's share of the 200 ms budget.
- Criterion 3: GoneException log lines from sendMessage (each one is a stale
  connection being deleted) and $disconnect invocations.
- Criterion 4: MessageCount, errors, and throttles under load. Zero errors and
  zero throttles during the load test is the evidence.
- Criterion 5: getHistory and joinRoom invocations, errors, and duration.
"""
from aws_cdk import Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_lambda as lambda_

PERIOD = Duration.minutes(1)
FULL = 24  # dashboard grid width


def _api_metric(api: apigw.WebSocketApi, stage: str, name: str, stat: str = "Sum", label: str | None = None):
    return cw.Metric(
        namespace="AWS/ApiGateway",
        metric_name=name,
        dimensions_map={"ApiId": api.api_id, "Stage": stage},
        statistic=stat,
        period=PERIOD,
        label=label or name,
    )


def _per_fn(fns: dict, names, make, label_suffix=""):
    return [make(fns[n]).with_(label=f"{n}{label_suffix}") for n in names]


def build_dashboard(
    stack: Stack,
    *,
    owner: str,
    api: apigw.WebSocketApi,
    stage_name: str,
    fns: dict[str, lambda_.Function],
    connections: ddb.Table,
    messages: ddb.Table,
) -> cw.Dashboard:
    all_fns = list(fns)
    send_log_group = f"/aws/lambda/{fns['send_message'].function_name}"

    dash = cw.Dashboard(
        stack, "Dashboard",
        dashboard_name=f"group-chat-{owner}",
        default_interval=Duration.hours(3),
        period_override=cw.PeriodOverride.AUTO,
    )

    def header(text: str):
        dash.add_widgets(cw.TextWidget(markdown=text, width=FULL, height=1))

    dash.add_widgets(cw.TextWidget(
        width=FULL, height=2,
        markdown=(
            f"# Group chat: `{owner}` stack\n"
            "One row per success criterion. Server-side numbers only; end-to-end latency "
            "comes from the client readout and the load test report."
        ),
    ))

    # ---- Criterion 1: unauthorized connections rejected -------------------
    header("## 1. Unauthorized connections rejected")
    dash.add_widgets(
        cw.GraphWidget(
            title="Connects vs rejected (4xx)", width=12,
            left=[
                _api_metric(api, stage_name, "ConnectCount", label="connections opened"),
                _api_metric(api, stage_name, "ClientError", label="4xx (rejected or bad request)"),
            ],
        ),
        cw.GraphWidget(
            title="Authorizer invocations and errors", width=12,
            left=[
                fns["authorizer"].metric_invocations(period=PERIOD, label="invocations"),
                fns["authorizer"].metric_errors(period=PERIOD, label="errors (crashes, not denials)"),
            ],
        ),
    )

    # ---- Criterion 2: latency ----------------------------------------------
    header("## 2. Broadcast latency (server side, budget is 200 ms end to end)")
    budget = cw.HorizontalAnnotation(value=200, label="200 ms budget", color=cw.Color.RED)
    dash.add_widgets(
        cw.GraphWidget(
            title="API Gateway integration latency (ms)", width=12,
            left=[
                _api_metric(api, stage_name, "IntegrationLatency", "p50", "p50"),
                _api_metric(api, stage_name, "IntegrationLatency", "p95", "p95"),
                _api_metric(api, stage_name, "IntegrationLatency", "p99", "p99"),
            ],
            left_annotations=[budget],
        ),
        cw.GraphWidget(
            title="sendMessage Lambda duration (ms)", width=12,
            left=[
                fns["send_message"].metric_duration(statistic="p50", period=PERIOD, label="p50"),
                fns["send_message"].metric_duration(statistic="p95", period=PERIOD, label="p95"),
                fns["send_message"].metric_duration(statistic="p99", period=PERIOD, label="p99"),
            ],
            left_annotations=[budget],
        ),
    )

    # ---- Criterion 3: stale connections cleaned up --------------------------
    header("## 3. Stale connections cleaned up")
    dash.add_widgets(
        cw.LogQueryWidget(
            title="Stale sockets found during broadcast (GoneException)", width=12,
            log_group_names=[send_log_group],
            view=cw.LogQueryVisualizationType.BAR,
            query_lines=[
                "filter @message like /GoneException/",
                "stats count() as stale_connections by bin(5m)",
            ],
        ),
        cw.GraphWidget(
            title="Connects vs disconnects", width=12,
            left=[
                fns["connect"].metric_invocations(period=PERIOD, label="$connect"),
                fns["disconnect"].metric_invocations(period=PERIOD, label="$disconnect"),
            ],
        ),
    )

    # ---- Criterion 4: concurrent load without loss --------------------------
    header("## 4. Concurrent load without message loss")
    dash.add_widgets(
        cw.GraphWidget(
            title="WebSocket messages and server errors", width=8,
            left=[
                _api_metric(api, stage_name, "MessageCount", label="messages (in + out)"),
                _api_metric(api, stage_name, "ExecutionError", label="execution errors"),
                _api_metric(api, stage_name, "IntegrationError", label="integration errors"),
            ],
        ),
        cw.GraphWidget(
            title="Lambda errors and throttles", width=8,
            left=_per_fn(fns, all_fns, lambda f: f.metric_errors(period=PERIOD), " errors")
            + _per_fn(fns, ["send_message"], lambda f: f.metric_throttles(period=PERIOD), " throttles"),
        ),
        cw.GraphWidget(
            title="DynamoDB throttled requests", width=8,
            left=[
                connections.metric("ThrottledRequests", statistic="Sum", period=PERIOD, label="Connections"),
                messages.metric("ThrottledRequests", statistic="Sum", period=PERIOD, label="Messages"),
            ],
        ),
    )

    # ---- Criterion 5: history on join ---------------------------------------
    header("## 5. History retrieved on join")
    dash.add_widgets(
        cw.GraphWidget(
            title="joinRoom and getHistory calls and errors", width=12,
            left=_per_fn(fns, ["join_room", "get_history"], lambda f: f.metric_invocations(period=PERIOD), " calls")
            + _per_fn(fns, ["join_room", "get_history"], lambda f: f.metric_errors(period=PERIOD), " errors"),
        ),
        cw.GraphWidget(
            title="History query duration p95 (ms)", width=12,
            left=_per_fn(fns, ["join_room", "get_history"],
                         lambda f: f.metric_duration(statistic="p95", period=PERIOD), " p95"),
        ),
    )

    # ---- At a glance ----------------------------------------------------------
    header("## Totals for the selected time range")
    dash.add_widgets(cw.SingleValueWidget(
        width=FULL, height=4, set_period_to_time_range=True,
        metrics=[
            _api_metric(api, stage_name, "ConnectCount", label="connections"),
            _api_metric(api, stage_name, "ClientError", label="rejected (4xx)"),
            _api_metric(api, stage_name, "MessageCount", label="messages"),
            fns["send_message"].metric_errors(label="send errors"),
            fns["send_message"].metric_throttles(label="send throttles"),
        ],
    ))
    return dash
