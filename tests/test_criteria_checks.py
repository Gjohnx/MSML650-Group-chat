"""Tests for the criterion 3 and 5 checks, run against the in-process fake server.

Each check must pass against a backend that follows docs/CONTRACTS.md and fail
against one that breaks the criterion, so a green run on AWS means something.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests" / "load"), str(ROOT / "tests" / "smoke")]

from check_history import run_history_check, seeded_in_order  # noqa: E402
from check_stale import run_stale_check  # noqa: E402
from fake_server import FakeChat  # noqa: E402


def with_fake(fake: FakeChat, scenario):
    async def go():
        async with fake.serve() as server:
            return await scenario(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    return asyncio.run(go())


def outcome(result) -> dict:
    return {name: ok for name, ok, _ in result["checks"]}


# ---- Criterion 5 ------------------------------------------------------------------

def test_history_check_passes_and_pages_50_50_20():
    result = with_fake(FakeChat(), lambda url: run_history_check([url] * 3, room="r", live=False))
    assert all(outcome(result).values()), result["checks"]
    assert result["pageSizes"] == [50, 50, 20]


def test_history_check_passes_with_live_traffic():
    result = with_fake(FakeChat(), lambda url: run_history_check([url] * 3, room="r", count=60))
    assert all(outcome(result).values()), result["checks"]


def test_history_check_fails_without_history_on_join():
    result = with_fake(FakeChat(join_history=False),
                       lambda url: run_history_check([url] * 3, room="r", count=5, live=False))
    assert outcome(result)["history frame on join"] is False


def test_history_check_fails_when_server_rejects_paging():
    result = with_fake(FakeChat(page_limit=10),
                       lambda url: run_history_check([url] * 3, room="r", count=120, live=False))
    assert outcome(result)["getHistory paging"] is False


def test_order_check_allows_same_millisecond_ties_only():
    p = "hist:x:"
    def msg(i, t):
        return {"body": f"{p}{i:04d}", "sentAt": t}
    assert seeded_in_order([msg(0, 1), msg(2, 2), msg(1, 2), msg(3, 3)], p) == (True, 1)
    assert seeded_in_order([msg(1, 1), msg(0, 2)], p)[0] is False
    assert seeded_in_order([msg(0, 2), msg(1, 1)], p)[0] is False


# ---- Criterion 3 ------------------------------------------------------------------

def stale(fake: FakeChat, timeout: float = 5):
    return with_fake(fake, lambda url: run_stale_check([url] * 6, room="r", kill=3,
                                                       counter=lambda: fake.room_size("r"),
                                                       cleanup_timeout=timeout))


def test_stale_check_passes_when_dead_connections_are_removed():
    result = stale(FakeChat())
    assert all(outcome(result).values()), result["checks"]


def test_stale_check_fails_when_dead_connections_linger():
    result = stale(FakeChat(keep_stale=True), timeout=2)
    checks = outcome(result)
    assert checks["stale rows removed"] is False
    assert checks["broadcast right after the kill reaches every survivor"] is True


def test_stale_check_without_row_access_checks_delivery_only():
    result = with_fake(FakeChat(), lambda url: run_stale_check([url] * 4, room="r", kill=2))
    assert all(outcome(result).values())
    assert "stale rows removed" not in outcome(result)
    assert any("not checked" in n for n in result["notes"])
