#!/usr/bin/env python3
"""Load and latency test for the group chat WebSocket API. Owner: role 6.

Proves success criteria 2 and 4 (docs/CONTRACTS.md section 7):

  4. Concurrent load without message loss: N clients join one room, each sends
     M numbered messages, and every client must receive every one of the N x M
     messages exactly once.
  2. Under 200 ms broadcast latency: each received message is timed from the
     sender's `clientSentAt` to the moment a receiver gets it. All clients run
     in this one process, so both ends read the same clock.

Against a deployed stack (creates Cognito users loadtest-0 .. loadtest-N-1 on
the first run, so pass --create once):

    cd infra && source .venv/bin/activate && cd ..
    pip install -r tests/load/requirements.txt
    python tests/load/load_test.py --owner john --clients 20 --messages 10 --create

Against any WebSocket URL that needs no token (the local fake server in CI):

    python tests/load/load_test.py --url ws://127.0.0.1:8765 --clients 5

Exits 0 when nothing was lost and p95 latency is under --max-p95-ms, 1
otherwise, so CI can use it as a gate. --json prints the full report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

DEFAULT_PASSWORD = "Chat-test-1234"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ranked = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ranked)))
    return ranked[rank - 1]


@dataclass
class Client:
    index: int
    url: str
    received: dict[str, int] = field(default_factory=dict)  # body -> times seen
    latencies_ms: list[float] = field(default_factory=list)
    got_history: bool = False
    errors: list[str] = field(default_factory=list)
    done_sending: asyncio.Event = field(default_factory=asyncio.Event)


def message_body(run_id: str, sender: int, seq: int) -> str:
    return f"load:{run_id}:{sender}:{seq}"


async def run_client(client: Client, *, room: str, run_id: str, messages: int, interval: float,
                     all_joined: asyncio.Barrier, all_sent: asyncio.Event, settle: float,
                     join_timeout: float) -> None:
    try:
        ws = await asyncio.wait_for(connect(client.url, open_timeout=join_timeout), join_timeout)
    except InvalidStatus as e:
        client.errors.append(f"connect refused: HTTP {e.response.status_code}")
        client.done_sending.set()
        await all_joined.abort()
        return
    except Exception as e:  # noqa: BLE001 - report any connect failure, keep the run going
        client.errors.append(f"connect failed: {type(e).__name__}: {e}")
        client.done_sending.set()
        await all_joined.abort()
        return

    async with ws:
        prefix = f"load:{run_id}:"

        async def reader() -> None:
            async for raw in ws:
                arrived = now_ms()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    client.errors.append(f"non-JSON frame: {str(raw)[:80]}")
                    continue
                kind = frame.get("type")
                if kind == "history":
                    client.got_history = True
                elif kind == "message" and str(frame.get("body", "")).startswith(prefix):
                    body = frame["body"]
                    client.received[body] = client.received.get(body, 0) + 1
                    sent = frame.get("clientSentAt")
                    if isinstance(sent, (int, float)):
                        client.latencies_ms.append(arrived - sent)
                elif kind == "error":
                    client.errors.append(f"server error {frame.get('code')}: {frame.get('message')}")

        read_task = asyncio.create_task(reader())
        try:
            await ws.send(json.dumps({"action": "joinRoom", "roomId": room}))
            # Everyone must be in the room before anyone sends, or early
            # messages would legitimately miss late joiners.
            await asyncio.wait_for(all_joined.wait(), join_timeout)
            for seq in range(messages):
                await ws.send(json.dumps({
                    "action": "sendMessage", "roomId": room,
                    "body": message_body(run_id, client.index, seq), "clientSentAt": now_ms(),
                }))
                if interval:
                    await asyncio.sleep(interval)
            client.done_sending.set()
            await all_sent.wait()
            await asyncio.sleep(settle)  # let in-flight broadcasts arrive
        except asyncio.BrokenBarrierError:
            client.errors.append("aborted: another client failed to connect")
        except TimeoutError:
            client.errors.append("timed out waiting for all clients to join")
        finally:
            client.done_sending.set()
            read_task.cancel()


async def run_load(urls: list[str], *, room: str, messages: int, interval: float = 0.0,
                   settle: float = 3.0, join_timeout: float = 15.0, join_wait: float = 1.0) -> dict:
    """Run one load test against the given connect URLs (one per client)."""
    run_id = uuid.uuid4().hex[:8]
    clients = [Client(i, url) for i, url in enumerate(urls)]
    all_joined = asyncio.Barrier(len(clients))
    all_sent = asyncio.Event()

    async def one(c: Client) -> None:
        await run_client(c, room=room, run_id=run_id, messages=messages, interval=interval,
                         all_joined=_DelayedBarrier(all_joined, join_wait), all_sent=all_sent,
                         settle=settle, join_timeout=join_timeout)

    started = time.monotonic()
    tasks = [asyncio.create_task(one(c)) for c in clients]
    # all_sent fires once every client has finished sending or failed, so the
    # settle window is measured from the last send, not from each client's own.
    await asyncio.gather(*(c.done_sending.wait() for c in clients))
    all_sent.set()
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - started
    return build_report(clients, run_id=run_id, room=room, messages=messages, elapsed=elapsed)


class _DelayedBarrier:
    """Barrier wait plus a short pause so joinRoom writes land before sends."""

    def __init__(self, barrier: asyncio.Barrier, delay: float) -> None:
        self.barrier, self.delay = barrier, delay

    async def wait(self) -> None:
        await self.barrier.wait()
        await asyncio.sleep(self.delay)

    async def abort(self) -> None:
        await self.barrier.abort()


def build_report(clients: list[Client], *, run_id: str, room: str, messages: int, elapsed: float) -> dict:
    connected = [c for c in clients if not any(e.startswith("connect") for e in c.errors)]
    expected = {message_body(run_id, s.index, q) for s in connected for q in range(messages)}
    missing = duplicated = 0
    per_client = []
    for c in clients:
        miss = len(expected - c.received.keys()) if c in connected else None
        dup = sum(n - 1 for n in c.received.values() if n > 1)
        missing += miss or 0
        duplicated += dup
        per_client.append({"client": c.index, "received": len(c.received), "missing": miss,
                           "duplicates": dup, "gotHistory": c.got_history, "errors": c.errors})
    latencies = [x for c in clients for x in c.latencies_ms]
    return {
        "runId": run_id, "room": room, "clients": len(clients), "connected": len(connected),
        "messagesPerClient": messages, "expectedPerClient": len(expected),
        "deliveries": sum(len(c.received) for c in clients),
        "missing": missing, "duplicates": duplicated,
        "latencyMs": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95),
                      "max": max(latencies) if latencies else None, "samples": len(latencies)},
        "elapsedSeconds": round(elapsed, 2), "perClient": per_client,
    }


def verdict(report: dict, max_p95_ms: float) -> list[str]:
    """Reasons the run failed; empty means it passed."""
    problems = []
    if report["connected"] < report["clients"]:
        problems.append(f"{report['clients'] - report['connected']} of {report['clients']} clients could not connect")
    if report["missing"]:
        problems.append(f"{report['missing']} messages lost (criterion 4)")
    if report["duplicates"]:
        problems.append(f"{report['duplicates']} duplicate deliveries")
    p95 = report["latencyMs"]["p95"]
    if p95 is None:
        problems.append("no latency samples: no broadcasts were received")
    elif p95 > max_p95_ms:
        problems.append(f"p95 latency {p95:.0f} ms is over {max_p95_ms:.0f} ms (criterion 2)")
    return problems


def cognito_urls(owner: str, clients: int, *, create: bool, profile: str | None, password: str) -> list[str]:
    """One connect URL per simulated user, using tools/get_token.py."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import boto3
    from get_token import ensure_user, get_id_token, stack_outputs

    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    outputs = stack_outputs(session, owner)
    idp = session.client("cognito-idp")
    urls = []
    for i in range(clients):
        username = f"loadtest-{i}"
        if create:
            ensure_user(idp, outputs["UserPoolId"], username, password)
        token = get_id_token(idp, outputs["AppClientId"], username, password)
        urls.append(f"{outputs['WebSocketUrl']}?token={token}")
    return urls


def print_summary(report: dict, problems: list[str]) -> None:
    lat = report["latencyMs"]
    fmt = lambda v: "n/a" if v is None else f"{v:.0f} ms"  # noqa: E731
    print(f"run {report['runId']} in room {report['room']}: {report['connected']}/{report['clients']} clients "
          f"connected, {report['messagesPerClient']} messages each, {report['elapsedSeconds']} s")
    print(f"deliveries {report['deliveries']}, missing {report['missing']}, duplicates {report['duplicates']}")
    print(f"latency p50 {fmt(lat['p50'])}, p95 {fmt(lat['p95'])}, max {fmt(lat['max'])} "
          f"over {lat['samples']} samples")
    errors = sorted({e for c in report["perClient"] for e in c["errors"]})
    for e in errors[:10]:
        print(f"  error: {e}")
    print("PASS" if not problems else "FAIL: " + "; ".join(problems))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner", help="test the deployed stack GroupChat-<owner>")
    target.add_argument("--url", help="test this WebSocket URL directly, no token")
    ap.add_argument("--clients", type=int, default=10, help="concurrent clients (default 10)")
    ap.add_argument("--messages", type=int, default=10, help="messages each client sends (default 10)")
    ap.add_argument("--interval", type=float, default=0.05, help="seconds between a client's sends (default 0.05)")
    ap.add_argument("--room", default=None, help="room id (default a fresh load-<random> room)")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds to wait for late deliveries (default 3)")
    ap.add_argument("--max-p95-ms", type=float, default=200.0, help="fail above this p95 (default 200)")
    ap.add_argument("--create", action="store_true", help="create the loadtest-N Cognito users first")
    ap.add_argument("--profile", default="msml650", help="AWS CLI profile; pass '' to use env credentials")
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args(argv)

    if args.clients < 1 or args.messages < 1:
        ap.error("--clients and --messages must be at least 1")
    room = args.room or f"load-{uuid.uuid4().hex[:6]}"
    if args.url:
        urls = [args.url] * args.clients
    else:
        urls = cognito_urls(args.owner, args.clients, create=args.create,
                            profile=args.profile or None, password=args.password)

    report = asyncio.run(run_load(urls, room=room, messages=args.messages,
                                  interval=args.interval, settle=args.settle))
    problems = verdict(report, args.max_p95_ms)
    report["passed"], report["problems"] = not problems, problems
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(report, problems)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
