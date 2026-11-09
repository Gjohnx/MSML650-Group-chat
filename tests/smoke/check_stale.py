#!/usr/bin/env python3
"""Check success criterion 3: stale connections are reliably cleaned up.

Opens N clients in a fresh room, then kills K of them by dropping the TCP
connection with no WebSocket close frame, the way a crashed browser or a lost
network does. A surviving client then broadcasts. It checks that:

1. every surviving client still receives the broadcast, and the sender gets no
   error,
2. the killed connections' rows disappear from the Connections table within
   the timeout (removed by $disconnect, or by sendMessage on GoneException;
   either counts, see docs/CONTRACTS.md section 4),
3. a second broadcast after cleanup still reaches every survivor, and
4. on a deployed stack, TTL is enabled on the table and every remaining row
   has expiresAt about 2 hours after connectedAt, the backstop for anything
   missed.

Row checks need DynamoDB, so they run only with --owner. Against the mock or
fake server only delivery to survivors is checked.

    python tests/smoke/check_stale.py --owner john --create
    python tests/smoke/check_stale.py --url ws://localhost:8080 --mock-tokens
    python tests/smoke/check_stale.py --url ws://127.0.0.1:8765

Exits 0 when every check passes. Owner: role 6.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import time
import uuid
from decimal import Decimal

from common import Client, add_target_args, now_ms, print_checks, target_urls

TTL_SECONDS = 2 * 3600
TTL_TOLERANCE = 300


def dynamo_counter(stack, room: str):
    """Count the room's rows in Connections through the roomId-index GSI."""
    from boto3.dynamodb.conditions import Key

    table = stack.table("Connections")

    def count() -> int:
        kwargs = {"IndexName": "roomId-index", "KeyConditionExpression": Key("roomId").eq(room), "Select": "COUNT"}
        total, page = 0, table.query(**kwargs)
        total += page["Count"]
        while "LastEvaluatedKey" in page:
            page = table.query(ExclusiveStartKey=page["LastEvaluatedKey"], **kwargs)
            total += page["Count"]
        return total
    return count


async def call(counter) -> int:
    value = counter()
    return await value if inspect.isawaitable(value) else value


async def wait_for_count(counter, expected: int, timeout: float) -> tuple[bool, int, float]:
    started = time.monotonic()
    while True:
        rows = await call(counter)
        elapsed = time.monotonic() - started
        if rows == expected or elapsed >= timeout:
            return rows == expected, rows, elapsed
        await asyncio.sleep(1)


async def broadcast(sender: Client, survivors: list[Client], room: str, body: str) -> tuple[bool, str]:
    marks = {c.name: len(c.frames) for c in survivors}
    errors_before = len(sender.errors())
    await sender.send({"action": "sendMessage", "roomId": room, "body": body, "clientSentAt": now_ms()})
    got = await asyncio.gather(*(c.wait_for(lambda f: f.get("type") == "message" and f.get("body") == body,
                                            timeout=10, start=marks[c.name]) for c in survivors))
    received = sum(g is not None for g in got)
    new_errors = sender.errors()[errors_before:]
    ok = received == len(survivors) and not new_errors
    detail = f"{received}/{len(survivors)} survivors received it"
    if new_errors:
        detail += f"; sender got {'; '.join(new_errors[:2])}"
    return ok, detail


def ttl_checks(stack, room: str) -> list[tuple[str, bool, str]]:
    from boto3.dynamodb.conditions import Key

    table = stack.table("Connections")
    desc = table.meta.client.describe_time_to_live(TableName=table.name)["TimeToLiveDescription"]
    enabled = desc.get("TimeToLiveStatus") == "ENABLED" and desc.get("AttributeName") == "expiresAt"
    rows = table.query(IndexName="roomId-index", KeyConditionExpression=Key("roomId").eq(room))["Items"]
    bad = []
    for r in rows:
        exp, conn = r.get("expiresAt"), r.get("connectedAt")
        if not isinstance(exp, (int, Decimal)) or not isinstance(conn, (int, Decimal)):
            bad.append(f"{r.get('connectionId')}: missing expiresAt or connectedAt")
        elif abs(int(exp) - (int(conn) // 1000 + TTL_SECONDS)) > TTL_TOLERANCE:
            bad.append(f"{r.get('connectionId')}: expiresAt {int(exp)} is not connectedAt + 2 h")
    return [
        ("TTL enabled on expiresAt", enabled, f"status {desc.get('TimeToLiveStatus')}, attribute {desc.get('AttributeName')}"),
        ("remaining rows expire about 2 h after connecting", bool(rows) and not bad,
         f"{len(rows)} rows checked" if rows and not bad else "; ".join(bad[:3]) or "no rows to check"),
    ]


async def run_stale_check(urls: list[str], *, room: str, kill: int, counter=None, stack=None,
                          cleanup_timeout: float = 30) -> dict:
    """urls: one per client. The first client sends; the last `kill` clients are killed."""
    clients = [Client(f"client-{i}", u) for i, u in enumerate(urls)]
    checks: list[tuple[str, bool, str]] = []
    notes: list[str] = []
    result = {"room": room, "clients": len(clients), "killed": kill, "checks": checks, "notes": notes}

    await asyncio.gather(*(c.open() for c in clients))
    for c in clients:
        await c.send({"action": "joinRoom", "roomId": room})
    joined = await asyncio.gather(*(c.wait_for(lambda f: f.get("type") in ("history", "error"), timeout=10)
                                    for c in clients))
    if counter:
        ok, rows, _ = await wait_for_count(counter, len(clients), timeout=10)
        checks.append(("all clients registered in the room", ok, f"{rows}/{len(clients)} rows for the room"))
    else:
        replied = sum(j is not None and j.get("type") == "history" for j in joined)
        checks.append(("all clients joined", replied == len(clients), f"{replied}/{len(clients)} got a history frame"))
        notes.append("no DynamoDB access in this mode: row cleanup and TTL are not checked, only delivery")
    if not all(ok for _, ok, _ in checks):
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
        return result

    sender, survivors, victims = clients[0], clients[: len(clients) - kill], clients[len(clients) - kill:]
    for v in victims:
        v.kill()
    killed_at = time.monotonic()
    run = uuid.uuid4().hex[:6]

    ok, detail = await broadcast(sender, survivors, room, f"stale:{run}:after-kill")
    checks.append(("broadcast right after the kill reaches every survivor", ok, detail))

    if counter:
        ok, rows, elapsed = await wait_for_count(counter, len(survivors), timeout=cleanup_timeout)
        total = time.monotonic() - killed_at
        result["cleanupSeconds"] = round(total, 1) if ok else None
        checks.append(("stale rows removed", ok,
                       f"{len(victims)} rows gone {total:.1f} s after the kill" if ok
                       else f"{rows} rows left after {cleanup_timeout:.0f} s, expected {len(survivors)}"))

    ok, detail = await broadcast(sender, survivors, room, f"stale:{run}:after-cleanup")
    checks.append(("broadcast after cleanup reaches every survivor", ok, detail))

    if stack:
        checks.extend(ttl_checks(stack, room))

    await asyncio.gather(*(c.close() for c in survivors), return_exceptions=True)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_target_args(ap)
    ap.add_argument("--clients", type=int, default=6, help="clients in the room (default 6)")
    ap.add_argument("--kill", type=int, default=3, help="clients to kill without closing (default 3)")
    ap.add_argument("--timeout", type=float, default=30, help="seconds to wait for cleanup (default 30)")
    ap.add_argument("--room", help="room id (default a fresh stale-<random> room)")
    args = ap.parse_args(argv)
    if not 1 <= args.kill < args.clients:
        ap.error("--kill must be at least 1 and less than --clients")

    room = args.room or f"stale-{uuid.uuid4().hex[:6]}"
    urls, stack = target_urls(args, [f"check-stale-{i}" for i in range(args.clients)])
    counter = dynamo_counter(stack, room) if stack else None
    result = asyncio.run(run_stale_check(urls, room=room, kill=args.kill, counter=counter, stack=stack,
                                         cleanup_timeout=args.timeout))
    passed = all(ok for _, ok, _ in result["checks"])
    if args.json:
        print(json.dumps({**result, "checks": [dict(zip(("name", "passed", "detail"), c, strict=True)) for c in result["checks"]],
                          "passed": passed}, indent=2))
    else:
        print_checks(f"Criterion 3: stale cleanup, room {room}", result["checks"], result["notes"])
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
