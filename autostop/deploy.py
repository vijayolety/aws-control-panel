"""Deploy (or update) the auto-stop stack: zips the Lambda, uploads it to S3 and creates/updates the CloudFormation stack.

    python autostop/deploy.py --bucket my-bucket --clients clients.json --region ap-south-2 [--profile uni_test_app]
                              [--email you@life180labs.com] [--threshold 10] [--live]

It deploys in DRY-RUN mode unless you pass --live. Run it with --plan first to see what would be sent without touching AWS.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
import handler  # noqa: E402  (validates the client list exactly as the Lambda will)

STACK = "l180-auto-stop"
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
MAX_EXTRA_EMAILS = 4


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("handler.py", date_time=(2020, 1, 1, 0, 0, 0))      # fixed timestamp: same code, same hash
        info.external_attr = 0o644 << 16
        z.writestr(info, (HERE / "src" / "handler.py").read_bytes())
    return buf.getvalue()


def compact_clients(path: Path) -> str:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # keep only what the Lambda needs, so the value stays under CloudFormation's 4096-character parameter limit
    keep = [{k: c[k] for k in ("slug", "name", "region", "role_arn", "external_id", "ec2_instances", "rds_instances") if c.get(k)}
            for c in raw["clients"]]
    for c in keep:
        for key in ("ec2_instances", "rds_instances"):
            if key in c:
                c[key] = [{k: x[k] for k in ("id", "name") if x.get(k)} if isinstance(x, dict) else {"id": x} for x in c[key]]
    text = json.dumps({"clients": keep}, separators=(",", ":"))
    handler.parse_clients(text)                      # raises ConfigError if the Lambda would refuse it
    if len(text) > 4096:
        raise SystemExit(f"the client list is {len(text)} characters; CloudFormation allows 4096. Trim it or move it to SSM.")
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", required=True, help="existing S3 bucket in the same region, for the Lambda zip")
    ap.add_argument("--clients", default="clients.json", type=Path)
    ap.add_argument("--region", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--stack", default=STACK)
    ap.add_argument("--threshold", default="10")
    ap.add_argument("--rule", default="all", choices=["all", "any"])
    ap.add_argument("--schedule", default="cron(30 * * * ? *)", help="UTC; the default fires at :00 IST, every hour")
    ap.add_argument("--email", default=None, help="primary notification address (kept from the last deploy when omitted)")
    ap.add_argument("--also-notify", action="append", default=None, metavar="EMAIL",
                    help=f"extra notification address, repeatable, up to {MAX_EXTRA_EMAILS} (kept from the last deploy when omitted)")
    ap.add_argument("--live", action="store_true", help="actually stop things (default is dry run)")
    ap.add_argument("--plan", action="store_true", help="print what would be deployed and exit")
    a = ap.parse_args(argv)

    for addr in ([a.email] if a.email else []) + (a.also_notify or []):
        if not EMAIL_RE.match(addr):
            raise SystemExit(f"{addr!r} is not an email address")
    extras = list(dict.fromkeys(e.strip() for e in (a.also_notify or []) if e.strip() and e.strip() != a.email))
    if len(extras) > MAX_EXTRA_EMAILS:
        raise SystemExit(f"at most {MAX_EXTRA_EMAILS} extra addresses (plus --email)")
    clients = compact_clients(a.clients)
    code = build_zip()
    key = f"l180-auto-stop/{hashlib.sha256(code).hexdigest()[:16]}.zip"
    params = {"ClientsJson": clients, "ThresholdHours": a.threshold, "DryRun": "false" if a.live else "true", "StopRule": a.rule,
              "Schedule": a.schedule, "CodeBucket": a.bucket, "CodeKey": key}
    print(f"stack {a.stack} in {a.region}: {'LIVE (will stop things)' if a.live else 'dry run (only logs)'}, limit {a.threshold}h, rule {a.rule}")
    print(f"clients: {', '.join(c['slug'] for c in handler.parse_clients(clients))}; code s3://{a.bucket}/{key} ({len(code)} bytes)")
    notify_line = ", ".join(([a.email] if a.email else []) + extras) or "(unchanged from the last deploy)"
    print(f"notifications: {notify_line}")
    if a.plan:
        return 0

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    session.client("s3").put_object(Bucket=a.bucket, Key=key, Body=code)
    cf = session.client("cloudformation")
    body = (HERE / "template.yaml").read_text(encoding="utf-8")
    try:
        previous = {x["ParameterKey"]: x.get("ParameterValue", "") for x in cf.describe_stacks(StackName=a.stack)["Stacks"][0]["Parameters"]}
        exists = True
    except ClientError:
        previous, exists = {}, False
    primary = a.email if a.email is not None else previous.get("NotificationEmail", "")
    extras = [e for e in extras if e != primary]                  # the primary address is already subscribed
    plist = [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]
    # addresses not mentioned on the command line keep their value from the last deploy, so a redeploy never drops a recipient
    for key_, given in (("NotificationEmail", a.email), ("AdditionalEmails", ",".join(extras) if a.also_notify is not None else None)):
        if given is not None:
            plist.append({"ParameterKey": key_, "ParameterValue": given})
        elif exists:
            plist.append({"ParameterKey": key_, "UsePreviousValue": True})
        else:
            plist.append({"ParameterKey": key_, "ParameterValue": ""})
    common = dict(StackName=a.stack, TemplateBody=body, Parameters=plist, Capabilities=["CAPABILITY_NAMED_IAM"])
    try:
        (cf.update_stack if exists else cf.create_stack)(**common)
    except ClientError as e:
        if "No updates are to be performed" in str(e):
            print("stack is already up to date")
            return 0
        raise
    waiter = cf.get_waiter("stack_update_complete" if exists else "stack_create_complete")
    print("waiting for CloudFormation ...")
    waiter.wait(StackName=a.stack, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    out = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=a.stack)["Stacks"][0].get("Outputs", [])}
    print("done:", json.dumps(out))
    if a.email or extras:
        print("each new address must confirm the SNS subscription email, or it will not receive notifications")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
