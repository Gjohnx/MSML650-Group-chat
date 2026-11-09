"""Unit tests for the getHistory handler (lambdas/get_history). Criterion 5.

DynamoDB is moto's in-memory fake; the API Gateway management client is
replaced with a recorder, so every frame the handler would post back to the
caller is captured. No AWS access is needed.

Tests marked xfail record a known gap between the handler and
docs/CONTRACTS.md. They report XPASS once the gap is fixed; remove the marker
then.

Not covered here: real DynamoDB can return LastEvaluatedKey when exactly
`limit` items are left, which would send one extra empty page. moto does not
reproduce that, so tests/smoke/check_history.py flags it on a live stack.
"""
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

HANDLER = Path(__file__).resolve().parents[1] / "lambdas" / "get_history" / "handler.py"
ROOM = "general"
CONN = "conn-alice"


class Recorder:
    """Stands in for the apigatewaymanagementapi client."""

    def __init__(self):
        self.posts = []

    def post_to_connection(self, ConnectionId, Data):  # noqa: N803 - boto3 argument names
        self.posts.append((ConnectionId, json.loads(Data)))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CONNECTIONS_TABLE", "Connections")
    monkeypatch.setenv("MESSAGES_TABLE", "Messages")
    monkeypatch.setenv("WS_ENDPOINT", "https://example.execute-api.us-east-1.amazonaws.com/prod")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        connections = ddb.create_table(
            TableName="Connections", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"},
                                  {"AttributeName": "roomId", "AttributeType": "S"}],
            GlobalSecondaryIndexes=[{"IndexName": "roomId-index",
                                     "KeySchema": [{"AttributeName": "roomId", "KeyType": "HASH"}],
                                     "Projection": {"ProjectionType": "ALL"}}],
        )
        messages = ddb.create_table(
            TableName="Messages", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "roomId", "KeyType": "HASH"},
                       {"AttributeName": "sortKey", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "roomId", "AttributeType": "S"},
                                  {"AttributeName": "sortKey", "AttributeType": "S"}],
        )
        connections.put_item(Item={"connectionId": CONN, "userId": "u-alice", "username": "alice",
                                   "roomId": ROOM, "connectedAt": 1_760_000_000_000,
                                   "expiresAt": 1_760_007_200})

        spec = importlib.util.spec_from_file_location("get_history_handler", HANDLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        recorder = Recorder()
        real_client = boto3.client

        def fake_client(service, *args, **kwargs):
            if service == "apigatewaymanagementapi":
                return recorder
            return real_client(service, *args, **kwargs)

        monkeypatch.setattr(module.boto3, "client", fake_client)
        yield {"handler": module.handler, "posts": recorder.posts, "messages": messages,
               "connections": connections}


def seed(table, count, room=ROOM, start=1_760_000_000_000):
    with table.batch_writer() as batch:
        for i in range(count):
            sent_at = start + i * 1000
            batch.put_item(Item={
                "roomId": room, "sortKey": f"{sent_at}#M{i:05d}", "messageId": f"M{i:05d}",
                "userId": "u-bob", "username": "bob", "body": f"message {i}",
                "sentAt": Decimal(sent_at), "clientSentAt": Decimal(sent_at - 25),
            })


def call(env, connection_id=CONN, **body):
    body.setdefault("action", "getHistory")
    event = {"requestContext": {"connectionId": connection_id, "routeKey": "getHistory"},
             "body": json.dumps(body)}
    response = env["handler"](event, None)
    assert env["posts"], "handler posted nothing back to the caller"
    posted_to, frame = env["posts"][-1]
    assert posted_to == connection_id
    return response, frame


def page_through(env, limit=50):
    pages, before = [], None
    for _ in range(20):
        args = {"roomId": ROOM, "limit": limit}
        if before:
            args["before"] = before
        _, frame = call(env, **args)
        assert frame["type"] == "history", frame
        pages.append(frame["messages"])
        before = frame["nextBefore"]
        if before is None:
            return pages
    pytest.fail("never reached nextBefore null")


# ---- Paging ---------------------------------------------------------------------

def test_120_messages_page_as_50_50_20(env):
    seed(env["messages"], 120)
    pages = page_through(env)
    assert [len(p) for p in pages] == [50, 50, 20]
    bodies = [m["body"] for p in reversed(pages) for m in p]
    assert bodies == [f"message {i}" for i in range(120)]


def test_first_page_is_the_newest_messages_oldest_first(env):
    seed(env["messages"], 120)
    _, frame = call(env, roomId=ROOM)
    assert frame["roomId"] == ROOM
    assert [m["body"] for m in frame["messages"]] == [f"message {i}" for i in range(70, 120)]
    assert frame["nextBefore"] == f"{1_760_000_000_000 + 70_000}#M00070"


def test_short_room_fits_one_page_with_null_cursor(env):
    seed(env["messages"], 7)
    _, frame = call(env, roomId=ROOM)
    assert len(frame["messages"]) == 7
    assert frame["nextBefore"] is None


def test_empty_room_returns_empty_history(env):
    _, frame = call(env, roomId=ROOM)
    assert frame == {"type": "history", "roomId": ROOM, "messages": [], "nextBefore": None}


def test_other_rooms_do_not_leak(env):
    seed(env["messages"], 5)
    seed(env["messages"], 5, room="random")
    env["connections"].update_item(Key={"connectionId": CONN}, UpdateExpression="SET roomId = :r",
                                   ExpressionAttributeValues={":r": "random"})
    _, frame = call(env, roomId="random")
    assert {m["roomId"] for m in frame["messages"]} == {"random"}


# ---- Frame shape ------------------------------------------------------------------

def test_messages_match_the_contract_frame(env):
    seed(env["messages"], 3)
    _, frame = call(env, roomId=ROOM)
    message = frame["messages"][0]
    assert message == {"type": "message", "roomId": ROOM, "messageId": "M00000", "userId": "u-bob",
                       "username": "bob", "body": "message 0", "sentAt": 1_760_000_000_000,
                       "clientSentAt": 1_760_000_000_000 - 25}
    assert isinstance(message["sentAt"], int) and isinstance(message["clientSentAt"], int)


def test_message_without_client_time_omits_it(env):
    env["messages"].put_item(Item={"roomId": ROOM, "sortKey": "1#A", "messageId": "A", "userId": "u",
                                   "username": "u", "body": "hi", "sentAt": Decimal(1)})
    _, frame = call(env, roomId=ROOM)
    assert "clientSentAt" not in frame["messages"][0]
    assert "sortKey" not in frame["messages"][0]


# ---- Errors (docs/CONTRACTS.md section 3) -------------------------------------------

@pytest.mark.parametrize("body, code", [
    ({"roomId": "General"}, "BAD_REQUEST"),
    ({"roomId": ""}, "BAD_REQUEST"),
    ({"roomId": "a" * 65}, "BAD_REQUEST"),
    ({}, "BAD_REQUEST"),
    ({"roomId": ROOM, "limit": 0}, "BAD_REQUEST"),
    ({"roomId": ROOM, "limit": "10"}, "BAD_REQUEST"),
    ({"roomId": ROOM, "limit": True}, "BAD_REQUEST"),
    ({"roomId": ROOM, "limit": 101}, "BAD_REQUEST"),
    ({"roomId": ROOM, "before": ""}, "BAD_REQUEST"),
    ({"roomId": ROOM, "before": 123}, "BAD_REQUEST"),
    ({"roomId": "other-room"}, "NOT_IN_ROOM"),
])
def test_bad_requests_get_contract_errors(env, body, code):
    _, frame = call(env, **body)
    assert frame["type"] == "error"
    assert frame["code"] == code
    assert frame["message"]


def test_invalid_json_body(env):
    env["handler"]({"requestContext": {"connectionId": CONN}, "body": "{not json"}, None)
    assert env["posts"][-1][1]["code"] == "BAD_REQUEST"


def test_unknown_connection_is_rejected(env):
    _, frame = call(env, connection_id="conn-nobody", roomId=ROOM)
    assert frame["code"] == "UNAUTHORIZED"


def test_missing_connection_id_returns_400_without_posting(env):
    response = env["handler"]({"requestContext": {}, "body": "{}"}, None)
    assert response["statusCode"] == 400
    assert env["posts"] == []


@pytest.mark.xfail(reason="Contract: limit max 100. The handler rejects anything over 50, "
                          "while its own error message says 'between 1 and 100'.")
def test_limit_100_is_allowed_by_contract(env):
    seed(env["messages"], 120)
    _, frame = call(env, roomId=ROOM, limit=100)
    assert frame["type"] == "history"
    assert len(frame["messages"]) == 100
