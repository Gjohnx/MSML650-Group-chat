"""$disconnect handler. Owner: role 2.

Deletes the Connections row. API Gateway calls this on a clean close and,
best effort, when it notices a dead socket. Anything it misses is removed by
sendMessage on GoneException, and the expiresAt TTL is the final backstop.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def response(status, body):
    return {"statusCode": status, "body": json.dumps(body)}


def handler(event, context):
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        return response(400, {"error": "Missing connection ID"})

    try:
        # Deleting a row that is already gone is not an error, so a second
        # $disconnect or a race with sendMessage's cleanup is harmless.
        boto3.resource("dynamodb").Table(CONNECTIONS_TABLE).delete_item(
            Key={"connectionId": connection_id}
        )
    except (BotoCoreError, ClientError):
        # The socket is closed whatever we return; TTL removes the row later.
        logger.exception("could not delete connection %s", connection_id)
        return response(500, {"error": "Could not delete connection"})

    return response(200, {"ok": True})
