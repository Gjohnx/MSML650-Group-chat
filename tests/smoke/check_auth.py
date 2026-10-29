#!/usr/bin/env python3
"""Smoke check for success criterion 1: unauthorized connections are rejected.

Opens the WebSocket API with no token and with a forged token and expects the
handshake to be refused (HTTP 401 or 403). With --valid-token it also signs in
a real Cognito user and expects that connection to open, which is the other
half of the criterion; leave it off until the authorizer verifies JWTs.

    python tests/smoke/check_auth.py --owner demo
    python tests/smoke/check_auth.py --owner demo --valid-token
    python tests/smoke/check_auth.py --url ws://127.0.0.1:8765     # fake server

Exits 0 when every check passes. Owner: role 6.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

FORGED = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmb3JnZWQifQ.c2lnbmF0dXJl"


async def try_connect(url: str, timeout: float = 10) -> int | None:
    """HTTP status the handshake was refused with, or None if it opened."""
    try:
        async with connect(url, open_timeout=timeout):
            return None
    except InvalidStatus as e:
        return e.response.status_code


def stack_url_and_token(owner: str, profile: str | None, want_token: bool) -> tuple[str, str | None]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import boto3
    from get_token import DEFAULT_PASSWORD, ensure_user, get_id_token, stack_outputs

    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    out = stack_outputs(session, owner)
    token = None
    if want_token:
        idp = session.client("cognito-idp")
        ensure_user(idp, out["UserPoolId"], "smoke-test", DEFAULT_PASSWORD)
        token = get_id_token(idp, out["AppClientId"], "smoke-test", DEFAULT_PASSWORD)
    return out["WebSocketUrl"], token


async def run(url: str, token: str | None) -> list[tuple[str, bool, str]]:
    checks = []
    for name, full in (("no token", url), ("forged token", f"{url}?token={FORGED}")):
        status = await try_connect(full)
        checks.append((name, status in (401, 403), "opened" if status is None else f"HTTP {status}"))
    if token:
        status = await try_connect(f"{url}?token={token}")
        checks.append(("valid token", status is None, "opened" if status is None else f"HTTP {status}"))
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner", help="check the deployed stack GroupChat-<owner>")
    target.add_argument("--url", help="check this WebSocket URL")
    ap.add_argument("--valid-token", action="store_true", help="also expect a real Cognito token to connect")
    ap.add_argument("--token", help="with --url: a token that should be accepted")
    ap.add_argument("--profile", default="msml650", help="AWS CLI profile; pass '' to use env credentials")
    args = ap.parse_args(argv)

    if args.owner:
        url, token = stack_url_and_token(args.owner, args.profile or None, args.valid_token)
    else:
        url, token = args.url, args.token

    checks = asyncio.run(run(url, token))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
