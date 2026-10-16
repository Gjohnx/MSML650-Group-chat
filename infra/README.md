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
cdk bootstrap          # once per account/region
cdk deploy
```

Outputs print the WebSocket URL, user pool id, and app client id, which the
client reads from `client/config.json`.
