"""$connect REQUEST authorizer. Owner: role 1.

Reads the Cognito ID token from the `token` query parameter, verifies the
signature against the user pool JWKS, and returns userId + username in the
authorizer context (docs/CONTRACTS.md section 5). On any failure, deny.
"""
import os

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
APP_CLIENT_ID = os.environ.get("APP_CLIENT_ID", "")


def _policy(effect, method_arn, context=None):
    doc = {
        "principalId": (context or {}).get("userId", "anonymous"),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": method_arn}],
        },
    }
    if context:
        doc["context"] = context
    return doc


def handler(event, context):
    token = (event.get("queryStringParameters") or {}).get("token")
    if not token:
        return _policy("Deny", event["methodArn"])
    # TODO: verify JWT (e.g. python-jose or PyJWT + cached JWKS), check aud/iss/exp,
    # then return _policy("Allow", event["methodArn"], {"userId": sub, "username": name})
    return _policy("Deny", event["methodArn"])
