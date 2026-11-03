"""getHistory handler. Owner: role 4.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""

import json
import os
import re

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")


def get_messages(room_id, before=None, limit=50):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(MESSAGES_TABLE)

    condition = Key("roomId").eq(room_id)

    if before:
        condition = condition & Key("sortKey").lt(before)

    result = table.query(
        KeyConditionExpression=condition, ScanIndexForward=False, Limit=limit
    )
    items = result.get("Items", [])

    has_more = "LastEvaluatedKey" in result
    next_before = items[-1]["sortKey"] if has_more and items else None

    messages = list(reversed(items))

    return {
        "type": "history",
        "roomId": room_id,
        "messages": messages,
        "nextBefore": next_before,
    }


def handler(event, context):
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "Missing connection ID"}

    ws = boto3.client("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT)

    def reply(frame):
        ws.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(frame).encode("utf-8"),
        )
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
    before = request.get("before")
    limit = request.get("limit", 50)

    if not isinstance(room_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", room_id):
        return error("BAD_REQUEST", "Invalid roomId")

    if before is not None and (not isinstance(before, str) or not before):
        return error("BAD_REQUEST", "Invalid before")

    if type(limit) is not int or not 1 <= limit <= 100:
        return error("BAD_REQUEST", "Limit must be between 1 and 100")

    try:
        connections = boto3.resource("dynamodb").Table(CONNECTIONS_TABLE)
        connection = connections.get_item(Key={"connectionId": connection_id}).get(
            "Item"
        )

        if not connection:
            return error("UNAUTHORIZED", "Connection not found")

        if connection.get("roomId") != room_id:
            return error("NOT_IN_ROOM", "Join the room first")

        history = get_messages(room_id, before, limit)

    except (BotoCoreError, ClientError):
        return error("INTERNAL", "Could not retrieve chat history")

    if not connection:
        return error("UNAUTHORIZED", "Connection not found")

    if connection.get("roomId") != room_id:
        return error("NOT_IN_ROOM", "Join the room first")

    history = get_messages(room_id, before, limit)

    fields = (
        "roomId",
        "messageId",
        "userId",
        "username",
        "body",
        "sentAt",
        "clientSentAt",
    )

    history["messages"] = [
        {
            "type": "message",
            **{
                field: int(item[field])
                if field in ("sentAt", "clientSentAt")
                else item[field]
                for field in fields
                if field in item
            },
        }
        for item in history["messages"]
    ]

    return reply(history)
