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
