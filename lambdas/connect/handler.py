"""$connect handler. Owner: role 2.

Runs only after the authorizer has allowed the handshake. Writes the
Connections row that every other route reads. Returning a non-2xx status here
makes API Gateway refuse the socket, so a connection never opens without a row.

See docs/CONTRACTS.md for the frame shapes and table schemas.
"""
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")

TTL_SECONDS = 2 * 3600  # API Gateway closes every socket after 2 hours anyway

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def response(status, body):
    return {"statusCode": status, "body": json.dumps(body)}


def handler(event, context):
    request_context = event.get("requestContext", {})
    connection_id = request_context.get("connectionId")
    authorizer = request_context.get("authorizer") or {}
    user_id = authorizer.get("userId")
    username = authorizer.get("username")

    if not connection_id:
        return response(400, {"error": "Missing connection ID"})

    # The authorizer already denied bad tokens; this guards against the route
    # ever being wired up without it.
    if not user_id or not username:
        return response(401, {"error": "Unauthorized"})

    connected_at = int(time.time() * 1000)
    item = {
        "connectionId": connection_id,
        "userId": user_id,
        "username": username,
        "connectedAt": connected_at,
        "expiresAt": connected_at // 1000 + TTL_SECONDS,
    }
    # roomId is left off rather than set to "": DynamoDB rejects an empty
    # string as a key of the roomId-index GSI.

    try:
        boto3.resource("dynamodb").Table(CONNECTIONS_TABLE).put_item(Item=item)
    except (BotoCoreError, ClientError):
        logger.exception("could not register connection %s", connection_id)
        return response(500, {"error": "Could not register connection"})

    return response(200, {"ok": True})
