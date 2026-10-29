"""getHistory handler. Owner: role 4.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import os
import boto3
from boto3.dynamodb.conditions import Key

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")


def get_messages(room_id, before= None, limit = 50):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(MESSAGES_TABLE)
    
    condition = Key("roomId").eq(room_id)
    
    
    if before:
        condition = condition & Key("sortKey").lt(before)
    
    result = table.query(
        KeyConditionExpression=condition,
        ScanIndexForward=False,
        Limit=limit
    )

    messages = list(reversed(result.get("Items",[])))

    return messages

def handler(event, context):
    # TODO: query Messages newest first with optional before/limit, reply with a history frame
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
