"""sendMessage handler. Owner: role 3.

Validates the frame, persists it to Messages, then fans a message frame out to
every connection in the room, the sender included (the echo is how the client
measures latency, criterion 2). Posts run in parallel so one slow socket does
not hold up the rest. A socket that answers GoneException has its
Connections row deleted on the spot (criterion 3).

userId and username come from the caller's Connections row, written by
$connect from the authorizer context, never from the client.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")

ROOM_ID = re.compile(r"[a-z0-9-]{1,64}")
MAX_BODY = 2000
MAX_WORKERS = 32
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Created once per container and reused while it stays warm; a cold client
# costs tens of milliseconds we cannot spare under the 200 ms budget.
_clients = {}


def ws_client():
    if "ws" not in _clients:
        _clients["ws"] = boto3.client("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT)
    return _clients["ws"]


def tables():
    if "tables" not in _clients:
        dynamodb = boto3.resource("dynamodb")
        _clients["tables"] = (dynamodb.Table(CONNECTIONS_TABLE), dynamodb.Table(MESSAGES_TABLE))
    return _clients["tables"]


def new_ulid(ms):
    """A ULID: 48 bits of ms timestamp then 80 random bits, Crockford base32."""
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def is_gone(err):
    return isinstance(err, ClientError) and err.response.get("Error", {}).get("Code") == "GoneException"


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
    """Post one frame to many sockets in parallel; delete rows whose socket is gone.

    Returns (delivered, gone) counts.
    """
    data = json.dumps(frame).encode("utf-8")

    def post(connection_id):
        try:
            ws.post_to_connection(ConnectionId=connection_id, Data=data)
            return "ok"
        except ClientError as err:
            if is_gone(err):
                return "gone"
            logger.warning("post to %s failed: %s", connection_id, err)
            return "failed"

    if not connection_ids:
        return 0, 0
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(connection_ids))) as pool:
        outcomes = list(pool.map(post, connection_ids))

    gone = [cid for cid, outcome in zip(connection_ids, outcomes, strict=True) if outcome == "gone"]
    for connection_id in gone:
        try:
            connections.delete_item(Key={"connectionId": connection_id})
        except (BotoCoreError, ClientError):
            logger.exception("could not delete stale connection %s", connection_id)
    return outcomes.count("ok"), len(gone)


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
    body = request.get("body")
    client_sent_at = request.get("clientSentAt")

    if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
        return error("BAD_REQUEST", "roomId must be 1 to 64 characters of a-z, 0-9 and -")

    if not isinstance(body, str) or not body.strip():
        return error("BAD_REQUEST", "body is required")
    body = body.strip()
    if len(body) > MAX_BODY:
        return error("BAD_REQUEST", f"body must be at most {MAX_BODY} characters")

    if client_sent_at is not None:
        # bool is an int subclass; reject it explicitly.
        if isinstance(client_sent_at, bool) or not isinstance(client_sent_at, (int, float)):
            return error("BAD_REQUEST", "clientSentAt must be epoch milliseconds")
        client_sent_at = int(client_sent_at)

    connections, messages = tables()

    try:
        connection = connections.get_item(Key={"connectionId": connection_id}).get("Item")
    except (BotoCoreError, ClientError):
        logger.exception("could not read connection %s", connection_id)
        return error("INTERNAL", "Could not send the message")

    if not connection:
        return error("UNAUTHORIZED", "Connection not found, reconnect")

    if connection.get("roomId") != room_id:
        return error("NOT_IN_ROOM", "Join the room first")

    sent_at = int(time.time() * 1000)
    message_id = new_ulid(sent_at)
    frame = {
        "type": "message",
        "roomId": room_id,
        "messageId": message_id,
        "userId": connection["userId"],
        "username": connection["username"],
        "body": body,
        "sentAt": sent_at,
    }
    if client_sent_at is not None:
        frame["clientSentAt"] = client_sent_at

    item = {key: value for key, value in frame.items() if key != "type"}
    item["sortKey"] = f"{sent_at}#{message_id}"

    # Persist before fan-out so anyone who joins mid-broadcast still finds the
    # message in their history frame.
    try:
        messages.put_item(Item=item)
        recipients = room_connection_ids(connections, room_id)
    except (BotoCoreError, ClientError):
        logger.exception("could not store or route message in %s", room_id)
        return error("INTERNAL", "Could not send the message")

    # The GSI is eventually consistent, so a sender who just joined may not be
    # listed yet. They always get their own echo.
    if connection_id not in recipients:
        recipients.append(connection_id)

    delivered, gone = broadcast(ws, connections, recipients, frame)
    logger.info(json.dumps({
        "event": "broadcast", "roomId": room_id, "messageId": message_id,
        "recipients": len(recipients), "delivered": delivered, "gone": gone,
        "handlerMs": int(time.time() * 1000) - sent_at,
    }))

    return {"statusCode": 200}
