#!/usr/bin/env python3
"""Publish the web client to a stack's S3 bucket and CloudFront. Owner: role 5b.

Writes client/config.json from the stack outputs, uploads the client files,
and invalidates the CloudFront cache so the new version shows up right away.
Run it from the infra venv (it has boto3).

    cd infra && source .venv/bin/activate && cd ..
    python tools/deploy_client.py --owner sai                  # config + upload
    python tools/deploy_client.py --owner sai --config-only    # just config.json, for the mock server
"""
import argparse
import json
import mimetypes
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import boto3

from get_token import stack_outputs

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
SKIP_DIRS = {"mock", "node_modules"}
SKIP_FILES = {"package.json", "package-lock.json", "README.md", "config.example.json", ".DS_Store"}


def write_config(outputs):
    config = {
        "wsUrl": outputs["WebSocketUrl"],
        "region": "us-east-1",
        "userPoolId": outputs["UserPoolId"],
        "appClientId": outputs["AppClientId"],
    }
    path = CLIENT_DIR / "config.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"wrote {path.relative_to(CLIENT_DIR.parent)}")


def client_files():
    for path in sorted(CLIENT_DIR.rglob("*")):
        rel = path.relative_to(CLIENT_DIR)
        if path.is_file() and rel.parts[0] not in SKIP_DIRS and path.name not in SKIP_FILES:
            yield path, rel.as_posix()


def upload(s3, bucket):
    count = 0
    for path, key in client_files():
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type, "CacheControl": "no-cache"})
        print(f"  {key}")
        count += 1
    print(f"uploaded {count} files to s3://{bucket}")


def invalidate(cloudfront, client_url):
    domain = urlparse(client_url).netloc
    for page in cloudfront.get_paginator("list_distributions").paginate():
        for dist in page.get("DistributionList", {}).get("Items", []):
            if dist["DomainName"] == domain:
                cloudfront.create_invalidation(
                    DistributionId=dist["Id"],
                    InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": str(time.time())},
                )
                print(f"invalidated CloudFront {dist['Id']} (takes a minute or two)")
                return
    print(f"warning: no CloudFront distribution found for {domain}; skipped invalidation", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", required=True, help="whose stack: GroupChat-<owner>")
    ap.add_argument("--profile", default="msml650", help="AWS CLI profile (default msml650)")
    ap.add_argument("--config-only", action="store_true", help="write client/config.json and stop")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    outputs = stack_outputs(session, args.owner)
    write_config(outputs)
    if args.config_only:
        return
    upload(session.client("s3"), outputs["ClientBucketName"])
    invalidate(session.client("cloudfront"), outputs["ClientUrl"])
    print(f"open {outputs['ClientUrl']}")


if __name__ == "__main__":
    main()
