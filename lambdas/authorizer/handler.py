"""$connect REQUEST authorizer. Owner: role 1.

Reads the Cognito ID token from the `token` query parameter, verifies the
signature against the user pool JWKS, and returns userId + username in the
authorizer context (docs/CONTRACTS.md section 5). On any failure, deny.
"""
import os
import jwt
from jwt import PyJWKClient

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
APP_CLIENT_ID = os.environ.get("APP_CLIENT_ID", "")

REGION = USER_POOL_ID.split("_")[0]

ISSUER = (
    f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
)

jwks_client = PyJWKClient(
    f"{ISSUER}/.well-known/jwks.json",
    cache_jwk_set=True,
    timeout=5,
)

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
    #return _policy("Deny", event["methodArn"])

    if not USER_POOL_ID or not APP_CLIENT_ID:
        return _policy("Deny", event["methodArn"])

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APP_CLIENT_ID,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )

        if claims.get("token_use") != "id":
            return _policy("Deny", event["methodArn"])

        user_id = claims["sub"]
        username = claims.get("cognito:username")

        if not user_id or not username:
            return _policy("Deny", event["methodArn"])

        return _policy(
            "Allow",
            event["methodArn"],
            {"userId": user_id, "username": username},
        )

    except Exception:
        return _policy("Deny", event["methodArn"])
