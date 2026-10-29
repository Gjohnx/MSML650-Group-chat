"""Tests for the load and smoke scripts, run against the in-process fake server.

These check that the harness itself is right: a clean run passes, a lossy
server is caught, and a refused handshake is reported. Run with `pytest`.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests" / "load"), str(ROOT / "tests" / "smoke")]

import check_auth  # noqa: E402
import load_test  # noqa: E402
from fake_server import FakeChat  # noqa: E402


def run_against(fake: FakeChat, *, clients: int, messages: int, query: str = "") -> dict:
    async def go():
        async with fake.serve() as server:
            port = server.sockets[0].getsockname()[1]
            urls = [f"ws://127.0.0.1:{port}{query}"] * clients
            return await load_test.run_load(urls, room="test", messages=messages, interval=0,
                                            settle=0.5, join_wait=0.05)
    return asyncio.run(go())


def test_clean_run_delivers_everything():
    report = run_against(FakeChat(), clients=5, messages=8)
    assert report["connected"] == 5
    assert report["expectedPerClient"] == 40
    assert report["deliveries"] == 200
    assert report["missing"] == 0 and report["duplicates"] == 0
    assert report["latencyMs"]["samples"] == 200
    assert all(c["gotHistory"] for c in report["perClient"])
    assert load_test.verdict(report, max_p95_ms=200) == []


def test_lost_messages_fail_the_run():
    report = run_against(FakeChat(drop_every=7), clients=4, messages=10)
    assert report["missing"] > 0
    assert any("lost" in p for p in load_test.verdict(report, max_p95_ms=200))


def test_slow_broadcast_fails_latency_gate():
    report = run_against(FakeChat(delay_ms=60), clients=2, messages=3)
    assert report["missing"] == 0
    assert report["latencyMs"]["p50"] >= 60
    assert any("p95" in p for p in load_test.verdict(report, max_p95_ms=50))


def test_refused_handshake_is_reported_not_hung():
    report = run_against(FakeChat(require_token=True), clients=3, messages=2)
    assert report["connected"] == 0
    assert any("could not connect" in p for p in load_test.verdict(report, max_p95_ms=200))
    assert "connect refused: HTTP 401" in report["perClient"][0]["errors"]


def test_percentile_nearest_rank():
    assert load_test.percentile([], 50) is None
    assert load_test.percentile([5], 95) == 5
    assert load_test.percentile(list(range(1, 101)), 95) == 95


def test_auth_smoke_against_fake():
    async def go():
        async with FakeChat(require_token=True).serve() as server:
            port = server.sockets[0].getsockname()[1]
            return await check_auth.run(f"ws://127.0.0.1:{port}", token="good")
    checks = asyncio.run(go())
    assert [(name, ok) for name, ok, _ in checks] == [
        ("no token", True), ("forged token", True), ("valid token", True)]


def test_auth_smoke_catches_open_endpoint():
    async def go():
        async with FakeChat().serve() as server:
            port = server.sockets[0].getsockname()[1]
            return await check_auth.run(f"ws://127.0.0.1:{port}", token=None)
    checks = asyncio.run(go())
    assert not any(ok for _, ok, _ in checks)
