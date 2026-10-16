"""$connect handler. Owner: role 2.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import os

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")


def handler(event, context):
    # TODO: write the Connections row from event.requestContext.connectionId and authorizer context
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
