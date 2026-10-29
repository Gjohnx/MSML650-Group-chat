# CI/CD

Two GitHub Actions workflows live in `.github/workflows/`.

## CI (`ci.yml`): every pull request and every push to `main`

No AWS access needed. Three checks, all must pass before merging:

| Check | What it does |
|-------|--------------|
| Lint | `ruff check .`: real bugs only (unused names, undefined names, bugbear). Line length is not enforced. Config in `ruff.toml`. |
| Test | `pytest`: Lambda unit tests in `tests/test_lambdas.py`, plus the load and smoke scripts run against an in-process fake server (`tests/load/fake_server.py`). |
| Synth | `cdk synth -c owner=ci`: catches a broken CDK change before it reaches a deploy. |

Run the same thing locally before pushing:

```bash
pip install -r tests/requirements.txt
ruff check . && pytest -q
cd infra && cdk synth -c owner=ci -q
```

When you add logic to a Lambda, add its tests to `tests/test_lambdas.py` (or a
new `tests/test_<name>.py`). Stub boto3 or use `moto`; CI has no AWS credentials.

## CD (`deploy.yml`): every push to `main`, or run by hand

1. Runs CI and stops if it fails.
2. `cdk deploy -c owner=demo`: updates the shared demo stack. Personal stacks are never touched.
3. Publishes the web client to S3 and invalidates CloudFront, once `client/index.html` exists. It writes `client/config.json` from the stack outputs, so nobody copies URLs by hand.
4. Smoke check for criterion 1: no token and a forged token must both get HTTP 401.
5. Manual runs only: tick **Run the load test** to measure criteria 2 and 4. The summary shows loss and p50/p95 latency, and the full JSON report is attached to the run.

Results appear in the run's summary page under the Actions tab.

To run it by hand: Actions → Deploy demo stack → Run workflow. Once the
authorizer verifies JWTs, also tick **require a real Cognito token** so the
smoke check proves both halves of criterion 1.

## One-time setup (account admin)

The deploy job signs in to AWS through GitHub's OIDC provider, so no AWS keys
are stored in GitHub. Until this is done, CD skips the deploy with a notice and
CI still runs.

Team IAM users cannot create IAM resources (see `AWS_ACCESS.md`), so someone
with admin access does this once:

1. Deploy the role:

   ```bash
   aws cloudformation deploy --region us-east-1 \
     --stack-name GroupChat-github-deploy-role \
     --template-file infra/ci/github-deploy-role.yaml \
     --capabilities CAPABILITY_NAMED_IAM
   ```

   If the account already has a `token.actions.githubusercontent.com`
   identity provider, add `--parameter-overrides CreateOidcProvider=false`.

2. Copy the `RoleArn` output:

   ```bash
   aws cloudformation describe-stacks --region us-east-1 \
     --stack-name GroupChat-github-deploy-role \
     --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
   ```

3. In GitHub: Settings → Secrets and variables → Actions → New repository
   secret, named `AWS_DEPLOY_ROLE_ARN`, value the ARN.

4. In GitHub: Settings → Environments → `demo` (created by the first workflow
   run, or create it now) → Deployment branches → Selected branches → `main`.
   The role only trusts jobs running in this environment, so this keeps
   branches other than `main` from deploying.

The role can only assume the CDK bootstrap roles, read the `GroupChat-demo`
stack outputs, create Cognito test users, publish the client bucket, and
invalidate CloudFront.
