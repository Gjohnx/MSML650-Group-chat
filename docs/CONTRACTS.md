# Interface contracts

These are the only things that cross between the six workstreams. Change them
here first, in a pull request, before changing code that depends on them.

## 1. WebSocket routes

Clients connect to the API Gateway WebSocket URL. Browsers cannot set headers
on a WebSocket handshake, so the Cognito ID token travels as a query parameter.

| Route         | Direction        | Owner | Purpose |
|---------------|------------------|-------|---------|
| `$connect`    | client -> server | 1, 2  | Authorizer validates `?token=<ID token>`; on success the connect Lambda writes a row to `Connections` |
| `$disconnect` | client -> server | 2     | Delete the `Connections` row |
| `joinRoom`    | client -> server | 2, 4  | Set `roomId` on the connection row, then reply with a `history` frame |
| `sendMessage` | client -> server | 3, 4  | Persist to `Messages`, then fan out a `message` frame to every connection in the room |
| `getHistory`  | client -> server | 4     | Paginate older messages |

Route selection expression: `$request.body.action`.

## 2. Client -> server frames

`userId` and `username` are never sent by the client. They come from the
authorizer context so a client cannot impersonate another user.

```json
{ "action": "joinRoom",    "roomId": "general" }
{ "action": "sendMessage", "roomId": "general", "body": "hello", "clientSentAt": 1760630400123 }
{ "action": "getHistory",  "roomId": "general", "before": "1760630400123#01J...", "limit": 50 }
```

- `roomId`: 1 to 64 chars, `[a-z0-9-]`.
- `body`: 1 to 2000 chars after trimming.
- `clientSentAt`: epoch milliseconds from the sender's clock. Optional. Echoed
  back unchanged so the sender can measure round trip latency.
- `before`: the `sortKey` of the oldest message the client already has. Omit for
  the newest page. `limit` defaults to 50, max 100.

## 3. Server -> client frames

```json
{ "type": "message", "roomId": "general", "messageId": "01J...", "userId": "...",
  "username": "ryan", "body": "hello", "sentAt": 1760630400150, "clientSentAt": 1760630400123 }

{ "type": "history", "roomId": "general", "messages": [ <message frames, oldest first> ],
  "nextBefore": "1760630300000#01J..." }

{ "type": "presence", "roomId": "general", "userId": "...", "username": "ryan", "event": "joined" }

{ "type": "error", "code": "BAD_REQUEST", "message": "body is required" }
```

- `sentAt` is set by the `sendMessage` Lambda (server clock), not the client.
- `nextBefore` is `null` when there are no older messages.
- Error codes: `UNAUTHORIZED`, `BAD_REQUEST`, `NOT_IN_ROOM`, `INTERNAL`.

## 4. DynamoDB tables

### `Connections`

| Attribute      | Type | Notes |
|----------------|------|-------|
| `connectionId` | S    | Partition key. From API Gateway. |
| `userId`       | S    | Cognito `sub` |
| `username`     | S    | Cognito `cognito:username` |
| `roomId`       | S    | Empty until `joinRoom` |
| `connectedAt`  | N    | Epoch ms |
| `expiresAt`    | N    | Epoch **seconds**. TTL attribute, set to connectedAt + 2 hours. Backstop for stale sockets. |

Global secondary index `roomId-index`: partition key `roomId`. Used by
`sendMessage` to find every socket in a room.

Stale cleanup rule (owner 2): when `post_to_connection` raises
`GoneException`, delete that row immediately. TTL catches anything missed.

### `Messages`

| Attribute      | Type | Notes |
|----------------|------|-------|
| `roomId`       | S    | Partition key |
| `sortKey`      | S    | Sort key: `"<sentAt>#<messageId>"`. The ms timestamp alone can collide under load. |
| `messageId`    | S    | ULID, generated server side |
| `userId`       | S    | |
| `username`     | S    | |
| `body`         | S    | |
| `sentAt`       | N    | Epoch ms, server clock |
| `clientSentAt` | N    | Optional, echoed from the client |

Querying newest first: `KeyConditionExpression roomId = :r`, `ScanIndexForward=False`,
`Limit=limit`, and `sortKey < :before` when paginating.

## 5. Authorizer context

The `$connect` REQUEST authorizer (owner 1) must return this context on success.
Every other Lambda reads it from `event.requestContext.authorizer`.

```json
{ "userId": "<cognito sub>", "username": "<cognito:username>" }
```

On failure it denies the policy, which makes API Gateway return 401 and the
socket never opens. That is success criterion 1.

## 6. Environment variables every Lambda receives

| Name                 | Value |
|----------------------|-------|
| `CONNECTIONS_TABLE`  | table name |
| `MESSAGES_TABLE`     | table name |
| `WS_ENDPOINT`        | `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>` for the management API |

## 7. Success criteria and who proves them

| # | Criterion | Owner | How it is shown in the demo |
|---|-----------|-------|-----------------------------|
| 1 | Unauthorized connections rejected | 1 | `wscat` with no token or a bad token gets 401 |
| 2 | Under 200 ms broadcast latency | 3 | Client shows `now - clientSentAt` on echoed messages; load script reports p50/p95 |
| 3 | Stale connections cleaned up | 2 | Kill a client without `$disconnect`; next broadcast removes the row; TTL shown in console |
| 4 | Concurrent load without message loss | 6 | Load script opens N clients, sends numbered messages, asserts every client got every number |
| 5 | History retrieved on join | 4 | Join a room with existing messages; `history` frame arrives before any new `message` |
