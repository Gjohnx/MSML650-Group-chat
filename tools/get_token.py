#!/usr/bin/env python3
"""Create a Cognito test user and print an ID token for the WebSocket API.

Reads the user pool and app client from your deployed stack's outputs, so you
only say whose stack to use. Run it from the infra venv (it has boto3).

    cd infra && source .venv/bin/activate && cd ..
    python tools/get_token.py --owner ryan --username alice --create
    python tools/get_token.py --owner ryan --username alice          # later runs

Prints the ID token, plus the ready-to-use connect URL. Tokens expire after an
hour; just run it again. Use --json to get everything as one JSON object for
scripts (role 6's load test can call this per simulated user).
"""
import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_PASSWORD = "Chat-test-1234"


def stack_outputs(session, owner):
    cfn = session.client("cloudformation")
    try:
        stack = cfn.describe_stacks(StackName=f"GroupChat-{owner}")["Stacks"][0]
    except ClientError as e:
        sys.exit(f"Could not read stack GroupChat-{owner}: {e.response['Error']['Message']}")
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def ensure_user(idp, pool_id, username, password):
    try:
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": f"{username}@example.com"},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        created = True
    except idp.exceptions.UsernameExistsException:
        created = False
    idp.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
    return created


def get_id_token(idp, client_id, username, password):
    resp = idp.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["IdToken"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", default="demo", help="whose stack: GroupChat-<owner> (default demo)")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", default=DEFAULT_PASSWORD, help=f"default {DEFAULT_PASSWORD}")
    ap.add_argument("--create", action="store_true", help="create the user if missing and set the password")
    ap.add_argument("--profile", default="msml650", help="AWS CLI profile (default msml650)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    outputs = stack_outputs(session, args.owner)
    idp = session.client("cognito-idp")

    created = ensure_user(idp, outputs["UserPoolId"], args.username, args.password) if args.create else None
    try:
        token = get_id_token(idp, outputs["AppClientId"], args.username, args.password)
    except ClientError as e:
        sys.exit(f"Sign-in failed for {args.username}: {e.response['Error']['Message']}. "
                 "Add --create the first time, or check the password.")

    connect_url = f"{outputs['WebSocketUrl']}?token={token}"
    if args.json:
        print(json.dumps({"username": args.username, "idToken": token, "wsUrl": outputs["WebSocketUrl"],
                          "connectUrl": connect_url, "created": created}))
    else:
        if created is True:
            print(f"created user {args.username}", file=sys.stderr)
        print(f"ID token for {args.username} (expires in 1 hour):\n{token}\n")
        print(f"Connect with:\nwscat -c '{connect_url}'")


if __name__ == "__main__":
    main()
