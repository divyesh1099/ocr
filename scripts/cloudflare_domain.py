#!/usr/bin/env python3
"""Custom domain setup for the OCR site when DNS lives in Cloudflare.

  cert   Request (or reuse) an ACM certificate in us-east-1 for the domain, add the DNS
         validation CNAME in Cloudflare, wait until it is issued, print the ARN.
  dns    Point the domain (CNAME, DNS-only) at the CloudFront distribution, read from the
         stack's `CloudFrontDomain` output or passed with --target.

Needs: AWS credentials, and CLOUDFLARE_API_TOKEN with "Zone > DNS > Edit" on the zone.

  python scripts/cloudflare_domain.py cert --domain ocr.dshare.me
  python scripts/cloudflare_domain.py dns  --domain ocr.dshare.me --stack Ocr --region ap-south-1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

CF_API = "https://api.cloudflare.com/client/v4"


# ---------------------------------------------------------------- Cloudflare
def cf(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        sys.exit("Set CLOUDFLARE_API_TOKEN (Zone > DNS > Edit permission for your zone)")
    req = urllib.request.Request(
        CF_API + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https endpoint
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"Cloudflare API {method} {path} failed: {exc.code} {exc.read()[:500]!r}")
    if not data.get("success"):
        sys.exit(f"Cloudflare API error: {data.get('errors')}")
    return data


def zone_id(domain: str, zone: str | None) -> str:
    labels = domain.rstrip(".").split(".")
    candidates = [zone] if zone else [".".join(labels[i:]) for i in range(len(labels) - 1)]
    for name in candidates:
        res = cf("GET", f"/zones?name={urllib.parse.quote(name)}")["result"]
        if res:
            return res[0]["id"]
    sys.exit(f"No Cloudflare zone found for {domain} (tried {candidates})")


def upsert_cname(zid: str, name: str, target: str, proxied: bool = False) -> None:
    name, target = name.rstrip("."), target.rstrip(".")
    existing = cf("GET", f"/zones/{zid}/dns_records?name={urllib.parse.quote(name)}")["result"]
    body = {"type": "CNAME", "name": name, "content": target, "ttl": 300 if not proxied else 1, "proxied": proxied,
            "comment": "managed by ocr/scripts/cloudflare_domain.py"}
    conflicting = [r for r in existing if r["type"] != "CNAME"]
    if conflicting:
        sys.exit(f"{name} already has non-CNAME records {[(r['type'], r['content']) for r in conflicting]}; "
                 "remove them first")
    if existing:
        rec = existing[0]
        if rec["content"] == target and rec.get("proxied") == proxied:
            print(f"CNAME {name} -> {target} already in place")
            return
        cf("PUT", f"/zones/{zid}/dns_records/{rec['id']}", body)
        print(f"updated CNAME {name} -> {target}")
    else:
        cf("POST", f"/zones/{zid}/dns_records", body)
        print(f"created CNAME {name} -> {target}")


# ---------------------------------------------------------------- commands
def cmd_cert(args) -> None:
    acm = boto3.client("acm", region_name="us-east-1")  # CloudFront only uses us-east-1 certificates
    arn = None
    for page in acm.get_paginator("list_certificates").paginate(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]):
        for c in page["CertificateSummaryList"]:
            if c["DomainName"] == args.domain:
                arn = c["CertificateArn"]
                break
    if not arn:
        arn = acm.request_certificate(DomainName=args.domain, ValidationMethod="DNS",
                                      IdempotencyToken=args.domain.replace(".", "")[:32],
                                      Options={"CertificateTransparencyLoggingPreference": "ENABLED"},
                                      Tags=[{"Key": "app", "Value": "ocr"}])["CertificateArn"]
        print(f"requested certificate {arn}")
    else:
        print(f"reusing certificate {arn}")

    record = None
    for _ in range(30):  # the validation record shows up a few seconds after the request
        cert = acm.describe_certificate(CertificateArn=arn)["Certificate"]
        if cert["Status"] == "ISSUED":
            break
        opts = cert.get("DomainValidationOptions", [])
        if opts and opts[0].get("ResourceRecord"):
            record = opts[0]["ResourceRecord"]
            break
        time.sleep(2)
    if record:
        upsert_cname(zone_id(args.domain, args.zone), record["Name"], record["Value"], proxied=False)
        print("waiting for ACM to validate (usually 1-5 minutes)...")
        acm.get_waiter("certificate_validated").wait(CertificateArn=arn, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
    print(f"certificate issued: {arn}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"certificate_arn={arn}\n")


def cmd_dns(args) -> None:
    target = args.target
    if not target:
        cfn = boto3.client("cloudformation", region_name=args.region)
        outputs = cfn.describe_stacks(StackName=args.stack)["Stacks"][0].get("Outputs", [])
        target = next((o["OutputValue"] for o in outputs if o["OutputKey"] == "CloudFrontDomain"), None)
        if not target:
            sys.exit(f"Stack {args.stack} has no CloudFrontDomain output yet; deploy it first")
    upsert_cname(zone_id(args.domain, args.zone), args.domain, target, proxied=args.proxied)
    print(f"https://{args.domain} will serve the site once DNS propagates (usually under a minute on Cloudflare)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cert", help="request/validate the ACM certificate via Cloudflare DNS")
    c.add_argument("--domain", required=True)
    c.add_argument("--zone", help="Cloudflare zone name (default: derived from the domain, e.g. dshare.me)")
    c.set_defaults(func=cmd_cert)
    d = sub.add_parser("dns", help="point the domain at CloudFront")
    d.add_argument("--domain", required=True)
    d.add_argument("--zone")
    d.add_argument("--target", help="CloudFront domain (default: read from the stack output)")
    d.add_argument("--stack", default="Ocr")
    d.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    d.add_argument("--proxied", action="store_true",
                   help="route through Cloudflare's proxy (orange cloud). Default is DNS-only, recommended "
                        "because CloudFront already terminates TLS and caches")
    d.set_defaults(func=cmd_dns)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
