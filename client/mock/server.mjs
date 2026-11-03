// Local stand-in for the WebSocket API. Owner: role 5b.
//
// Serves client/ over HTTP and speaks the frames in docs/CONTRACTS.md on the
// same port, with in-memory rooms instead of DynamoDB. It lets the client be
// built and demoed before the Lambdas land.
//
// Tokens are decoded but NOT verified: any unexpired Cognito ID token works,
// so sign in through the real user pool as usual. No token gets 401 and a
// malformed or expired one gets 403, like API Gateway.
//
//   cd client && npm install && npm run mock        # http://localhost:8080
//   PORT=9000 MOCK_DELAY_MS=40 npm run mock          # simulate network delay

import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.PORT || 8080);
const DELAY_MS = Number(process.env.MOCK_DELAY_MS || 0);
const ROOT = resolve(fileURLToPath(new URL("..", import.meta.url)));
const ROOM_RE = /^[a-z0-9-]{1,64}$/;
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon" };

// ---- In-memory tables --------------------------------------------------------

const connections = new Map(); // ws -> { connectionId, userId, username, roomId }
const rooms = new Map(); // roomId -> messages sorted by sortKey ascending

const B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
function ulid(t = Date.now()) {
  let ts = "";
  for (let i = 0, n = t; i < 10; i++, n = Math.floor(n / 32)) ts = B32[n % 32] + ts;
  return ts + [...randomBytes(16)].map((b) => B32[b % 32]).join("");
}

function store(roomId, userId, username, body, sentAt, clientSentAt) {
  const messageId = ulid(sentAt);
  const m = { roomId, sortKey: `${sentAt}#${messageId}`, messageId, userId, username, body, sentAt };
  if (clientSentAt !== undefined) m.clientSentAt = clientSentAt;
  const list = rooms.get(roomId) || [];
  list.push(m);
  list.sort((a, b) => (a.sortKey < b.sortKey ? -1 : 1));
  rooms.set(roomId, list);
  return m;
}

function frame(m) {
  const { sortKey, ...rest } = m;
  return { type: "message", ...rest };
}

// 120 old messages in #general, so history and "load older" have something to page through.
const seedStart = Date.now() - 120 * 60_000;
for (let i = 1; i <= 120; i++) store("general", "seed", "seed-bot", `Seeded message ${i} of 120`, seedStart + i * 60_000);

// ---- Routes -------------------------------------------------------------------

function send(ws, f) {
  const data = JSON.stringify(f);
  const go = () => ws.readyState === ws.OPEN && ws.send(data);
  DELAY_MS ? setTimeout(go, DELAY_MS) : go();
}

const error = (ws, code, message) => send(ws, { type: "error", code, message });

function history(roomId, before, limit) {
  const all = rooms.get(roomId) || [];
  const older = before ? all.filter((m) => m.sortKey < before) : all;
  const page = older.slice(-limit);
  return {
    type: "history",
    roomId,
    messages: page.map(frame),
    nextBefore: older.length > page.length ? page[0].sortKey : null,
  };
}

const routes = {
  joinRoom(ws, conn, f) {
    if (!ROOM_RE.test(f.roomId || "")) return error(ws, "BAD_REQUEST", "roomId must be 1 to 64 chars of [a-z0-9-]");
    conn.roomId = f.roomId;
    send(ws, history(f.roomId, null, 50));
    const presence = { type: "presence", roomId: f.roomId, userId: conn.userId, username: conn.username, event: "joined" };
    for (const [other, c] of connections) if (other !== ws && c.roomId === f.roomId) send(other, presence);
  },

  sendMessage(ws, conn, f) {
    const body = typeof f.body === "string" ? f.body.trim() : "";
    if (!body || body.length > 2000) return error(ws, "BAD_REQUEST", "body must be 1 to 2000 chars");
    if (!f.roomId || f.roomId !== conn.roomId) return error(ws, "NOT_IN_ROOM", "join the room before sending");
    const clientSentAt = typeof f.clientSentAt === "number" ? f.clientSentAt : undefined;
    const m = frame(store(conn.roomId, conn.userId, conn.username, body, Date.now(), clientSentAt));
    for (const [other, c] of connections) if (c.roomId === conn.roomId) send(other, m);
  },

  getHistory(ws, conn, f) {
    if (!f.roomId || f.roomId !== conn.roomId) return error(ws, "NOT_IN_ROOM", "join the room before reading history");
    const limit = Math.min(100, Math.max(1, Number.parseInt(f.limit, 10) || 50));
    send(ws, history(f.roomId, typeof f.before === "string" ? f.before : null, limit));
  },
};

// ---- $connect: decode (not verify) the ID token -------------------------------

function userFromToken(token) {
  try {
    const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
    if (!claims.sub || !claims.exp || claims.exp * 1000 < Date.now()) return null;
    return { userId: claims.sub, username: claims["cognito:username"] || claims.sub };
  } catch {
    return null;
  }
}

// ---- HTTP: static client files + a config.json pointing at this server --------

async function serveStatic(req, res) {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (path === "/config.json") {
    let real = {};
    try {
      real = JSON.parse(await readFile(join(ROOT, "config.json"), "utf8"));
    } catch {
      console.warn("client/config.json not found; Cognito sign-in won't work until it exists");
    }
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    return res.end(JSON.stringify({ ...real, wsUrl: `ws://localhost:${PORT}` }));
  }
  const file = resolve(ROOT, "." + (path.endsWith("/") ? path + "index.html" : path));
  const blocked = !file.startsWith(ROOT + sep) || /[\\/](mock|node_modules)[\\/]/.test(file.slice(ROOT.length));
  try {
    if (blocked) throw new Error("blocked");
    const data = await readFile(file);
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream", "Cache-Control": "no-store" });
    res.end(data);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain" });
    res.end("not found");
  }
}

const server = createServer(serveStatic);
const wss = new WebSocketServer({ noServer: true });

server.on("upgrade", (req, socket, head) => {
  const token = new URL(req.url, "http://x").searchParams.get("token");
  const user = token && userFromToken(token);
  if (!user) {
    socket.end(token ? "HTTP/1.1 403 Forbidden\r\n\r\n" : "HTTP/1.1 401 Unauthorized\r\n\r\n");
    console.log(`$connect rejected (${token ? 403 : 401})`);
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    const conn = { connectionId: randomBytes(8).toString("base64url"), ...user, roomId: "" };
    connections.set(ws, conn);
    console.log(`$connect ${conn.username} (${conn.connectionId})`);

    ws.on("message", (raw) => {
      let f;
      try {
        f = JSON.parse(raw);
      } catch {
        return error(ws, "BAD_REQUEST", "frames must be JSON");
      }
      const route = routes[f?.action];
      if (!route) return error(ws, "BAD_REQUEST", `unknown action ${JSON.stringify(f?.action)}`);
      route(ws, conn, f);
    });
    ws.on("close", () => {
      connections.delete(ws);
      console.log(`$disconnect ${conn.username} (${conn.connectionId})`);
    });
  });
});

server.listen(PORT, () => console.log(`mock chat API + client on http://localhost:${PORT}`));
