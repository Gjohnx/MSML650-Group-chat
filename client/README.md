# client (owner: role 5b)

Static web client: Cognito sign-in, room picker, chat window, and a latency
readout computed from the echoed `clientSentAt` field. Served from S3 behind
CloudFront. Reads `config.json` (written from the stack outputs, not committed).

No framework and no build step: plain HTML, CSS, and ES modules.

| File | What it does |
|------|--------------|
| `index.html`, `styles.css` | The page |
| `js/auth.js` | Sign up, confirm, sign in, token refresh, straight against the Cognito API |
| `js/socket.js` | WebSocket with `?token=`, reconnect with backoff, "rejected" detection |
| `js/app.js` | Rooms, history, load older, presence, errors, latency p50/p95 |
| `mock/server.mjs` | Local stand-in for the WebSocket API (dev only, never uploaded) |

## Run it locally against the mock API

Until the Lambdas are done, the mock speaks the frames in `docs/CONTRACTS.md`
with in-memory rooms. `#general` starts with 120 seeded messages so history
and "load older" have something to page through. Sign-in is still real
Cognito, so you need your own stack deployed.

```bash
cd infra && source .venv/bin/activate && cd ..
python tools/deploy_client.py --owner <yourname> --config-only   # writes client/config.json
python tools/get_token.py --owner <yourname> --username alice --create
python tools/get_token.py --owner <yourname> --username bob --create

cd client && npm install && npm run mock     # http://localhost:8080
```

Sign in as `alice` in one window and `bob` in a private window (password
`Chat-test-1234`). `MOCK_DELAY_MS=40 npm run mock` adds artificial delay so the
latency readout has something to show.

The mock decodes tokens but does not verify them. Real verification is the
authorizer's job (role 1).

## Deploy to your stack

```bash
python tools/deploy_client.py --owner <yourname>
```

Writes `config.json`, uploads the client to the stack's bucket, and invalidates
CloudFront. It prints the site URL at the end.

Until the authorizer verifies JWTs, the real API rejects every connection, so
the deployed client shows "rejected" after sign-in. That is expected.
