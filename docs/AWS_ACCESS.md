# AWS account and access

Set up 2026-10-22 by Ryan (role 5). One shared account, one region, one IAM
user per teammate, one stack per teammate.

## Account

| Item | Value |
|------|-------|
| Account | Ryan's personal AWS account, alias `msml650-team2` |
| Region | `us-east-1` only. Pinned in `infra/app.py`. |
| Console sign-in | https://msml650-team2.signin.aws.amazon.com/console |
| Root user | Ryan only. Passkey MFA plus authenticator app. Never shared. |
| Budget alarms | Email to Ryan at $10 and $25 per month |
| CDK bootstrap | Done once for `us-east-1`. Do not rerun. |

## Your IAM user

Each teammate has an IAM user named with their lowercase first name
(`dang`, `aaryan`, `abhiram`, `john`, `sai`, `ryan`), in the group
`msml650-team`. You received privately:

- the sign-in link above, your username, and a temporary password
- an access key ID and secret for the command line

Never paste keys into the group chat, a commit, or a `.env` that is not
gitignored. If a key leaks, tell Ryan and it gets deleted and reissued.

## What the team policy allows

The group carries `MSML650TeamDeveloper`, which allows full use of every
service in our stack and lets CDK assume its deploy roles:

Lambda, API Gateway, DynamoDB, Cognito, S3, CloudFront, CloudFormation,
CloudWatch, Logs, EventBridge, X-Ray, SSM parameter reads, and IAM read plus
PassRole.

It explicitly denies: billing, account settings, creating or editing IAM users
and their keys, turning off MFA, and launching EC2 instances. If you hit
"not authorized" on something you genuinely need, ask Ryan rather than
working around it.

`ryan` additionally has `AdministratorAccess` for bootstrap and IAM work.

## First time on your laptop

```bash
aws configure --profile msml650
# access key ID, secret, region us-east-1, output json
aws sts get-caller-identity --profile msml650   # should print your username

cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install -g aws-cdk
cdk synth --profile msml650 -c owner=<yourname> -q
cdk deploy --profile msml650 -c owner=<yourname>
```

## Stacks

| Stack | Who deploys | Purpose |
|-------|-------------|---------|
| `GroupChat-<yourname>` | you | Your own copy. Break it freely. |
| `GroupChat-demo` | Ryan | The one we present. Deployed from `main` only. |

Tear yours down when you are done for the week to keep the console tidy:
`cdk destroy --profile msml650 -c owner=<yourname>`. The stack is serverless,
so an idle one costs nothing, but a load test against it does.

## Cost rules

- No EC2. The policy blocks it anyway.
- Load tests (role 6) run against `GroupChat-demo` or the tester's own stack,
  never against someone else's, and at agreed times.
- Delete CloudWatch log groups older than the project when the course ends.

## After Week 15

Ryan deletes every access key, removes the IAM users, and destroys all stacks.
