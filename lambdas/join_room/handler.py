"""joinRoom handler. Owner: role 2 and 4.

Sets roomId on the caller's Connections row, replies with a history frame of
the newest messages (criterion 5: it is sent before anything else from this
handler), then tells everyone else in the room with a presence frame.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")

ROOM_ID = re.compile(r"[a-z0-9-]{1,64}")
HISTORY_LIMIT = 50
MESSAGE_FIELDS = ("roomId", "messageId", "userId", "username", "body", "sentAt", "clientSentAt")
NUMBER_FIELDS = ("sentAt", "clientSentAt")
MAX_WORKERS = 32

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Created once per container and reused while it stays warm.
_clients = {}


def ws_client():
    if "ws" not in _clients:
        # One pooled connection per presence thread; botocore defaults to 10.
        _clients["ws"] = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=WS_ENDPOINT,
            config=Config(max_pool_connections=MAX_WORKERS),
        )
    return _clients["ws"]


def tables():
    if "tables" not in _clients:
        dynamodb = boto3.resource("dynamodb")
        _clients["tables"] = (dynamodb.Table(CONNECTIONS_TABLE), dynamodb.Table(MESSAGES_TABLE))
    return _clients["tables"]


def is_gone(err):
    return isinstance(err, ClientError) and err.response.get("Error", {}).get("Code") == "GoneException"


def as_message_frame(item):
    """A Messages row as a contract message frame: no sortKey, numbers as ints."""
    return {
        "type": "message",
        **{
            field: int(item[field]) if field in NUMBER_FIELDS else item[field]
            for field in MESSAGE_FIELDS
            if field in item
        },
    }


def newest_messages(messages, room_id):
    result = messages.query(
        KeyConditionExpression=Key("roomId").eq(room_id),
        ScanIndexForward=False,
        Limit=HISTORY_LIMIT,
    )
    items = result.get("Items", [])
    has_more = "LastEvaluatedKey" in result
    return {
        "type": "history",
        "roomId": room_id,
        "messages": [as_message_frame(item) for item in reversed(items)],
        "nextBefore": items[-1]["sortKey"] if has_more and items else None,
    }


def room_connection_ids(connections, room_id):
    ids = []
    kwargs = {
        "IndexName": "roomId-index",
        "KeyConditionExpression": Key("roomId").eq(room_id),
        "ProjectionExpression": "connectionId",
    }
    while True:
        page = connections.query(**kwargs)
        ids.extend(item["connectionId"] for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return ids
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def broadcast(ws, connections, connection_ids, frame):
    """Post one frame to many sockets in parallel; delete rows whose socket is gone."""
    data = json.dumps(frame).encode("utf-8")

    def post(connection_id):
        try:
            ws.post_to_connection(ConnectionId=connection_id, Data=data)
            return None
        except ClientError as err:
            if is_gone(err):
                return connection_id
            logger.warning("post to %s failed: %s", connection_id, err)
            return None

    if not connection_ids:
        return
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(connection_ids))) as pool:
        gone = [cid for cid in pool.map(post, connection_ids) if cid]
    for connection_id in gone:
        try:
            connections.delete_item(Key={"connectionId": connection_id})
        except (BotoCoreError, ClientError):
            logger.exception("could not delete stale connection %s", connection_id)


def handler(event, context):
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "Missing connection ID"}

    ws = ws_client()

    def reply(frame):
        try:
            ws.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(frame).encode("utf-8"),
            )
        except ClientError as err:
            if not is_gone(err):
                raise
        return {"statusCode": 200}

    def error(code, message):
        return reply({"type": "error", "code": code, "message": message})

    try:
        request = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return error("BAD_REQUEST", "Invalid JSON")

    if not isinstance(request, dict):
        return error("BAD_REQUEST", "Expected a JSON object")

    room_id = request.get("roomId")
    if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
        return error("BAD_REQUEST", "roomId must be 1 to 64 characters of a-z, 0-9 and -")

    connections, messages = tables()

    try:
        # Conditional so a socket whose row was already cleaned up cannot
        # recreate a half-empty row without userId or expiresAt.
        connection = connections.update_item(
            Key={"connectionId": connection_id},
            UpdateExpression="SET roomId = :r",
            ConditionExpression="attribute_exists(connectionId)",
            ExpressionAttributeValues={":r": room_id},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return error("UNAUTHORIZED", "Connection not found, reconnect")
        logger.exception("could not join %s to %s", connection_id, room_id)
        return error("INTERNAL", "Could not join the room")
    except BotoCoreError:
        logger.exception("could not join %s to %s", connection_id, room_id)
        return error("INTERNAL", "Could not join the room")

    try:
        history = newest_messages(messages, room_id)
    except (BotoCoreError, ClientError):
        logger.exception("could not load history for %s", room_id)
        return error("INTERNAL", "Could not load chat history")
    reply(history)

    # Presence is a courtesy: a failure here must not turn a good join into an error.
    try:
        others = [cid for cid in room_connection_ids(connections, room_id) if cid != connection_id]
        broadcast(ws, connections, others, {
            "type": "presence",
            "roomId": room_id,
            "userId": connection.get("userId"),
            "username": connection.get("username"),
            "event": "joined",
        })
    except (BotoCoreError, ClientError):
        logger.exception("could not announce %s in %s", connection_id, room_id)

    return {"statusCode": 200}
