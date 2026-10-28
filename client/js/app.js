// Web client: sign-in, room picker, chat window, latency readout. Owner: role 5b.
//
// Frame shapes are in docs/CONTRACTS.md sections 2 and 3. The client never
// sends userId or username; the server takes them from the authorizer.

import * as auth from "./auth.js";
import { ChatSocket } from "./socket.js";

const ROOM_RE = /^[a-z0-9-]{1,64}$/;
const RECENT_KEY = "groupchat.rooms";
const PAGE = 50;
const $ = (id) => document.getElementById(id);

let socket = null;
let state = "offline";
let room = null; // the room we're in, or joining once the socket opens
let messages = new Map(); // messageId -> message frame, current room only
let notices = []; // presence lines for the current room
let nextBefore = null;
let loadingOlder = false;
const latency = []; // ms samples from our own echoed messages

main();

async function main() {
  let config;
  try {
    const res = await fetch("config.json", { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    config = await res.json();
  } catch {
    return fatal("Could not load config.json. Run tools/deploy_client.py, or start the mock server (see client/README.md).");
  }
  auth.initAuth(config);
  socket = new ChatSocket({ url: config.wsUrl, getToken: auth.idToken, onFrame, onState });
  wireAuth();
  wireChat();
  if (auth.currentUser()) enterChat();
  else showAuth();
}

function fatal(text) {
  $("fatal").textContent = text;
  $("fatal").hidden = false;
}

// ---- Auth view -------------------------------------------------------------

function showAuth(msg) {
  $("chatView").hidden = true;
  $("authView").hidden = false;
  $("whoami").hidden = true;
  $("signOut").hidden = true;
  authMsg(msg || "");
}

function authMsg(text, isError = false) {
  const el = $("authMsg");
  el.textContent = text;
  el.hidden = !text;
  el.classList.toggle("error", isError);
}

function showTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const f of document.querySelectorAll("[data-pane]")) f.hidden = f.dataset.pane !== name;
}

// Runs an async form action with the submit button disabled and errors shown.
function onSubmit(form, action) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    authMsg("");
    try {
      await action(Object.fromEntries(new FormData(form)));
    } catch (err) {
      if (err.code === "UserNotConfirmedException") {
        $("confirmForm").username.value = form.username.value;
        showTab("confirm");
        authMsg("This account isn't confirmed yet. Enter the code from your email.", true);
      } else {
        authMsg(err.message, true);
      }
    } finally {
      button.disabled = false;
    }
  });
}

function wireAuth() {
  for (const b of document.querySelectorAll(".tabs button")) {
    b.addEventListener("click", () => {
      showTab(b.dataset.tab);
      authMsg("");
    });
  }

  onSubmit($("signInForm"), async ({ username, password }) => {
    await auth.signIn(username.trim(), password);
    $("signInForm").reset();
    enterChat();
  });

  onSubmit($("signUpForm"), async ({ username, email, password }) => {
    const r = await auth.signUp(username.trim(), email.trim(), password);
    $("signUpForm").reset();
    if (r.UserConfirmed) {
      $("signInForm").username.value = username.trim();
      showTab("signIn");
      authMsg("Account created. Sign in.");
    } else {
      $("confirmForm").username.value = username.trim();
      showTab("confirm");
      authMsg(`We sent a code to ${r.CodeDeliveryDetails?.Destination || email}.`);
    }
  });

  onSubmit($("confirmForm"), async ({ username, code }) => {
    await auth.confirmSignUp(username.trim(), code.trim());
    $("signInForm").username.value = username.trim();
    $("confirmForm").reset();
    showTab("signIn");
    authMsg("Confirmed. Sign in with your password.");
  });

  $("resendCode").addEventListener("click", async () => {
    const username = $("confirmForm").username.value.trim();
    if (!username) return authMsg("Enter your username first.", true);
    try {
      await auth.resendCode(username);
      authMsg("Sent a new code.");
    } catch (err) {
      authMsg(err.message, true);
    }
  });

  $("signOut").addEventListener("click", () => {
    socket.close();
    auth.signOut();
    room = null;
    showAuth("Signed out.");
  });
}

// ---- Chat view -------------------------------------------------------------

function enterChat() {
  const me = auth.currentUser();
  $("authView").hidden = true;
  $("chatView").hidden = false;
  $("whoami").textContent = me.username;
  $("whoami").hidden = false;
  $("signOut").hidden = false;
  renderRooms();
  const last = recentRooms()[0];
  if (last) joinRoom(last);
  socket.connect();
}

function wireChat() {
  $("roomForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("roomForm").roomId.value.trim().toLowerCase();
    if (!ROOM_RE.test(id)) return chatMsg("Room names are 1 to 64 lowercase letters, digits, or dashes.");
    $("roomForm").reset();
    joinRoom(id);
  });

  $("sendForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("sendForm").body;
    const body = input.value.trim();
    if (!body || !room) return;
    if (body.length > 2000) return chatMsg("Messages are at most 2000 characters.");
    if (!socket.send({ action: "sendMessage", roomId: room, body, clientSentAt: Date.now() })) {
      return chatMsg("Not connected. Your message wasn't sent.");
    }
    input.value = "";
  });

  $("loadOlder").addEventListener("click", () => {
    if (!nextBefore || loadingOlder) return;
    loadingOlder = true;
    socket.send({ action: "getHistory", roomId: room, before: nextBefore, limit: PAGE });
  });

  $("retry").addEventListener("click", () => {
    chatMsg("");
    socket.connect();
  });
}

function joinRoom(id) {
  room = id;
  messages = new Map();
  notices = [];
  nextBefore = null;
  loadingOlder = false;
  $("roomTitle").textContent = `# ${id}`;
  rememberRoom(id);
  renderRooms();
  renderMessages();
  updateComposer();
  socket.send({ action: "joinRoom", roomId: id }); // if not open yet, onState resends on connect
}

function onState(s) {
  state = s;
  $("status").textContent = s;
  $("status").dataset.state = s;
  $("retry").hidden = true;
  if (s === "connected") {
    chatMsg("");
    if (room) socket.send({ action: "joinRoom", roomId: room });
  } else if (s === "rejected") {
    chatMsg("The server rejected the connection (unauthorized).", true);
  } else if (s === "signed-out") {
    auth.signOut();
    showAuth("Your session expired. Sign in again.");
  }
  updateComposer();
}

function onFrame(f) {
  switch (f.type) {
    case "history": {
      if (f.roomId !== room) return;
      const older = loadingOlder;
      loadingOlder = false;
      if (!older) messages = new Map();
      for (const m of f.messages || []) messages.set(m.messageId, m);
      nextBefore = f.messages?.length ? f.nextBefore : null;
      renderMessages(older ? "keep" : "bottom");
      updateComposer();
      break;
    }
    case "message": {
      if (f.roomId !== room) return;
      const mine = f.userId === auth.currentUser()?.userId;
      if (mine && typeof f.clientSentAt === "number") {
        f.latencyMs = Date.now() - f.clientSentAt;
        recordLatency(f.latencyMs);
      }
      messages.set(f.messageId, f);
      renderMessages(mine || nearBottom() ? "bottom" : "keep-position");
      break;
    }
    case "presence": {
      if (f.roomId !== room || f.userId === auth.currentUser()?.userId) return;
      notices.push({ at: Date.now(), text: `${f.username} ${f.event}` });
      renderMessages(nearBottom() ? "bottom" : "keep-position");
      break;
    }
    case "error":
      loadingOlder = false;
      chatMsg(`${f.code}: ${f.message}`);
      if (f.code === "NOT_IN_ROOM" && room) socket.send({ action: "joinRoom", roomId: room });
      break;
  }
}

// ---- Rendering -------------------------------------------------------------

function chatMsg(text, retry = false) {
  $("chatMsgText").textContent = text;
  $("chatMsg").hidden = !text;
  $("retry").hidden = !retry;
}

function updateComposer() {
  const ready = state === "connected" && !!room;
  $("sendForm").body.disabled = !ready;
  $("sendForm").querySelector("button").disabled = !ready;
}

function nearBottom() {
  const el = $("messages");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

// scroll: "bottom" jumps to newest, "keep" holds the view steady after older
// messages are prepended, "keep-position" leaves scrollTop alone.
function renderMessages(scroll = "bottom") {
  const el = $("messages");
  const list = $("messageList");
  const prevHeight = el.scrollHeight;
  const me = auth.currentUser()?.userId;

  const items = [
    ...[...messages.values()].map((m) => ({ at: m.sentAt, m })),
    ...notices.map((n) => ({ at: n.at, n })),
  ].sort((a, b) => a.at - b.at);

  list.replaceChildren(
    ...items.map(({ m, n }) => {
      const li = document.createElement("li");
      if (n) {
        li.className = "notice";
        li.textContent = n.text;
        return li;
      }
      li.className = m.userId === me ? "msg mine" : "msg";
      const head = document.createElement("div");
      head.className = "meta";
      const who = document.createElement("strong");
      who.textContent = m.username;
      const when = document.createElement("time");
      when.textContent = new Date(m.sentAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      head.append(who, when);
      if (m.latencyMs !== undefined) {
        const lat = document.createElement("span");
        lat.className = m.latencyMs < 200 ? "lat ok" : "lat slow";
        lat.textContent = `${m.latencyMs} ms`;
        head.append(lat);
      }
      const body = document.createElement("p");
      body.textContent = m.body;
      li.append(head, body);
      return li;
    }),
  );

  $("loadOlder").hidden = !nextBefore;
  $("loadOlder").disabled = loadingOlder;
  if (scroll === "bottom") el.scrollTop = el.scrollHeight;
  else if (scroll === "keep") el.scrollTop += el.scrollHeight - prevHeight;
}

function recordLatency(ms) {
  latency.push(ms);
  if (latency.length > 500) latency.shift();
  const sorted = [...latency].sort((a, b) => a - b);
  const pct = (p) => sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)];
  const show = (id, v) => {
    $(id).textContent = `${v} ms`;
    $(id).className = v < 200 ? "ok" : "slow";
  };
  show("latLast", ms);
  show("latP50", pct(0.5));
  show("latP95", pct(0.95));
  $("latN").textContent = String(latency.length);
}

// ---- Recent rooms ----------------------------------------------------------

function recentRooms() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_KEY));
    if (Array.isArray(list) && list.length) return list.filter((r) => ROOM_RE.test(r));
  } catch {
    // fall through
  }
  return ["general"];
}

function rememberRoom(id) {
  const list = [id, ...recentRooms().filter((r) => r !== id)].slice(0, 8);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch {
    // Storage blocked: the list just won't survive a reload.
  }
}

function renderRooms() {
  $("roomList").replaceChildren(
    ...recentRooms().map((id) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.textContent = `# ${id}`;
      b.className = id === room ? "room-link active" : "room-link";
      b.addEventListener("click", () => id !== room && joinRoom(id));
      li.append(b);
      return li;
    }),
  );
}
