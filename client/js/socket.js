// WebSocket connection to the chat API. Owner: role 5b.
//
// Browsers can't set headers on the handshake, so the ID token rides in
// ?token= (docs/CONTRACTS.md section 1). A browser also never sees the 401/403
// from a rejected handshake; it just gets a close before open. We treat that
// as "rejected", retry once with a fresh token, then stop.
//
// API Gateway drops idle sockets after 10 minutes and all sockets after 2
// hours, so any close after a successful open reconnects with backoff.

export class ChatSocket {
  constructor({ url, getToken, onFrame, onState }) {
    this.url = url;
    this.getToken = getToken;
    this.onFrame = onFrame;
    this.onState = onState;
    this.ws = null;
    this.stopped = false;
    this.retries = 0;
    this.rejections = 0;
  }

  async connect() {
    this.stopped = false;
    const token = await this.getToken();
    if (!token) return this._state("signed-out");
    this._state(this.retries ? "reconnecting" : "connecting");

    const ws = new WebSocket(`${this.url}?token=${encodeURIComponent(token)}`);
    let opened = false;
    this.ws = ws;

    ws.onopen = () => {
      opened = true;
      this.retries = 0;
      this.rejections = 0;
      this._state("connected");
    };
    ws.onmessage = (e) => {
      let frame;
      try {
        frame = JSON.parse(e.data);
      } catch {
        return;
      }
      this.onFrame(frame);
    };
    ws.onclose = () => {
      if (this.ws !== ws || this.stopped) return;
      this.ws = null;
      if (!opened && ++this.rejections >= 2) return this._state("rejected");
      const delay = Math.min(30_000, 500 * 2 ** this.retries++);
      this._state("reconnecting");
      setTimeout(() => this.stopped || this.connect(), delay);
    };
  }

  send(frame) {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(frame));
    return true;
  }

  close() {
    this.stopped = true;
    this.ws?.close();
    this.ws = null;
    this._state("offline");
  }

  _state(s) {
    this.onState(s);
  }
}
