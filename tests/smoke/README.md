# criterion checks (owner: role 6)

One script per success criterion that is not covered by the load test. Each
prints PASS or FAIL per step and exits 0 only when everything passes, so they
work by hand, in CI, and as the live-demo script.

| Script | Criterion | What it proves |
|--------|-----------|----------------|
| `check_auth.py` | 1 | No token and a forged token are refused; `--valid-token` also requires a real token to connect |
| `check_stale.py` | 3 | Killed clients' rows leave the Connections table, survivors keep receiving, TTL is set as a backstop |
| `check_history.py` | 5 | History arrives on join before any live message, pages back in order, ends with `nextBefore: null` |

Criteria 2 and 4 are measured by `tests/load/load_test.py`.

## Against a deployed stack

```bash
cd infra && source .venv/bin/activate && cd ..
pip install -r tests/requirements.txt
python tests/smoke/check_auth.py    --owner <yourname> --valid-token
python tests/smoke/check_stale.py   --owner <yourname> --create
python tests/smoke/check_history.py --owner <yourname> --create
```

`--create` makes the Cognito test users the first time. `check_stale.py` and
`check_history.py` read the stack's DynamoDB tables, so they need the
`msml650` profile. If `sendMessage` is not ready yet but `joinRoom` and
`getHistory` are, `check_history.py --seed-via dynamo` writes the seed
messages straight into the Messages table.

## Against Sai's mock or the Python fake server

```bash
cd client && npm install && npm run mock       # ws://localhost:8080
python tests/smoke/check_history.py --url ws://localhost:8080 --mock-tokens
python tests/smoke/check_stale.py   --url ws://localhost:8080 --mock-tokens

python tests/load/fake_server.py --port 8765   # no Node needed
python tests/smoke/check_history.py --url ws://127.0.0.1:8765
```

Without DynamoDB access, `check_stale.py` checks only that survivors keep
receiving; row cleanup and TTL need `--owner`.

## Things the checks report as notes, not failures

- Messages sent in the same millisecond: `sortKey` is `<sentAt>#<messageId>`,
  so their order follows the message id, not the send order. The contract only
  orders by time.
- An empty last history page: with exactly `limit` messages left, DynamoDB can
  still return `LastEvaluatedKey`, so the client makes one extra round trip.
