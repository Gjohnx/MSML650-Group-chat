"""Unit tests for the Lambda handlers that run without AWS.

Each handler module must import cleanly and the authorizer must deny a
connection that has no token or a malformed one (criterion 1). Owners add
tests for their own handler's logic here as it lands; use moto or stubbed
boto3 clients so CI never needs AWS credentials.
"""
import importlib.util
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"
METHOD_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/$connect"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_handler", LAMBDAS / name / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", sorted(p.name for p in LAMBDAS.iterdir() if (p / "handler.py").exists()))
def test_handler_imports_and_has_entry_point(name):
    assert callable(load(name).handler)


def effect(policy: dict) -> str:
    return policy["policyDocument"]["Statement"][0]["Effect"]


@pytest.mark.parametrize("query", [None, {}, {"token": ""}, {"token": "not-a-jwt"},
                                   {"token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"}])
def test_authorizer_denies_missing_or_bad_token(query):
    authorizer = load("authorizer")
    policy = authorizer.handler({"queryStringParameters": query, "methodArn": METHOD_ARN}, None)
    assert effect(policy) == "Deny"
    assert "context" not in policy or not policy["context"].get("userId")

def test_authorizer_allows_valid_token(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    authorizer = load("authorizer")

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    public_key = private_key.public_key()

    monkeypatch.setattr(authorizer, "USER_POOL_ID", "us-east-1_testpool")
    monkeypatch.setattr(authorizer, "APP_CLIENT_ID", "test-client")

    issuer = (
        "https://cognito-idp.us-east-1.amazonaws.com/"
        "us-east-1_testpool"
    )
    monkeypatch.setattr(authorizer, "ISSUER", issuer)

    class FakeSigningKey:
        key = public_key

    monkeypatch/setattr(
        authorizer.jwks_client,
        "get_signing_key_from_jwt",
        lambda token: FakeSigningKey(),
    )

    import time
    now = int(time.time())

    token = jwt.encode(
        {
            "sub": "test-user-123",
            "cognito:username": "testuser",
            "token_use": "id",
            "aud": "test-client",
            "iss": issuer,
            "iat": now,
            "exp": now + 3600,
        },
        private_key,
        algorithm="RS256",
    )

    event = {
        "queryStringParameters": {"token": token},
        "methodArn": METHOD_ARN,
    }

    policy = authorizer.handler(event, None)

    assert effect(policy) == "Allow"
    assert policy["context"]["userId"] == "test-user-123"
    assert policy["context"]["username"] == "testuser"
