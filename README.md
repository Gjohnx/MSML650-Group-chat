# MSML650-Group-chat

MSML 650 Cloud Computing, Fall 2026, Team 2.

An event-driven, serverless group chat platform on AWS: Cognito for
authentication, API Gateway WebSocket APIs for bidirectional messaging, Lambda
for message handling, and DynamoDB for connection tracking and chat history.

## Success criteria

1. Unauthorized connections are rejected
2. Under 200 ms end-to-end message broadcast latency
3. Stale connections are reliably cleaned up
4. Concurrent user load is sustained without message loss
5. Persisted chat history is returned when a user joins a room

## Layout

| Path | Owner | What lives here |
|------|-------|-----------------|
| `docs/CONTRACTS.md` | everyone | Frame shapes, table schemas, routes. Read this first. |
| `lambdas/authorizer/` | role 1 | Cognito JWT check on `$connect` |
| `lambdas/connect/`, `lambdas/disconnect/`, `lambdas/join_room/` | role 2 | Connection lifecycle and stale cleanup |
| `lambdas/send_message/` | role 3 | Persist and fan out |
| `lambdas/get_history/` | role 4 | Paginated history |
| `infra/` | role 5 | CDK app that deploys everything |
| `client/` | role 5 | Static web client |
| `tests/load/` | role 6 | Concurrency and latency tests |

## Roles

| # | Area | Criterion | Who |
|---|------|-----------|-----|
| 1 | Auth (Cognito + authorizer) | 1 | |
| 2 | WebSocket API + connection lifecycle | 3 | |
| 3 | Message broadcast | 2 | |
| 4 | Persistence + history | 5 | |
| 5 | Client + infrastructure as code | demo | Ryan |
| 6 | Testing, load, status update, slides | 6 |Jianjiong Xiang|

## Working agreement

- Branch per piece, pull request into `main`, one teammate reviews.
- Change `docs/CONTRACTS.md` by pull request before changing code that depends on it.
- Never commit `.env`, `config.json`, or keys. See `.gitignore`.

## Deliverables

| When | What |
|------|------|
| Week 4 | Topic summary (submitted) |
| Week 10 | Status update |
| Before Week 15 | Slides |
| Week 15 | 15 minute presentation with live demo, peer evaluations |
