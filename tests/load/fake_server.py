"""In-process stand-in for the WebSocket API, for CI. Owner: role 6.

Speaks the frames in docs/CONTRACTS.md (joinRoom -> history, sendMessage ->
message fan-out to the room) so the load test can be exercised without AWS.
It is not a model of the real backend's performance; it only proves the test
harness measures loss and latency correctly.

    python tests/load/fake_server.py --port 8765

Knobs for testing the harness itself: `drop_every` drops every Nth delivery,
`require_token` refuses handshakes without ?token= (HTTP 401, like the
authorizer), `delay_ms` adds latency to each delivery.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response


class FakeChat:
    def __init__(self, *, drop_every: int = 0, require_token: bool = False, delay_ms: float = 0) -> None:
        self.rooms: dict[str, set[ServerConnection]] = {}
        self.history: dict[str, list[dict]] = {}
        self.drop_every, self.require_token, self.delay_ms = drop_every, require_token, delay_ms
        self.deliveries = 0

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
                    await ws.send(json.dumps({"type": "history", "roomId": room,
                                              "messages": self.history.get(room, [])[-50:], "nextBefore": None}))
                elif action == "sendMessage":
                    if room != joined:
                        await ws.send(json.dumps({"type": "error", "code": "NOT_IN_ROOM", "message": room}))
                        continue
                    msg = {"type": "message", "roomId": room, "messageId": uuid.uuid4().hex,
                           "userId": "fake", "username": "fake", "body": frame.get("body", ""),
                           "sentAt": time.time_ns() // 1_000_000, "clientSentAt": frame.get("clientSentAt")}
                    self.history.setdefault(room, []).append(msg)
                    await self.fan_out(room, json.dumps(msg))
                else:
                    await ws.send(json.dumps({"type": "error", "code": "BAD_REQUEST", "message": f"{action}"}))
        finally:
            if joined:
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
