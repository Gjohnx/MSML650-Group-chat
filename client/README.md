# client (owner: role 5)

Static web client: Cognito sign-in, room picker, chat window, and a latency
readout computed from the echoed `clientSentAt` field. Served from S3 behind
CloudFront. Reads `config.json` (written by the infra outputs, not committed).
