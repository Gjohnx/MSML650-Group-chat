"""Shared helpers for the criterion checks in tests/smoke. Owner: role 6.

Three ways to point a check at a backend:

- `--owner <name>`: the deployed stack GroupChat-<name>. Signs in real Cognito
  users through tools/get_token.py and can read the stack's DynamoDB tables.
- `--url ws://... --mock-tokens`: Sai's mock (client/mock/server.mjs), which
  decodes tokens without verifying them, so unsigned test tokens work.
- `--url ws://...`: the Python fake server (tests/load/fake_server.py), which
  needs no token.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect

DEFAULT_PASSWORD = "Chat-test-1234"
TOOLS = Path(__file__).resolve().parents[2] / "tools"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def unsigned_token(username: str, hours: float = 1) -> str:
    """A JWT-shaped token with no valid signature, for the mock only."""
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    claims = {"sub": f"test-{username}", "cognito:username": username, "exp": int(time.time() + hours * 3600)}
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.c2ln"


def add_target_args(ap) -> None:
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner", help="check the deployed stack GroupChat-<owner>")
    target.add_argument("--url", help="check this WebSocket URL (mock or fake server)")
    ap.add_argument("--mock-tokens", action="store_true",
                    help="with --url: add unsigned test tokens, for Sai's mock")
    ap.add_argument("--create", action="store_true", help="with --owner: create the Cognito test users first")
    ap.add_argument("--profile", default="msml650", help="AWS CLI profile; pass '' to use env credentials")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")


class Stack:
    """A deployed GroupChat-<owner> stack: connect URLs and table names."""

    def __init__(self, owner: str, profile: str | None) -> None:
        sys.path.insert(0, str(TOOLS))
        import boto3
        from get_token import stack_outputs

        self.owner = owner
        self.session = boto3.Session(profile_name=profile, region_name="us-east-1")
        self.outputs = stack_outputs(self.session, owner)
        self._tables: dict[str, str] | None = None

    def connect_urls(self, usernames: list[str], *, create: bool) -> list[str]:
        from get_token import ensure_user, get_id_token

        idp = self.session.client("cognito-idp")
        urls = []
        for name in usernames:
            if create:
                ensure_user(idp, self.outputs["UserPoolId"], name, DEFAULT_PASSWORD)
            token = get_id_token(idp, self.outputs["AppClientId"], name, DEFAULT_PASSWORD)
            urls.append(f"{self.outputs['WebSocketUrl']}?token={token}")
        return urls

    def table(self, logical_prefix: str):
        """The DynamoDB Table resource whose logical id starts with the prefix."""
        if self._tables is None:
            cfn = self.session.client("cloudformation")
            resources = cfn.describe_stack_resources(StackName=f"GroupChat-{self.owner}")["StackResources"]
            self._tables = {r["LogicalResourceId"]: r["PhysicalResourceId"]
                            for r in resources if r["ResourceType"] == "AWS::DynamoDB::Table"}
        matches = [phys for logical, phys in self._tables.items() if logical.startswith(logical_prefix)]
        if len(matches) != 1:
            raise SystemExit(f"expected one {logical_prefix} table in GroupChat-{self.owner}, found {matches}")
        return self.session.resource("dynamodb").Table(matches[0])


def target_urls(args, usernames: list[str]) -> tuple[list[str], Stack | None]:
    if args.owner:
        stack = Stack(args.owner, args.profile or None)
        return stack.connect_urls(usernames, create=args.create), stack
    if args.mock_tokens:
        sep = "&" if "?" in args.url else "?"
        return [f"{args.url}{sep}token={unsigned_token(u)}" for u in usernames], None
    return [args.url] * len(usernames), None


@dataclass
class Client:
    """One WebSocket client that records every frame it receives, in order."""

    name: str
    url: str
    frames: list[tuple[int, dict]] = field(default_factory=list)  # (arrival ms, frame)
    ws: ClientConnection | None = None
    _reader: asyncio.Task | None = None
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def open(self, timeout: float = 15) -> None:
        self.ws = await connect(self.url, open_timeout=timeout, max_size=2**22)
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    frame = {"type": "_unparsable", "raw": str(raw)[:200]}
                self.frames.append((now_ms(), frame))
                self._arrived.set()
        except Exception:  # noqa: BLE001 - a killed client's reader just stops
            pass

    async def send(self, frame: dict) -> None:
        await self.ws.send(json.dumps(frame))

    async def wait_for(self, predicate, timeout: float = 10, start: int = 0) -> dict | None:
        """First frame at index >= start matching predicate, or None on timeout."""
        deadline = time.monotonic() + timeout
        index = start
        while True:
            while index < len(self.frames):
                frame = self.frames[index][1]
                index += 1
                if predicate(frame):
                    return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), remaining)
            except TimeoutError:
                return None

    def of_type(self, kind: str) -> list[dict]:
        return [f for _, f in self.frames if f.get("type") == kind]

    def errors(self) -> list[str]:
        return [f"{f.get('code')}: {f.get('message')}" for f in self.of_type("error")]

    def kill(self) -> None:
        """Drop the TCP connection with no WebSocket close frame, like a crashed browser."""
        self.ws.transport.abort()

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()


def print_checks(title: str, checks: list[tuple[str, bool, str]], notes: list[str] = ()) -> bool:
    print(title)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    for note in notes:
        print(f"note  {note}")
    passed = all(ok for _, ok, _ in checks)
    print("PASS" if passed else "FAIL")
    return passed
