# Auto-stop (Lambda + EventBridge)

An hourly Lambda (on the hour in IST) that stops client environments that have been up too long. It does not depend on the control panel or Railway.

## Rules
* Per client in the client list: when **every running resource** (EC2 and RDS) has been up for **12 hours or more**, it stops the
  environment in the panel's safe order: applications, wait until they are stopped, then databases.
  (`StopRule=any` stops when at least one resource is over the limit instead.)
* Uptime is "since the last start": EC2 `LaunchTime`; RDS the newest `DB instance started` event (14-day RDS event limit, so the
  limit cannot exceed 336 hours).
* Safe by default: **dry run** unless you deploy with `--live`; a resource tagged `auto-stop=disabled` protects its whole client;
  an unknown uptime (for example a missing permission) means skip, never stop; if the applications do not stop within 5 minutes
  the databases are left running and an error is reported.
* One client failing never affects the others. An optional email reports every stop, would-be stop (dry run) and error.
* AWS restarts an RDS database that has been stopped for 7 days. That is an AWS rule, unrelated to this job.

## Deploy
1. Create (or pick) an S3 bucket in the same region for the Lambda zip: `aws s3 mb s3://<bucket> --region <region>`.
2. Check what would be deployed (touches nothing): `python autostop/deploy.py --bucket <bucket> --region ap-south-2 --clients clients.json --plan`
3. Deploy in dry-run mode: `python autostop/deploy.py --bucket <bucket> --region ap-south-2 --clients clients.json --profile <profile> --email you@life180labs.com`
4. Run it once by hand and read the log: `aws lambda invoke --function-name l180-auto-stop --payload "{}" --cli-binary-format raw-in-base64-out out.json` then CloudWatch log group `/aws/lambda/l180-auto-stop`
   (lines start with `auto-stop {...}`; `would_stop` is what live mode would do).
5. When a few runs look right, redeploy with `--live`. A manual live test: invoke with payload `{"dry_run": false}`.

Redeploying is the same command (it updates the stack). Other options: `--threshold`, `--rule any`, `--schedule "cron(0/15 * * * ? *)"`.

## Cost
Effectively $0 a month: about 730 runs a month sit inside the Lambda and EventBridge free tiers; only the logs cost anything (cents).

## Clients in other AWS accounts
Add `role_arn` (and `external_id`) to the client, as in the panel's `clients.json`. The Lambda role may assume
`arn:aws:iam::*:role/l180-control-panel`, and that role's policy already includes everything needed (describe, stop, `rds:DescribeEvents`).
Also allow the Lambda role in that role's trust policy.

## Tests
`pytest tests/test_autostop.py` (fake AWS via moto, including a full stack creation).
