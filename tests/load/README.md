# load tests (owner: role 6)

Opens N concurrent WebSocket clients, each authenticated with a real Cognito
token, has them join one room, and sends numbered messages. Asserts every
client received every sequence number (criterion 4) and reports p50/p95
broadcast latency (criterion 2). Also serves as the integration smoke test.

## How it measures

- **Criterion 4:** every client must receive every message exactly once. Lost
  and duplicate deliveries are counted per client.
- **Criterion 2:** each delivery is timed from the sender's `clientSentAt` to
  arrival at the receiver. All clients run in one process, so both ends use the
  same clock. The run fails if p95 is over 200 ms (`--max-p95-ms`).

`load_test.py` exits 0 on pass and 1 on fail.

## Run it against your stack

```bash
cd infra && source .venv/bin/activate && cd ..
pip install -r tests/load/requirements.txt
python tests/load/load_test.py --owner <yourname> --clients 20 --messages 10 --create
```

`--create` makes Cognito users `loadtest-0` to `loadtest-N-1` the first time;
leave it off on later runs. Each run uses a fresh room so old history does not
count. Add `--json` for the full per-client report.

It needs `joinRoom` and `sendMessage` to work, so until roles 2 and 3 land
their handlers it will report that no broadcasts were received.

## Run it without AWS

`fake_server.py` speaks the same frames as `docs/CONTRACTS.md`. CI uses it to
check that the harness measures loss and latency correctly.

```bash
python tests/load/fake_server.py --port 8765 &
python tests/load/load_test.py --url ws://127.0.0.1:8765 --clients 20
python tests/load/fake_server.py --port 8766 --drop-every 50 &   # should FAIL
python tests/load/load_test.py --url ws://127.0.0.1:8766 --clients 10
```

## Criterion 1 smoke check

`tests/smoke/check_auth.py --owner <name>` expects no token and a forged token
to be refused. Add `--valid-token` once the authorizer verifies JWTs, to also
require a real token to connect.
