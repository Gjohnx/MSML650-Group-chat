#!/usr/bin/env python3
"""Check success criterion 5: persisted chat history is returned when a user joins.

Seeds a fresh room with numbered messages, then a new user joins while a third
user keeps chatting in the room. It checks that:

1. a `history` frame arrives on join,
2. it arrives before any new `message` frame for the room (docs/CONTRACTS.md
   section 7),
3. the first page holds the newest messages, oldest first, in the contract's
   message shape,
4. paging back with getHistory returns every seeded message exactly once, in
   order, and
5. the last page says `nextBefore: null`.

120 seeded messages with the default page size of 50 should page as
50 + 50 + 20.

    python tests/smoke/check_history.py --owner john --create          # deployed stack
    python tests/smoke/check_history.py --owner john --seed-via dynamo # if sendMessage is not ready
    python tests/smoke/check_history.py --url ws://localhost:8080 --mock-tokens   # Sai's mock
    python tests/smoke/check_history.py --url ws://127.0.0.1:8765      # Python fake server

Exits 0 when every check passes. Owner: role 6.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from itertools import pairwise

from common import Client, add_target_args, now_ms, print_checks, target_urls

REQUIRED_FIELDS = ("type", "roomId", "messageId", "userId", "username", "body", "sentAt")


def seed_body(run: str, i: int) -> str:
    return f"hist:{run}:{i:04d}"


async def seed_over_websocket(client: Client, room: str, run: str, count: int) -> str | None:
    """Send `count` numbered messages, each confirmed by its own echo. Returns an error or None."""
    await client.send({"action": "joinRoom", "roomId": room})
    await client.wait_for(lambda f: f.get("type") in ("history", "error"), timeout=10)
    for i in range(count):
        body = seed_body(run, i)
        start = len(client.frames)
        await client.send({"action": "sendMessage", "roomId": room, "body": body, "clientSentAt": now_ms()})
        echo = await client.wait_for(lambda f, b=body: f.get("type") == "message" and f.get("body") == b,
                                     timeout=10, start=start)
        if echo is None:
            errors = client.errors()
            return f"message {i} was not echoed back" + (f" (server said {errors[-1]})" if errors else "")
    return None


def seed_in_dynamodb(table, room: str, run: str, count: int) -> None:
    """Write seeded messages straight into the Messages table, one ms apart."""
    base = now_ms() - count * 1000
    with table.batch_writer() as batch:
        for i in range(count):
            sent_at = base + i * 1000
            message_id = f"{sent_at:013d}{uuid.uuid4().hex[:13].upper()}"
            batch.put_item(Item={"roomId": room, "sortKey": f"{sent_at}#{message_id}", "messageId": message_id,
                                 "userId": "seed", "username": "seed-bot", "body": seed_body(run, i),
                                 "sentAt": sent_at})


def seeded_in_order(messages: list[dict], prefix: str) -> tuple[bool, int]:
    """Messages oldest first by sentAt, and in seed order across different milliseconds.

    sortKey is "<sentAt>#<messageId>", so two messages in the same millisecond
    are ordered by message id, not by send order. Ties are counted, not failed.
    Returns (in order, number of ties).
    """
    if any(not isinstance(m.get("sentAt"), (int, float)) for m in messages):
        return False, 0
    groups: list[list[int]] = []  # seed indexes, one group per sentAt, in returned order
    last = None
    ties = 0
    for m in messages:
        if m["sentAt"] == last:
            ties += 1
            groups[-1].append(int(m["body"][len(prefix):]))
        else:
            if last is not None and m["sentAt"] < last:
                return False, ties
            groups.append([int(m["body"][len(prefix):])])
            last = m["sentAt"]
    return all(max(a) < min(b) for a, b in pairwise(groups)), ties


async def chatter(client: Client, room: str, run: str, stop: asyncio.Event) -> int:
    """Keep sending live messages until stopped; returns how many were sent."""
    await client.send({"action": "joinRoom", "roomId": room})
    await client.wait_for(lambda f: f.get("type") in ("history", "error"), timeout=10)
    sent = 0
    while not stop.is_set():
        await client.send({"action": "sendMessage", "roomId": room, "body": f"live:{run}:{sent}",
                           "clientSentAt": now_ms()})
        sent += 1
        try:
            await asyncio.wait_for(stop.wait(), 0.05)
        except TimeoutError:
            pass
    return sent


async def run_history_check(urls: list[str], *, room: str, count: int = 120, page_limit: int = 50,
                            seed_via: str = "ws", stack=None, live: bool = True) -> dict:
    """urls = [seeder, joiner, chatter]. Returns checks, notes and details."""
    run = uuid.uuid4().hex[:6]
    seeder, joiner, talker = (Client(n, u) for n, u in zip(("seeder", "joiner", "chatter"), urls, strict=True))
    checks: list[tuple[str, bool, str]] = []
    notes: list[str] = []
    result = {"room": room, "seeded": count, "pageSizes": [], "checks": checks, "notes": notes}

    # 1. Seed the room.
    if seed_via == "dynamo":
        seed_in_dynamodb(stack.table("Messages"), room, run, count)
        checks.append(("seed room", True, f"{count} messages written to the Messages table"))
    else:
        await seeder.open()
        problem = await seed_over_websocket(seeder, room, run, count)
        await seeder.close()
        checks.append(("seed room", problem is None, problem or f"{count} messages sent and echoed"))
        if problem:
            return result

    # 2. A third user chats while the new user joins.
    stop = asyncio.Event()
    talk_task = None
    if live:
        await talker.open()
        talk_task = asyncio.create_task(chatter(talker, room, run, stop))
        await asyncio.sleep(0.3)

    # 3. The new user joins.
    await joiner.open()
    await joiner.send({"action": "joinRoom", "roomId": room})
    first = await joiner.wait_for(lambda f: f.get("type") == "history" and f.get("roomId") == room, timeout=10)
    await asyncio.sleep(0.5)  # let live messages race the history frame
    stop.set()
    live_sent = await talk_task if talk_task else 0
    if live:
        await talker.close()

    checks.append(("history frame on join", first is not None,
                   "received" if first else f"none within 10 s; errors: {joiner.errors() or 'none'}"))
    if first is None:
        await joiner.close()
        return result

    room_frames = [f for _, f in joiner.frames if f.get("roomId") == room and f.get("type") in ("history", "message")]
    history_index = next(i for i, f in enumerate(room_frames) if f.get("type") == "history")
    early = [f for f in room_frames[:history_index] if f.get("type") == "message"]
    live_seen = len([f for f in room_frames if f.get("type") == "message"])
    checks.append(("history before any new message", not early,
                   f"{len(early)} live messages arrived first" if early
                   else f"history came first; {live_seen} live messages after it ({live_sent} sent)"))

    page = first.get("messages", [])
    shape_problems = [m for m in page if any(k not in m for k in REQUIRED_FIELDS) or m.get("type") != "message"]
    ordered = all(a["sentAt"] <= b["sentAt"] for a, b in pairwise(page)) if not shape_problems else False
    checks.append(("first page shape and order", bool(page) and not shape_problems and ordered,
                   f"{len(page)} messages, oldest first" if page and not shape_problems and ordered
                   else f"{len(page)} messages; {len(shape_problems)} malformed; ordered={ordered}"))
    if any("sortKey" in m for m in page):
        notes.append("history messages include sortKey, which the contract's message frame does not have")

    # 4. Page back to the start of the room.
    pages = [page]
    before = first.get("nextBefore")
    ended_with_null = before is None
    for _ in range(50):
        if before is None:
            break
        start = len(joiner.frames)
        await joiner.send({"action": "getHistory", "roomId": room, "before": before, "limit": page_limit})
        reply = await joiner.wait_for(lambda f: f.get("type") in ("history", "error"), timeout=10, start=start)
        if reply is None or reply.get("type") == "error":
            detail = "no reply within 10 s" if reply is None else f"{reply.get('code')}: {reply.get('message')}"
            checks.append(("getHistory paging", False, f"page {len(pages) + 1}: {detail}"))
            await joiner.close()
            return result
        pages.append(reply.get("messages", []))
        before = reply.get("nextBefore")
        ended_with_null = before is None
    await joiner.close()

    sizes = [len(p) for p in pages]
    result["pageSizes"] = sizes
    if len(pages) > 1 and sizes[-1] == 0:
        notes.append("the last getHistory page was empty: the page before it should already have said "
                     "nextBefore null (DynamoDB returns LastEvaluatedKey when exactly `limit` items remain)")

    everything = [m for p in reversed(pages) for m in p]
    prefix = f"hist:{run}:"
    seeded_msgs = [m for m in everything if isinstance(m.get("body"), str) and m["body"].startswith(prefix)]
    seeded = [m["body"] for m in seeded_msgs]
    expected = [seed_body(run, i) for i in range(count)]
    missing = len(set(expected) - set(seeded))
    duplicates = len(seeded) - len(set(seeded))
    in_order, ties = seeded_in_order(seeded_msgs, prefix)
    if ties:
        notes.append(f"{ties} seeded messages share a millisecond with the one before; the contract orders "
                     "only by sentAt, so their relative order is not checked")
    checks.append(("every seeded message returned once, in order", not missing and not duplicates and in_order,
                   f"{' + '.join(map(str, sizes))} across {len(pages)} pages; missing {missing}, "
                   f"duplicates {duplicates}, in order {in_order}"))
    checks.append(("last page ends with nextBefore null", ended_with_null,
                   "yes" if ended_with_null else "stopped after 50 pages without reaching the start"))
    errors = joiner.errors()
    checks.append(("no errors for the joining user", not errors, "none" if not errors else "; ".join(errors[:3])))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_target_args(ap)
    ap.add_argument("--messages", type=int, default=120, help="messages to seed (default 120)")
    ap.add_argument("--limit", type=int, default=50, help="getHistory page size (default 50)")
    ap.add_argument("--seed-via", choices=("ws", "dynamo"), default="ws",
                    help="seed with sendMessage (default) or write straight to the Messages table (--owner only)")
    ap.add_argument("--no-live", action="store_true", help="skip the live chatter during join")
    ap.add_argument("--room", help="room id (default a fresh hist-<random> room)")
    args = ap.parse_args(argv)
    if args.seed_via == "dynamo" and not args.owner:
        ap.error("--seed-via dynamo needs --owner")

    room = args.room or f"hist-{uuid.uuid4().hex[:6]}"
    urls, stack = target_urls(args, ["check-seeder", "check-joiner", "check-chatter"])
    result = asyncio.run(run_history_check(urls, room=room, count=args.messages, page_limit=args.limit,
                                           seed_via=args.seed_via, stack=stack, live=not args.no_live))
    passed = all(ok for _, ok, _ in result["checks"])
    if args.json:
        print(json.dumps({**result, "checks": [dict(zip(("name", "passed", "detail"), c, strict=True)) for c in result["checks"]],
                          "passed": passed}, indent=2))
    else:
        print_checks(f"Criterion 5: history on join, room {room}", result["checks"], result["notes"])
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
