"""Unit tests for connect, disconnect, joinRoom and sendMessage. Criteria 2, 3, 5.

DynamoDB is moto's in-memory fake; the API Gateway management client is
replaced with a recorder that captures every frame posted and can play a dead
socket (GoneException) for chosen connection ids. No AWS access is needed.
"""
import importlib.util
import json
import time
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"
ROOM = "general"


class Recorder:
    """Stands in for the apigatewaymanagementapi client."""

    def __init__(self):
        self.posts = []
        self.gone = set()

    def post_to_connection(self, ConnectionId, Data):  # noqa: N803 - boto3 argument names
        if ConnectionId in self.gone:
            raise ClientError({"Error": {"Code": "GoneException", "Message": "gone"}}, "PostToConnection")
        self.posts.append((ConnectionId, json.loads(Data)))

    def frames_to(self, connection_id, frame_type=None):
        return [f for cid, f in self.posts
                if cid == connection_id and (frame_type is None or f.get("type") == frame_type)]


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

        recorder = Recorder()
        real_client = boto3.client

        def fake_client(service, *args, **kwargs):
            if service == "apigatewaymanagementapi":
                return recorder
            return real_client(service, *args, **kwargs)

        monkeypatch.setattr(boto3, "client", fake_client)

        handlers = {}
        for name in ("connect", "disconnect", "join_room", "send_message"):
            spec = importlib.util.spec_from_file_location(f"{name}_handler", LAMBDAS / name / "handler.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            handlers[name] = module.handler

        yield {"h": handlers, "ws": recorder, "connections": connections, "messages": messages}


def connect(env, connection_id, username):
    event = {"requestContext": {"connectionId": connection_id, "routeKey": "$connect",
                                "authorizer": {"userId": f"u-{username}", "username": username}}}
    return env["h"]["connect"](event, None)


def route(env, name, connection_id, **body):
    event = {"requestContext": {"connectionId": connection_id}, "body": json.dumps(body)}
    return env["h"][name](event, None)


def join(env, connection_id, room=ROOM):
    return route(env, "join_room", connection_id, action="joinRoom", roomId=room)


def send(env, connection_id, body="hello", room=ROOM, **extra):
    return route(env, "send_message", connection_id, action="sendMessage", roomId=room, body=body, **extra)


def row(env, connection_id):
    return env["connections"].get_item(Key={"connectionId": connection_id}).get("Item")


def last_error(env, connection_id):
    errors = env["ws"].frames_to(connection_id, "error")
    assert errors, f"no error frame posted to {connection_id}"
    return errors[-1]


# ---- $connect ---------------------------------------------------------------------

def test_connect_writes_row_with_identity_and_two_hour_ttl(env):
    before = int(time.time() * 1000)
    assert connect(env, "c1", "alice")["statusCode"] == 200
    item = row(env, "c1")
    assert item["userId"] == "u-alice"
    assert item["username"] == "alice"
    assert "roomId" not in item  # an empty string would be an invalid GSI key
    assert before <= int(item["connectedAt"]) <= int(time.time() * 1000)
    assert int(item["expiresAt"]) == int(item["connectedAt"]) // 1000 + 7200


def test_connect_without_authorizer_context_is_refused(env):
    response = env["h"]["connect"]({"requestContext": {"connectionId": "c1"}}, None)
    assert response["statusCode"] == 401
    assert row(env, "c1") is None


def test_connect_without_connection_id_is_400(env):
    assert env["h"]["connect"]({"requestContext": {}}, None)["statusCode"] == 400


# ---- $disconnect ------------------------------------------------------------------

def test_disconnect_deletes_row(env):
    connect(env, "c1", "alice")
    assert env["h"]["disconnect"]({"requestContext": {"connectionId": "c1"}}, None)["statusCode"] == 200
    assert row(env, "c1") is None


def test_disconnect_of_unknown_connection_is_fine(env):
    assert env["h"]["disconnect"]({"requestContext": {"connectionId": "nope"}}, None)["statusCode"] == 200


# ---- joinRoom ---------------------------------------------------------------------

def test_join_sets_room_and_replies_with_history_first(env):
    connect(env, "c1", "alice")
    join(env, "c1")
    assert row(env, "c1")["roomId"] == ROOM
    frames = env["ws"].frames_to("c1")
    assert frames[0] == {"type": "history", "roomId": ROOM, "messages": [], "nextBefore": None}


def test_join_history_is_newest_50_oldest_first(env):
    with env["messages"].batch_writer() as batch:
        for i in range(60):
            sent_at = 1_760_000_000_000 + i * 1000
            batch.put_item(Item={"roomId": ROOM, "sortKey": f"{sent_at}#M{i:05d}", "messageId": f"M{i:05d}",
                                 "userId": "u-bob", "username": "bob", "body": f"message {i}",
                                 "sentAt": Decimal(sent_at)})
    connect(env, "c1", "alice")
    join(env, "c1")
    history = env["ws"].frames_to("c1", "history")[0]
    assert [m["body"] for m in history["messages"]] == [f"message {i}" for i in range(10, 60)]
    assert history["nextBefore"] == f"{1_760_000_000_000 + 10_000}#M00010"
    first = history["messages"][0]
    assert first["type"] == "message" and "sortKey" not in first and isinstance(first["sentAt"], int)


def test_join_announces_presence_to_others_only(env):
    connect(env, "c1", "alice")
    connect(env, "c2", "bob")
    join(env, "c1")
    join(env, "c2")
    presence = env["ws"].frames_to("c1", "presence")
    assert presence == [{"type": "presence", "roomId": ROOM, "userId": "u-bob", "username": "bob",
                         "event": "joined"}]
    assert env["ws"].frames_to("c2", "presence") == []


def test_switching_rooms_moves_the_connection(env):
    connect(env, "c1", "alice")
    join(env, "c1")
    join(env, "c1", room="random")
    assert row(env, "c1")["roomId"] == "random"


def test_join_from_unknown_connection_does_not_create_a_row(env):
    join(env, "ghost")
    assert last_error(env, "ghost")["code"] == "UNAUTHORIZED"
    assert row(env, "ghost") is None


@pytest.mark.parametrize("room", ["General", "", "a" * 65, "has space", None])
def test_join_rejects_bad_room_ids(env, room):
    connect(env, "c1", "alice")
    route(env, "join_room", "c1", action="joinRoom", roomId=room)
    assert last_error(env, "c1")["code"] == "BAD_REQUEST"
    assert "roomId" not in row(env, "c1")


def test_presence_to_dead_socket_cleans_it_up(env):
    connect(env, "c1", "alice")
    connect(env, "c2", "bob")
    join(env, "c1")
    env["ws"].gone.add("c1")
    join(env, "c2")
    assert row(env, "c1") is None
    assert row(env, "c2")["roomId"] == ROOM


# ---- sendMessage ------------------------------------------------------------------

def setup_room(env, *names):
    for i, name in enumerate(names):
        connect(env, f"c{i}", name)
        join(env, f"c{i}")


def test_message_is_persisted_and_fanned_out_to_room_including_sender(env):
    setup_room(env, "alice", "bob", "carol")
    connect(env, "elsewhere", "dave")
    join(env, "elsewhere", room="random")

    send(env, "c0", body="  hello  ", clientSentAt=1_760_630_400_123)

    frames = [env["ws"].frames_to(f"c{i}", "message") for i in range(3)]
    assert all(len(f) == 1 for f in frames)
    msg = frames[0][0]
    assert all(f[0] == msg for f in frames)
    assert msg["body"] == "hello"
    assert msg["userId"] == "u-alice" and msg["username"] == "alice"
    assert msg["clientSentAt"] == 1_760_630_400_123
    assert isinstance(msg["sentAt"], int) and len(msg["messageId"]) == 26
    assert env["ws"].frames_to("elsewhere", "message") == []

    stored = env["messages"].get_item(Key={"roomId": ROOM, "sortKey": f"{msg['sentAt']}#{msg['messageId']}"})
    assert stored["Item"]["body"] == "hello"
    assert int(stored["Item"]["clientSentAt"]) == 1_760_630_400_123


def test_message_without_client_time_omits_it(env):
    setup_room(env, "alice")
    send(env, "c0")
    msg = env["ws"].frames_to("c0", "message")[0]
    assert "clientSentAt" not in msg


def test_identity_comes_from_the_row_not_the_client(env):
    setup_room(env, "alice")
    send(env, "c0", userId="u-mallory", username="mallory")
    msg = env["ws"].frames_to("c0", "message")[0]
    assert msg["userId"] == "u-alice" and msg["username"] == "alice"


def test_sent_messages_show_up_in_the_next_join_history(env):
    setup_room(env, "alice")
    for i in range(3):
        send(env, "c0", body=f"m{i}")
    connect(env, "late", "bob")
    join(env, "late")
    history = env["ws"].frames_to("late", "history")[0]
    assert [m["body"] for m in history["messages"]] == ["m0", "m1", "m2"]


def test_gone_connections_are_deleted_and_survivors_still_get_it(env):
    setup_room(env, "alice", "bob", "carol", "dave")
    env["ws"].gone.update({"c2", "c3"})
    send(env, "c0", body="after kill")
    assert row(env, "c2") is None and row(env, "c3") is None
    assert row(env, "c1") is not None
    for cid in ("c0", "c1"):
        assert [m["body"] for m in env["ws"].frames_to(cid, "message")] == ["after kill"]
    assert env["ws"].frames_to("c0", "error") == []


def test_message_ids_are_unique_and_sort_keys_ascend(env):
    setup_room(env, "alice")
    for i in range(20):
        send(env, "c0", body=f"m{i}")
    ids = [m["messageId"] for m in env["ws"].frames_to("c0", "message")]
    assert len(set(ids)) == 20
    items = env["messages"].scan()["Items"]
    assert len(items) == 20


@pytest.mark.parametrize("body, code", [
    ({"roomId": "random", "body": "hi"}, "NOT_IN_ROOM"),
    ({"roomId": "General", "body": "hi"}, "BAD_REQUEST"),
    ({"roomId": ROOM}, "BAD_REQUEST"),
    ({"roomId": ROOM, "body": "   "}, "BAD_REQUEST"),
    ({"roomId": ROOM, "body": 5}, "BAD_REQUEST"),
    ({"roomId": ROOM, "body": "x" * 2001}, "BAD_REQUEST"),
    ({"roomId": ROOM, "body": "hi", "clientSentAt": "now"}, "BAD_REQUEST"),
    ({"roomId": ROOM, "body": "hi", "clientSentAt": True}, "BAD_REQUEST"),
])
def test_bad_sends_get_contract_errors_and_store_nothing(env, body, code):
    setup_room(env, "alice")
    route(env, "send_message", "c0", action="sendMessage", **body)
    error = last_error(env, "c0")
    assert error["code"] == code and error["message"]
    assert env["messages"].scan()["Items"] == []


def test_body_of_exactly_2000_chars_is_allowed(env):
    setup_room(env, "alice")
    send(env, "c0", body="x" * 2000)
    assert len(env["ws"].frames_to("c0", "message")) == 1


def test_send_from_unknown_connection_is_unauthorized(env):
    send(env, "ghost")
    assert last_error(env, "ghost")["code"] == "UNAUTHORIZED"


def test_send_before_join_is_not_in_room(env):
    connect(env, "c1", "alice")
    send(env, "c1")
    assert last_error(env, "c1")["code"] == "NOT_IN_ROOM"


def test_invalid_json_is_bad_request(env):
    setup_room(env, "alice")
    env["h"]["send_message"]({"requestContext": {"connectionId": "c0"}, "body": "{nope"}, None)
    assert last_error(env, "c0")["code"] == "BAD_REQUEST"
