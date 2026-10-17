"""sendMessage handler. Owner: role 3.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import os

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")


def handler(event, context):
    # TODO: validate, write to Messages, query roomId-index, fan out via post_to_connection, delete rows on GoneException
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
