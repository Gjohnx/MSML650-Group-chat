# infra (owner: role 5)

AWS CDK app in Python that deploys the whole stack in one command:

- Cognito user pool + app client
- API Gateway WebSocket API with the routes in `docs/CONTRACTS.md`
- One Lambda per folder under `lambdas/`
- DynamoDB `Connections` (with `roomId-index` GSI and TTL on `expiresAt`) and `Messages`
- S3 bucket + CloudFront for the client

## First time

```bash
cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install -g aws-cdk
cdk bootstrap --profile msml650      # done once by role 5, do not rerun
cdk deploy --profile msml650 -c owner=<yourname>
```

Every teammate deploys their own copy, named `GroupChat-<yourname>`, so you
can break things freely. `owner=demo` is the shared stack and only role 5
deploys it. Region is pinned to us-east-1. Tear yours down with
`cdk destroy --profile msml650 -c owner=<yourname>`.

Check it synthesizes before pushing:

```bash
cdk synth --profile msml650 -c owner=<yourname> -q
```

Outputs print the WebSocket URL, user pool id, and app client id, which the
client reads from `client/config.json`.
