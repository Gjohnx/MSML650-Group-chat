# load tests (owner: role 6)

Opens N concurrent WebSocket clients, each authenticated with a real Cognito
token, has them join one room, and sends numbered messages. Asserts every
client received every sequence number (criterion 4) and reports p50/p95
broadcast latency (criterion 2). Also serves as the integration smoke test.
