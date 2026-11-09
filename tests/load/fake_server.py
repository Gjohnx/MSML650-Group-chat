"""In-process stand-in for the WebSocket API, for CI. Owner: role 6.

Speaks the frames in docs/CONTRACTS.md (joinRoom -> history, sendMessage ->
message fan-out to the room) so the load test can be exercised without AWS.
It is not a model of the real backend's performance; it only proves the test
harness measures loss and latency correctly.

    python tests/load/fake_server.py --port 8765

Knobs for testing the harness itself: `drop_every` drops every Nth delivery,
`require_token` refuses handshakes without ?token= (HTTP 401, like the
authorizer), `delay_ms` adds latency to each delivery, `join_history=False`
skips the history frame on join (breaks criterion 5), and `keep_stale=True`
never removes dead connections from a room (breaks criterion 3).

getHistory pages like the real handler: newest first from the store, returned
oldest first, `nextBefore` is the sortKey of the oldest message on the page,
or null when nothing older is left. `room_size(room)` stands in for counting
the room's rows in the Connections table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response


class FakeChat:
    def __init__(self, *, drop_every: int = 0, require_token: bool = False, delay_ms: float = 0,
                 join_history: bool = True, keep_stale: bool = False, page_limit: int = 100) -> None:
        self.rooms: dict[str, set[ServerConnection]] = {}
        self.history: dict[str, list[dict]] = {}  # stored rows, ascending by sortKey
        self.drop_every, self.require_token, self.delay_ms = drop_every, require_token, delay_ms
        self.join_history, self.keep_stale, self.page_limit = join_history, keep_stale, page_limit
        self.deliveries = 0
        self._last_sent_at = 0

    def room_size(self, room: str) -> int:
        return len(self.rooms.get(room, ()))

    def store(self, room: str, body: str, client_sent_at=None, username: str = "fake") -> dict:
        sent_at = max(time.time_ns() // 1_000_000, self._last_sent_at + 1)
        self._last_sent_at = sent_at
        message_id = uuid.uuid4().hex.upper()
        row = {"roomId": room, "sortKey": f"{sent_at}#{message_id}", "messageId": message_id,
               "userId": username, "username": username, "body": body, "sentAt": sent_at}
        if client_sent_at is not None:
            row["clientSentAt"] = client_sent_at
        self.history.setdefault(room, []).append(row)
        return row

    @staticmethod
    def as_frame(row: dict) -> dict:
        return {"type": "message", **{k: v for k, v in row.items() if k != "sortKey"}}

    def page(self, room: str, before: str | None, limit: int) -> dict:
        rows = self.history.get(room, [])
        older = [r for r in rows if before is None or r["sortKey"] < before]
        page = older[-limit:]
        return {"type": "history", "roomId": room, "messages": [self.as_frame(r) for r in page],
                "nextBefore": page[0]["sortKey"] if len(older) > len(page) else None}

    def check_token(self, connection: ServerConnection, request: Request) -> Response | None:
        if not self.require_token:
            return None
        token = parse_qs(urlparse(request.path).query).get("token", [""])[0]
        if token != "good":
            return connection.respond(401, "Unauthorized\n")
        return None

    async def handler(self, ws: ServerConnection) -> None:
        joined: str | None = None
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    await ws.send(json.dumps({"type": "error", "code": "BAD_REQUEST", "message": "not JSON"}))
                    continue
                action, room = frame.get("action"), frame.get("roomId")
                if action == "joinRoom" and room:
                    if joined:
                        self.rooms.get(joined, set()).discard(ws)
                    joined = room
                    self.rooms.setdefault(room, set()).add(ws)
                    if self.join_history:
                        await ws.send(json.dumps(self.page(room, None, 50)))
                elif action == "sendMessage":
                    if room != joined:
                        await ws.send(json.dumps({"type": "error", "code": "NOT_IN_ROOM", "message": room}))
                        continue
                    row = self.store(room, frame.get("body", ""), frame.get("clientSentAt"))
                    await self.fan_out(room, json.dumps(self.as_frame(row)))
                elif action == "getHistory":
                    if room != joined:
                        await ws.send(json.dumps({"type": "error", "code": "NOT_IN_ROOM", "message": room}))
                        continue
                    limit = frame.get("limit", 50)
                    if type(limit) is not int or not 1 <= limit <= self.page_limit:
                        await ws.send(json.dumps({"type": "error", "code": "BAD_REQUEST", "message": "limit"}))
                        continue
                    await ws.send(json.dumps(self.page(room, frame.get("before"), limit)))
                else:
                    await ws.send(json.dumps({"type": "error", "code": "BAD_REQUEST", "message": f"{action}"}))
        except ConnectionClosed:
            pass  # a client that vanished without a close frame (the stale case)
        finally:
            if joined and not self.keep_stale:
                self.rooms.get(joined, set()).discard(ws)

    async def fan_out(self, room: str, payload: str) -> None:
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        for peer in list(self.rooms.get(room, ())):
            self.deliveries += 1
            if self.drop_every and self.deliveries % self.drop_every == 0:
                continue
            try:
                await peer.send(payload)
            except Exception:  # noqa: BLE001 - a gone peer is the stale-connection case
                if not self.keep_stale:
                    self.rooms[room].discard(peer)

    def serve(self, host: str = "127.0.0.1", port: int = 0):
        return serve(self.handler, host, port, process_request=self.check_token)


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--drop-every", type=int, default=0)
    ap.add_argument("--require-token", action="store_true")
    args = ap.parse_args()
    async with FakeChat(drop_every=args.drop_every, require_token=args.require_token).serve(port=args.port):
        print(f"fake chat on ws://127.0.0.1:{args.port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(_main())
