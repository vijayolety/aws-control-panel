"""Auto-stop: an hourly Lambda that stops client environments that have been up for too long.

Runs from EventBridge. For each client in CLIENTS_JSON (same shape as the control panel's clients.json) it looks at the running
EC2 instances and available RDS databases. When every running resource has been up for THRESHOLD_HOURS or more, it stops the
environment in the panel's safe order: applications first, wait until they are stopped, then databases.

Safety rules (each one errs on the side of NOT stopping):
  * DRY_RUN defaults to "true": it only logs what it would stop.
  * A resource tagged auto-stop=disabled exempts the whole client.
  * If an uptime cannot be determined (for example a missing permission), the client is skipped, never stopped blindly.
  * If the applications do not reach "stopped" in time, the databases are left running and the failure is reported.

Uptime: EC2 uses LaunchTime (AWS resets it on every start). RDS has no start time on the instance, so it uses the newest
"DB instance started" event (or "DB instance created" if it was never stopped). RDS keeps events for 14 days; a database with
no event in that window has been up longer than that, which is more than any allowed threshold.

Standalone on purpose: it imports nothing from the control panel, because it is deployed to AWS on its own.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

RDS_EVENT_WINDOW_DAYS = 14
MAX_THRESHOLD_HOURS = RDS_EVENT_WINDOW_DAYS * 24
EXEMPT_TAG, EXEMPT_VALUE = "auto-stop", "disabled"
ROLE_SESSION = "l180-auto-stop"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    clients: list[dict]
    threshold_hours: float = 10.0
    dry_run: bool = True
    rule: str = "all"                    # "all": every running resource is over the limit; "any": at least one is
    sns_topic_arn: str = ""
    wait_seconds: int = 300              # how long to wait for applications to stop before leaving the databases alone
    poll_seconds: int = 10


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def parse_clients(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw or "")
    except ValueError:
        raise ConfigError("CLIENTS_JSON is not valid JSON") from None
    items = data.get("clients") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ConfigError('CLIENTS_JSON must look like {"clients": [ ... ]} with at least one client')
    clients, seen = [], set()
    for i, c in enumerate(items):
        if not isinstance(c, dict) or not c.get("slug") or not c.get("region"):
            raise ConfigError(f"client #{i + 1} needs a slug and a region")
        if c["slug"] in seen:
            raise ConfigError(f"duplicate client slug {c['slug']!r}")
        seen.add(c["slug"])
        ids, names = {}, {}
        for key in ("ec2_instances", "rds_instances"):
            ids[key] = [x["id"] if isinstance(x, dict) else str(x) for x in c.get(key, [])]
            names.update({x["id"]: x["name"] for x in c.get(key, []) if isinstance(x, dict) and x.get("name")})
        if not ids["ec2_instances"] and not ids["rds_instances"]:
            raise ConfigError(f"client {c['slug']!r} has no EC2 or RDS instances")
        clients.append({"slug": c["slug"], "name": c.get("name") or c["slug"], "region": c["region"],
                        "role_arn": c.get("role_arn") or "", "external_id": c.get("external_id") or "",
                        "names": names, **ids})
    return clients


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    try:
        hours = float(env.get("THRESHOLD_HOURS") or 10)
    except ValueError:
        raise ConfigError("THRESHOLD_HOURS must be a number") from None
    if not 0 < hours <= MAX_THRESHOLD_HOURS:
        raise ConfigError(f"THRESHOLD_HOURS must be between 0 and {MAX_THRESHOLD_HOURS} (RDS only remembers {RDS_EVENT_WINDOW_DAYS} days)")
    rule = (env.get("STOP_RULE") or "all").strip().lower()
    if rule not in ("all", "any"):
        raise ConfigError('STOP_RULE must be "all" or "any"')
    return Config(clients=parse_clients(env.get("CLIENTS_JSON")), threshold_hours=hours,
                  dry_run=_flag(env.get("DRY_RUN"), True), rule=rule, sns_topic_arn=env.get("SNS_TOPIC_ARN", ""),
                  wait_seconds=int(env.get("WAIT_SECONDS") or 300), poll_seconds=int(env.get("POLL_SECONDS") or 10))


# ---------- AWS access ----------

def make_api(client: dict, service: str):
    """boto3 client for one service in one client's region, through that client's role when it has one."""
    kwargs = {"region_name": client["region"]}
    if client["role_arn"]:
        args = {"RoleArn": client["role_arn"], "RoleSessionName": ROLE_SESSION, "DurationSeconds": 3600}
        if client["external_id"]:
            args["ExternalId"] = client["external_id"]
        creds = boto3.client("sts").assume_role(**args)["Credentials"]
        kwargs.update(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                      aws_session_token=creds["SessionToken"])
    return boto3.client(service, **kwargs)


# ---------- inspecting ----------

@dataclass
class Resource:
    kind: str                       # "ec2" | "rds"
    id: str
    running: bool
    uptime: float | None = None     # seconds; None = unknown
    over: bool = False              # RDS only: no start event in the 14-day window, so up for longer than that
    exempt: bool = False

    @property
    def effective(self) -> float | None:
        return float("inf") if self.over else self.uptime


def _exempt(tags) -> bool:
    return any(t.get("Key") == EXEMPT_TAG and str(t.get("Value", "")).strip().lower() == EXEMPT_VALUE for t in tags or [])


def inspect_ec2(api, ids: list[str], now: datetime) -> list[Resource]:
    out = []
    if not ids:
        return out
    for res in api.describe_instances(InstanceIds=ids)["Reservations"]:
        for inst in res["Instances"]:
            running = inst["State"]["Name"] == "running"
            launch = inst.get("LaunchTime")
            uptime = max(0.0, (now - launch).total_seconds()) if running and isinstance(launch, datetime) else None
            out.append(Resource("ec2", inst["InstanceId"], running, uptime, exempt=_exempt(inst.get("Tags"))))
    return out


def rds_started(api, rid: str, now: datetime) -> tuple[float | None, bool]:
    """(seconds up, over_window) from RDS events. Raises on API errors so the caller can treat the uptime as unknown."""
    started = created = None
    pages = api.get_paginator("describe_events").paginate(
        SourceIdentifier=rid, SourceType="db-instance", Duration=RDS_EVENT_WINDOW_DAYS * 24 * 60)
    for page in pages:
        for ev in page.get("Events", []):
            when, msg = ev.get("Date"), ev.get("Message", "")
            if not isinstance(when, datetime):
                continue
            if msg == "DB instance started":
                started = when if started is None else max(started, when)
            elif msg == "DB instance created":
                created = when
    start = started or created
    if start is None:
        return None, True
    return max(0.0, (now - start).total_seconds()), False


def inspect_rds(api, ids: list[str], now: datetime) -> list[Resource]:
    out = []
    for rid in ids:
        db = api.describe_db_instances(DBInstanceIdentifier=rid)["DBInstances"][0]
        running = db["DBInstanceStatus"] == "available"
        tags = api.list_tags_for_resource(ResourceName=db["DBInstanceArn"]).get("TagList", [])
        uptime, over = None, False
        if running:
            try:
                uptime, over = rds_started(api, rid, now)
            except Exception as exc:                       # e.g. rds:DescribeEvents not allowed: unknown, so never stop on it
                log.warning("cannot read RDS events for %s: %s", rid, exc)
        out.append(Resource("rds", rid, running, uptime, over, _exempt(tags)))
    return out


# ---------- deciding ----------

def hours(seconds: float) -> str:
    return "over 14 days" if seconds == float("inf") else f"{seconds / 3600:.1f}h"


def decide(resources: list[Resource], threshold_hours: float, rule: str) -> tuple[str, str]:
    """("stop" | "skip", reason). Only running resources count."""
    running = [r for r in resources if r.running]
    if not running:
        return "skip", "nothing is running"
    exempt = [r.id for r in running if r.exempt]
    if exempt:
        return "skip", f"exempt by tag {EXEMPT_TAG}={EXEMPT_VALUE} on {', '.join(exempt)}"
    unknown = [r.id for r in running if r.effective is None]
    if unknown:
        return "skip", f"uptime unknown for {', '.join(unknown)}"
    limit = threshold_hours * 3600
    over = [r for r in running if r.effective >= limit]
    ups = ", ".join(f"{r.id} {hours(r.effective)}" for r in running)
    if (rule == "all" and len(over) == len(running)) or (rule == "any" and over):
        return "stop", f"up {ups} (limit {threshold_hours:g}h, rule {rule})"
    return "skip", f"under the limit: {ups} (limit {threshold_hours:g}h, rule {rule})"


# ---------- acting ----------

def stop_environment(ec2, rds, running: list[Resource], cfg: Config, sleep=time.sleep) -> None:
    """Applications first, wait until stopped, then databases. Raises if the applications do not stop in time."""
    ec2_ids = [r.id for r in running if r.kind == "ec2"]
    rds_ids = [r.id for r in running if r.kind == "rds"]
    if ec2_ids:
        ec2.stop_instances(InstanceIds=ec2_ids)
        deadline = time.monotonic() + cfg.wait_seconds
        while True:
            states = [i["State"]["Name"] for res in ec2.describe_instances(InstanceIds=ec2_ids)["Reservations"] for i in res["Instances"]]
            if states and all(s == "stopped" for s in states):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"applications still not stopped after {cfg.wait_seconds}s; databases left running")
            sleep(cfg.poll_seconds)
    for rid in rds_ids:
        rds.stop_db_instance(DBInstanceIdentifier=rid)


def process_client(client: dict, cfg: Config, now: datetime, api_factory=make_api, sleep=time.sleep) -> dict:
    result = {"client": client["slug"], "name": client["name"], "action": "skipped", "reason": ""}
    try:
        ec2 = api_factory(client, "ec2") if client["ec2_instances"] else None
        rds = api_factory(client, "rds") if client["rds_instances"] else None
        resources = (inspect_ec2(ec2, client["ec2_instances"], now) if ec2 else []) + \
                    (inspect_rds(rds, client["rds_instances"], now) if rds else [])
        verdict, reason = decide(resources, cfg.threshold_hours, cfg.rule)
        result["reason"] = reason
        if verdict == "stop":
            running = [r for r in resources if r.running]
            result["resources"] = [f"{r.kind}:{r.id}" for r in running]
            result["details"] = [{"kind": r.kind, "id": r.id, "name": client["names"].get(r.id, ""), "up": duration(r.effective)}
                                 for r in running]
            if cfg.dry_run:
                result["action"] = "would_stop"
            else:
                stop_environment(ec2, rds, running, cfg, sleep)
                result["action"] = "stopped"
    except Exception as exc:
        result.update(action="error", reason=f"{type(exc).__name__}: {exc}")
    return result


def duration(seconds: float | None) -> str:
    """'1d 5h 30m' for people; 'over 14 days' for an RDS database with no start event in the window."""
    if seconds is None:
        return "unknown"
    if seconds == float("inf"):
        return "over 14 days"
    m = int(seconds // 60)
    d, rest = divmod(m, 1440)
    h, m = divmod(rest, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h or d else "", f"{m}m") if p)


HEADLINES = {"would_stop": "WOULD BE STOPPED", "stopped": "STOPPED", "error": "FAILED"}


def email_subject(r: dict, dry_run: bool) -> str:
    """One email per client, with the client's name in the subject. SNS subjects must be single-line ASCII, 100 characters at most."""
    what = {"would_stop": "would be stopped", "stopped": "stopped", "error": "FAILED"}[r["action"]]
    prefix = "[Dry run] " if dry_run and r["action"] != "error" else ""
    text = f"{prefix}L180 auto-stop: {r['name']} {what}"
    return " ".join(text.encode("ascii", "ignore").decode().split())[:100]


def email_body(r: dict, cfg: Config, now: datetime) -> str:
    rule = "every running resource" if cfg.rule == "all" else "at least one running resource"
    mode = "DRY RUN - nothing was stopped" if cfg.dry_run else "LIVE"
    rows = [(f"{d['name']} ({d['id']})" if d["name"] else d["id"], "EC2" if d["kind"] == "ec2" else "RDS", d["up"])
            for d in r.get("details", [])]
    width = max([len(x[0]) for x in rows] + [8])
    out = [
        "L180 AUTO-STOP",
        "=" * 60,
        f"  Client : {r['name']}",
        f"  Result : {HEADLINES[r['action']]}",
        f"  Mode   : {mode}",
        f"  Rule   : stop when {rule} has been up {cfg.threshold_hours:g}h or more",
        f"  When   : {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
    ]
    if rows:
        out += [f"  {'RESOURCE'.ljust(width)}  TYPE  UP FOR", f"  {'-' * width}  ----  {'-' * 12}"]
        out += [f"  {n.ljust(width)}  {t:<4}  {u}" for n, t, u in rows]
        out.append("")
    if r["action"] == "error":
        out += ["  What happened:", f"    {r['reason']}", "", "  Nothing further was stopped for this client. Check the CloudWatch log /aws/lambda/l180-auto-stop."]
    elif r["action"] == "would_stop":
        out += ["  Applications would be stopped first, then databases.",
                "  Redeploy with --live to turn this on. Tag a resource auto-stop=disabled to exempt its client."]
    else:
        out += ["  Applications were stopped first, then databases.", "  Start the environment again from the control panel when needed."]
    return "\n".join(out) + "\n"


def notify(cfg: Config, results: list[dict], api_factory=None, now: datetime | None = None) -> None:
    if not cfg.sns_topic_arn:
        return
    now = now or _now()
    try:
        sns = (api_factory or boto3.client)("sns")
    except Exception as exc:
        log.warning("could not create the SNS client: %s", exc)
        return
    for r in results:
        if r["action"] not in HEADLINES:
            continue
        try:
            sns.publish(TopicArn=cfg.sns_topic_arn, Subject=email_subject(r, cfg.dry_run), Message=email_body(r, cfg, now))
        except Exception as exc:                              # a failed email must not fail the run
            log.warning("could not publish the notification for %s: %s", r["client"], exc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run(cfg: Config, now: datetime | None = None, api_factory=make_api, sleep=time.sleep, sns_factory=None) -> dict:
    now = now or _now()
    results = [process_client(c, cfg, now, api_factory, sleep) for c in cfg.clients]
    for r in results:
        log.info("auto-stop %s", json.dumps(r, sort_keys=True))
    notify(cfg, results, sns_factory, now)
    return {"dry_run": cfg.dry_run, "threshold_hours": cfg.threshold_hours, "results": results}


def lambda_handler(event, context):
    cfg = load_config()
    if isinstance(event, dict) and isinstance(event.get("dry_run"), bool):     # manual test: {"dry_run": false}
        cfg = Config(**{**cfg.__dict__, "dry_run": event["dry_run"]})
    return run(cfg)
