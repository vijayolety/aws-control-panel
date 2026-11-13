"""The auto-stop Lambda (autostop/src/handler.py), against moto's fake AWS."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "autostop" / "src"))
import handler as h  # noqa: E402

REGION = "ap-south-2"
HOUR = 3600


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def clients_json(ec2=(), rds=(), **extra):
    return json.dumps({"clients": [{"slug": "acme", "region": REGION, "ec2_instances": [{"id": i} for i in ec2],
                                    "rds_instances": [{"id": i} for i in rds], **extra}]})


def cfg(ec2=(), rds=(), **env):
    return h.load_config({"CLIENTS_JSON": clients_json(ec2, rds), "POLL_SECONDS": "0", "WAIT_SECONDS": "5", **env})


def launch_ec2(n=1, tags=None):
    api = boto3.client("ec2", region_name=REGION)
    kwargs = {"TagSpecifications": [{"ResourceType": "instance", "Tags": tags}]} if tags else {}
    return [i["InstanceId"] for i in api.run_instances(ImageId="ami-12345678", MinCount=n, MaxCount=n, **kwargs)["Instances"]], api


def make_db(name="acme-db", tags=None):
    api = boto3.client("rds", region_name=REGION)
    api.create_db_instance(DBInstanceIdentifier=name, DBInstanceClass="db.t3.micro", Engine="postgres",
                           MasterUsername="u", MasterUserPassword="password123", AllocatedStorage=20, Tags=tags or [])
    return api


def later(hours):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def state(api, ids):
    return {i["State"]["Name"] for r in api.describe_instances(InstanceIds=ids)["Reservations"] for i in r["Instances"]}


@pytest.fixture
def events(monkeypatch):
    """moto does not model RDS events; feed rds_started a chosen start time instead."""
    box = {"hours": 13, "raises": False}

    def fake(api, rid, now):
        if box["raises"]:
            raise RuntimeError("AccessDenied")
        return (None, True) if box["hours"] is None else (box["hours"] * HOUR, False)

    monkeypatch.setattr(h, "rds_started", fake)
    return box


# ---------- config ----------

def test_defaults_are_the_safe_ones():
    c = h.load_config({"CLIENTS_JSON": clients_json(ec2=["i-1"])})
    assert c.dry_run is True and c.threshold_hours == 10 and c.rule == "all"


@pytest.mark.parametrize("env", [
    {"CLIENTS_JSON": "nope"},
    {"CLIENTS_JSON": "{}"},
    {"CLIENTS_JSON": '{"clients":[{"slug":"a"}]}'},
    {"CLIENTS_JSON": '{"clients":[{"slug":"a","region":"r"}]}'},          # no resources
    {"CLIENTS_JSON": clients_json(ec2=["i-1"]), "THRESHOLD_HOURS": "0"},
    {"CLIENTS_JSON": clients_json(ec2=["i-1"]), "THRESHOLD_HOURS": "400"},   # beyond what RDS events can prove
    {"CLIENTS_JSON": clients_json(ec2=["i-1"]), "THRESHOLD_HOURS": "abc"},
    {"CLIENTS_JSON": clients_json(ec2=["i-1"]), "STOP_RULE": "sometimes"},
])
def test_bad_config_is_refused(env):
    with pytest.raises(h.ConfigError):
        h.load_config(env)


def test_duplicate_slugs_are_refused():
    one = {"slug": "a", "region": "r", "ec2_instances": ["i-1"]}
    with pytest.raises(h.ConfigError):
        h.parse_clients(json.dumps({"clients": [one, one]}))


# ---------- the decision ----------

def res(kind, uptime, running=True, over=False, exempt=False):
    return h.Resource(kind, f"{kind}-1", running, uptime, over, exempt)


def test_all_rule_needs_every_running_resource_over_the_limit():
    ok = [res("rds", 13 * HOUR), res("ec2", 12 * HOUR)]
    assert h.decide(ok, 12, "all")[0] == "stop"
    young_app = [res("rds", 30 * HOUR), res("ec2", 2 * HOUR)]
    assert h.decide(young_app, 12, "all")[0] == "skip"
    assert h.decide(young_app, 12, "any")[0] == "stop"


def test_just_under_the_limit_is_not_stopped():
    assert h.decide([res("ec2", 12 * HOUR - 1)], 12, "all")[0] == "skip"


def test_stopped_resources_do_not_count_and_nothing_running_is_a_skip():
    assert h.decide([res("rds", None, running=False), res("ec2", 13 * HOUR)], 12, "all")[0] == "stop"
    assert h.decide([res("ec2", None, running=False)], 12, "all") == ("skip", "nothing is running")


def test_exempt_tag_protects_the_whole_environment():
    verdict, reason = h.decide([res("ec2", 99 * HOUR), res("rds", 99 * HOUR, exempt=True)], 12, "any")
    assert verdict == "skip" and "exempt" in reason


def test_unknown_uptime_is_never_a_reason_to_stop():
    verdict, reason = h.decide([res("ec2", 99 * HOUR), res("rds", None)], 12, "any")
    assert verdict == "skip" and "unknown" in reason


def test_a_database_with_no_event_in_the_window_counts_as_over_14_days():
    assert h.decide([res("rds", None, over=True)], 12, "all")[0] == "stop"


# ---------- against fake AWS ----------

@mock_aws
def test_stops_apps_then_database_when_everything_is_over_the_limit(events):
    ids, ec2 = launch_ec2(2)
    rds = make_db()
    out = h.run(cfg(ids, ["acme-db"], DRY_RUN="false"), now=later(13), sleep=lambda s: None)
    assert out["results"][0]["action"] == "stopped", out
    assert state(ec2, ids) == {"stopped"}
    assert rds.describe_db_instances(DBInstanceIdentifier="acme-db")["DBInstances"][0]["DBInstanceStatus"] in ("stopped", "stopping")


@mock_aws
def test_dry_run_changes_nothing(events):
    ids, ec2 = launch_ec2()
    rds = make_db()
    out = h.run(cfg(ids, ["acme-db"]), now=later(13))            # DRY_RUN not set: the default must be dry
    assert out["dry_run"] is True and out["results"][0]["action"] == "would_stop"
    assert state(ec2, ids) == {"running"}
    assert rds.describe_db_instances(DBInstanceIdentifier="acme-db")["DBInstances"][0]["DBInstanceStatus"] == "available"


@mock_aws
def test_under_the_limit_nothing_is_stopped(events):
    events["hours"] = 1
    ids, ec2 = launch_ec2()
    make_db()
    out = h.run(cfg(ids, ["acme-db"], DRY_RUN="false"), now=later(1))
    assert out["results"][0]["action"] == "skipped"
    assert state(ec2, ids) == {"running"}


@mock_aws
def test_exempt_tag_on_an_instance_blocks_the_stop(events):
    ids, ec2 = launch_ec2(tags=[{"Key": "auto-stop", "Value": "disabled"}])
    out = h.run(cfg(ids, DRY_RUN="false"), now=later(13))
    assert out["results"][0]["action"] == "skipped" and "exempt" in out["results"][0]["reason"]
    assert state(ec2, ids) == {"running"}


@mock_aws
def test_exempt_tag_on_a_database_blocks_the_stop(events):
    ids, ec2 = launch_ec2()
    make_db(tags=[{"Key": "auto-stop", "Value": "disabled"}])
    out = h.run(cfg(ids, ["acme-db"], DRY_RUN="false"), now=later(13))
    assert out["results"][0]["action"] == "skipped"
    assert state(ec2, ids) == {"running"}


@mock_aws
def test_unreadable_rds_events_stop_nothing(events):
    events["raises"] = True
    ids, ec2 = launch_ec2()
    make_db()
    out = h.run(cfg(ids, ["acme-db"], DRY_RUN="false"), now=later(13))
    assert out["results"][0]["action"] == "skipped" and "unknown" in out["results"][0]["reason"]
    assert state(ec2, ids) == {"running"}


@mock_aws
def test_already_stopped_environment_is_left_alone(events):
    ids, ec2 = launch_ec2()
    ec2.stop_instances(InstanceIds=ids)
    out = h.run(cfg(ids, DRY_RUN="false"), now=later(13))
    assert out["results"][0]["reason"] == "nothing is running"


@mock_aws
def test_database_is_left_running_if_the_applications_do_not_stop(events, monkeypatch):
    ids, ec2 = launch_ec2()
    rds = make_db()

    class Stuck:
        def __init__(self, real): self.real = real
        def stop_instances(self, **kw): return {}                       # accepted, but never reaches "stopped"
        def __getattr__(self, name): return getattr(self.real, name)

    def factory(client, service):
        api = boto3.client(service, region_name=REGION)
        return Stuck(api) if service == "ec2" else api

    out = h.run(cfg(ids, ["acme-db"], DRY_RUN="false", WAIT_SECONDS="0"), now=later(13), api_factory=factory, sleep=lambda s: None)
    r = out["results"][0]
    assert r["action"] == "error" and "databases left running" in r["reason"]
    assert rds.describe_db_instances(DBInstanceIdentifier="acme-db")["DBInstances"][0]["DBInstanceStatus"] == "available"


@mock_aws
def test_a_failing_client_does_not_stop_the_others(events):
    ids, ec2 = launch_ec2()
    raw = json.dumps({"clients": [
        {"slug": "broken", "region": REGION, "ec2_instances": ["i-doesnotexist"]},
        {"slug": "acme", "region": REGION, "ec2_instances": ids}]})
    out = h.run(h.load_config({"CLIENTS_JSON": raw, "DRY_RUN": "false", "POLL_SECONDS": "0"}), now=later(13), sleep=lambda s: None)
    assert [r["action"] for r in out["results"]] == ["error", "stopped"]
    assert state(ec2, ids) == {"stopped"}


@mock_aws
def test_one_email_per_client_with_the_client_name_in_the_subject(events):
    ids_a, _ = launch_ec2()
    ids_b, _ = launch_ec2()
    raw = json.dumps({"clients": [
        {"slug": "uniservice", "name": "Uniservice", "region": REGION, "ec2_instances": [{"id": ids_a[0], "name": "Uniservice test app"}]},
        {"slug": "acme", "name": "Acme Corp", "region": REGION, "ec2_instances": [{"id": ids_b[0], "name": "Acme web"}]}]})
    sent = []

    class Spy:
        def publish(self, **kw): sent.append(kw)

    h.run(h.load_config({"CLIENTS_JSON": raw, "SNS_TOPIC_ARN": "arn:aws:sns:x:1:t"}), now=later(13), sns_factory=lambda svc: Spy())
    assert [m["Subject"] for m in sent] == ["[Dry run] L180 auto-stop: Uniservice would be stopped",
                                           "[Dry run] L180 auto-stop: Acme Corp would be stopped"]
    body = sent[0]["Message"]
    assert "Client : Uniservice" in body and "WOULD BE STOPPED" in body and "DRY RUN - nothing was stopped" in body
    assert f"Uniservice test app ({ids_a[0]})" in body and "EC2" in body and "13h" in body
    assert "Acme web" not in body                                                  # each email is about one client only


@mock_aws
def test_live_subject_has_no_dry_run_tag_and_errors_say_failed(events):
    ids, _ = launch_ec2()
    sent = []

    class Spy:
        def publish(self, **kw): sent.append(kw)

    h.run(cfg(ids, DRY_RUN="false", SNS_TOPIC_ARN="arn:aws:sns:x:1:t"), now=later(13), sns_factory=lambda svc: Spy(), sleep=lambda s: None)
    assert sent[0]["Subject"] == "L180 auto-stop: acme stopped" and "Result : STOPPED" in sent[0]["Message"]
    sent.clear()
    bad = h.load_config({"CLIENTS_JSON": clients_json(["i-doesnotexist"]), "SNS_TOPIC_ARN": "arn:aws:sns:x:1:t"})
    h.run(bad, now=later(13), sns_factory=lambda svc: Spy())
    assert sent[0]["Subject"] == "L180 auto-stop: acme FAILED" and "Result : FAILED" in sent[0]["Message"]


def test_subject_is_single_line_ascii_and_at_most_100_characters():
    r = {"name": "Caf" + chr(233) + chr(10) + "Client " + "x" * 200, "action": "would_stop"}
    subject = h.email_subject(r, True)
    assert subject.isascii() and chr(10) not in subject and len(subject) <= 100 and subject.startswith("[Dry run] L180 auto-stop: Caf")


@pytest.mark.parametrize("seconds,text", [(0, "0m"), (59 * 60, "59m"), (3600, "1h 0m"), (13.5 * 3600, "13h 30m"),
                                          (29 * 3600 + 28 * 60, "1d 5h 28m"), (float("inf"), "over 14 days"), (None, "unknown")])
def test_durations_read_naturally(seconds, text):
    assert h.duration(seconds) == text


@mock_aws
def test_a_failing_notification_is_harmless(events):
    ids, _ = launch_ec2()

    class Broken:
        def publish(self, **kw): raise RuntimeError("boom")

    out = h.run(cfg(ids, SNS_TOPIC_ARN="arn:aws:sns:x:1:t"), now=later(13), sns_factory=lambda svc: Broken())
    assert out["results"][0]["action"] == "would_stop"


@mock_aws
def test_nothing_to_report_sends_no_notification(events):
    events["hours"] = 1
    ids, _ = launch_ec2()
    sent = []

    class Spy:
        def publish(self, **kw): sent.append(kw)

    h.run(cfg(ids, SNS_TOPIC_ARN="arn:aws:sns:x:1:t"), now=later(1), sns_factory=lambda svc: Spy())
    assert sent == []


@mock_aws
def test_lambda_handler_event_can_override_dry_run(events, monkeypatch):
    ids, ec2 = launch_ec2()
    monkeypatch.setenv("CLIENTS_JSON", clients_json(ids))
    monkeypatch.setenv("POLL_SECONDS", "0")
    monkeypatch.setattr(h, "_now", lambda: later(13))
    out = h.lambda_handler({"dry_run": False}, None)
    assert out["dry_run"] is False and out["results"][0]["action"] == "stopped"
    assert state(ec2, ids) == {"stopped"}


def test_rds_start_time_uses_the_newest_started_event_and_ignores_reboots():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    ev = lambda msg, hrs: {"Message": msg, "Date": now - timedelta(hours=hrs)}   # noqa: E731

    class Api:
        def get_paginator(self, name):
            class P:
                def paginate(self, **kw):
                    assert kw["SourceType"] == "db-instance" and kw["Duration"] == 14 * 24 * 60
                    return [{"Events": [ev("DB instance started", 30), ev("DB instance started", 5), ev("DB instance restarted", 1),
                                        ev("DB instance created", 200)]}]
            return P()

    assert h.rds_started(Api(), "db", now) == (5 * HOUR, False)

    class Empty(Api):
        def get_paginator(self, name):
            class P:
                def paginate(self, **kw): return [{"Events": []}]
            return P()

    assert h.rds_started(Empty(), "db", now) == (None, True)


# ---------- deploy script and template ----------

def _deploy_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("autostop_deploy", Path(__file__).resolve().parent.parent / "autostop" / "deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_deploy_plan_validates_clients_and_touches_nothing(tmp_path, capsys):
    f = tmp_path / "clients.json"
    f.write_text(json.dumps({"clients": [{"slug": "acme", "name": "Acme", "region": REGION, "secret_note": "x",
                                          "ec2_instances": [{"id": "i-1", "name": "App"}], "rds_instances": [{"id": "db", "name": "D"}]}]}))
    assert _deploy_module().main(["--bucket", "b", "--clients", str(f), "--region", REGION, "--plan"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "acme" in out and "LIVE" not in out


def test_deploy_refuses_a_client_list_the_lambda_would_refuse(tmp_path):
    f = tmp_path / "clients.json"
    f.write_text(json.dumps({"clients": [{"slug": "acme", "region": REGION}]}))          # no resources
    with pytest.raises(h.ConfigError):
        _deploy_module().main(["--bucket", "b", "--clients", str(f), "--region", REGION, "--plan"])


def test_deployed_zip_contains_exactly_the_handler_and_is_reproducible():
    import io
    import zipfile
    d = _deploy_module()
    a, b = d.build_zip(), d.build_zip()
    assert a == b
    assert zipfile.ZipFile(io.BytesIO(a)).namelist() == ["handler.py"]


@mock_aws
def test_the_stack_creates_in_fake_aws_dry_run_by_default(tmp_path):
    f = tmp_path / "clients.json"
    f.write_text(json.dumps({"clients": [{"slug": "acme", "region": REGION, "ec2_instances": [{"id": "i-1"}]}]}))
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="code", CreateBucketConfiguration={"LocationConstraint": REGION})
    assert _deploy_module().main(["--bucket", "code", "--clients", str(f), "--region", REGION, "--email", "a@example.com"]) == 0
    lam = boto3.client("lambda", region_name=REGION)
    env = lam.get_function_configuration(FunctionName="l180-auto-stop")
    assert env["Environment"]["Variables"]["DRY_RUN"] == "true" and env["Timeout"] == 900
    rules = boto3.client("events", region_name=REGION).list_rules(NamePrefix="l180-auto-stop")["Rules"]
    assert rules and rules[0]["ScheduleExpression"] == "cron(30 * * * ? *)"


@mock_aws
def test_several_recipients_and_a_redeploy_never_drops_them(tmp_path):
    f = tmp_path / "clients.json"
    f.write_text(json.dumps({"clients": [{"slug": "acme", "region": REGION, "ec2_instances": [{"id": "i-1"}]}]}))
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="code", CreateBucketConfiguration={"LocationConstraint": REGION})
    d = _deploy_module()
    base = ["--bucket", "code", "--clients", str(f), "--region", REGION]
    assert d.main(base + ["--email", "a@example.com"]) == 0
    assert d.main(base + ["--also-notify", "b@example.com", "--also-notify", "c@example.com", "--also-notify", "a@example.com"]) == 0
    cf = boto3.client("cloudformation", region_name=REGION)      # moto does not evaluate the template's conditions, so check the inputs

    def params():
        return {p["ParameterKey"]: p["ParameterValue"] for p in cf.describe_stacks(StackName="l180-auto-stop")["Stacks"][0]["Parameters"]}

    assert params()["NotificationEmail"] == "a@example.com"
    assert params()["AdditionalEmails"] == "b@example.com,c@example.com"                   # a@ was listed twice, kept once
    assert d.main(base + ["--live"]) == 0                                                  # no email flags: nobody is dropped
    assert params()["NotificationEmail"] == "a@example.com" and params()["AdditionalEmails"] == "b@example.com,c@example.com"
    assert params()["DryRun"] == "false"


def test_bad_or_too_many_addresses_are_refused(tmp_path):
    f = tmp_path / "clients.json"
    f.write_text(json.dumps({"clients": [{"slug": "acme", "region": REGION, "ec2_instances": [{"id": "i-1"}]}]}))
    base = ["--bucket", "b", "--clients", str(f), "--region", REGION, "--plan"]
    d = _deploy_module()
    with pytest.raises(SystemExit):
        d.main(base + ["--also-notify", "not-an-email"])
    with pytest.raises(SystemExit):
        d.main(base + sum((["--also-notify", f"u{i}@example.com"] for i in range(5)), []))
