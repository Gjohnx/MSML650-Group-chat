"""$disconnect handler. Owner: role 2.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import os

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")


def handler(event, context):
    # TODO: delete the Connections row
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
