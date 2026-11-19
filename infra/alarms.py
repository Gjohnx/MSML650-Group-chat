"""CloudWatch alarms for the group chat stack. Owner: role 5a.

The dashboard shows what happened; these alarms say when something is wrong
without anyone watching the graphs. Every alarm notifies one SNS topic on
both ALARM and OK, so a recovery is announced the same way a failure is.

To get the notifications by email, deploy with:

    cdk deploy --profile msml650 -c owner=<yourname> -c alarm_email=you@example.com

and click the confirmation link AWS emails you. Without alarm_email the alarms
still change state and show on the dashboard; they just notify nobody.

Thresholds are tuned for a class demo, not production: one error is worth
knowing about, and the latency alarm uses the server's share of the 200 ms
end-to-end budget.
"""
from aws_cdk import Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs

PERIOD = Duration.minutes(1)
SEND_P95_BUDGET_MS = 150  # leaves ~50 ms of the 200 ms budget for the network


def build_alarms(
    stack: Stack,
    *,
    owner: str,
    api: apigw.WebSocketApi,
    stage_name: str,
    fns: dict[str, lambda_.Function],
) -> tuple[sns.Topic, list[cw.Alarm]]:
    topic = sns.Topic(stack, "AlarmTopic", topic_name=f"group-chat-{owner}-alarms")
    email = stack.node.try_get_context("alarm_email")
    if email:
        topic.add_subscription(subs.EmailSubscription(email))

    alarms: list[cw.Alarm] = []

    def add(construct_id: str, name: str, description: str, **kwargs) -> None:
        alarm = cw.Alarm(
            stack, construct_id,
            alarm_name=f"group-chat-{owner}-{name}",
            alarm_description=description,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            **kwargs,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(topic))
        alarm.add_ok_action(cw_actions.SnsAction(topic))
        alarms.append(alarm)

    # Any Lambda crashing. An authorizer *denial* is not an error; a crash is.
    for name, fn in fns.items():
        add(
            f"{name}ErrorsAlarm", f"{name}-errors",
            f"{name} Lambda threw an error in the last minute",
            metric=fn.metric_errors(period=PERIOD, statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )

    # sendMessage throttled: Lambda refused to run it, so messages were dropped.
    # This is the alarm the failure drill trips (docs/FAILURE_DRILL.md).
    add(
        "SendThrottlesAlarm", "send-throttles",
        "sendMessage was throttled: messages are being dropped (criterion 4)",
        metric=fns["send_message"].metric_throttles(period=PERIOD, statistic="Sum"),
        threshold=1,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    )

    # Errors API Gateway itself saw: integration failures and execution errors.
    def api_metric(metric_name: str) -> cw.Metric:
        return cw.Metric(
            namespace="AWS/ApiGateway",
            metric_name=metric_name,
            dimensions_map={"ApiId": api.api_id, "Stage": stage_name},
            statistic="Sum",
            period=PERIOD,
        )

    add(
        "ApiServerErrorsAlarm", "api-server-errors",
        "WebSocket API saw integration or execution errors in the last minute",
        metric=cw.MathExpression(
            expression="FILL(e, 0) + FILL(i, 0)",
            using_metrics={"e": api_metric("ExecutionError"), "i": api_metric("IntegrationError")},
            label="API server errors",
            period=PERIOD,
        ),
        threshold=1,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    )

    # Criterion 2: server share of the latency budget, sustained (3 of 5 min)
    # so one cold start does not page anyone.
    add(
        "SendLatencyAlarm", "send-latency-p95",
        f"sendMessage p95 duration above {SEND_P95_BUDGET_MS} ms for 3 of the last 5 minutes (criterion 2)",
        metric=fns["send_message"].metric_duration(period=PERIOD, statistic="p95"),
        threshold=SEND_P95_BUDGET_MS,
        evaluation_periods=5,
        datapoints_to_alarm=3,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
    )

    return topic, alarms
