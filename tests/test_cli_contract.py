"""What mes (jobs and the gate) relies on: `describe`, MES_ALLOW, and the exit codes.

The describe JSON must have the shape mes apps produce (mes.App.describe), without fitdata
depending on mes. Nothing here talks to Garmin or ssh."""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys

import pytest

from conftest import DAY
from fakegarmin import FakeSession
from fitdata_garmin import cli, client, describe, paths

COMMAND_KEYS = {"name", "kind", "help", "builtin", "args"}
ARG_KEYS = {"name", "flag", "kind", "type", "required", "default", "many", "choices", "help"}
TOP_KEYS = {"app", "title", "version", "mes_version", "allowed", "json", "exit_codes", "commands", "jobs"}


def _describe(run, *env_args):
    code, out, err = run("--json", "describe")
    assert code == 0, err
    return json.loads(out)


def test_describe_has_the_mes_shape(run):
    d = _describe(run)
    assert TOP_KEYS <= set(d) and d["app"] == "fitdata-garmin" and d["allowed"] == "admin"
    assert d["exit_codes"] == {"0": "ok", "1": "error", "2": "usage", "3": "not found", "4": "not allowed",
                               "5": "refused for safety", "6": "needs a person", "75": "temporary failure, retry"}
    for c in d["commands"]:
        assert set(c) == COMMAND_KEYS and c["kind"] in ("read", "write", "admin") and c["help"]
        for a in c["args"]:
            assert set(a) == ARG_KEYS and a["kind"] in ("positional", "option", "flag") and a["help"]
    json.dumps(d)  # plain JSON all the way down


def test_describe_lists_every_command_with_its_kind(run):
    d = _describe(run)
    kinds = {c["name"]: c["kind"] for c in d["commands"]}
    parser_commands = set(describe._subparsers(cli._parser()))
    assert set(kinds) == parser_commands == set(describe.COMMAND_KINDS)
    assert kinds["login"] == "admin"
    assert {k for k, v in kinds.items() if v == "write"} == {
        "sync", "activities-sync", "backfill", "account", "index", "migrate-storage"}
    for read in ("summary", "days", "activities", "day", "activity", "sources", "schema", "sql", "status", "describe"):
        assert kinds[read] == "read"
    builtin = {c["name"] for c in d["commands"] if c["builtin"]}
    assert builtin == {"describe", "guide", "schema", "sources"}


def test_describe_arguments(run):
    by = {c["name"]: {a["name"]: a for a in c["args"]} for c in _describe(run)["commands"]}
    assert by["days"]["from"] == {"name": "from", "flag": "--from", "kind": "option", "type": "str",
                                  "required": False, "default": None, "many": False, "choices": None,
                                  "help": "first day, YYYY-MM-DD"}
    assert by["day"]["date"]["kind"] == "positional" and by["day"]["date"]["required"]
    assert by["sql"]["limit"]["default"] == 1000 and by["sql"]["limit"]["type"] == "int"
    assert by["activity"]["records"]["kind"] == "flag" and by["activity"]["records"]["type"] == "bool"
    assert by["migrate-storage"]["to"]["choices"] == ["bucket", "files"] and by["migrate-storage"]["to"]["required"]
    assert by["backfill"]["max_days"]["flag"] == "--max-days"
    assert "json" not in by["days"]  # the global flags are not repeated per command


def test_describe_lists_the_jobs_of_app_toml(run):
    d = _describe(run)
    assert d["short"] == "garmin" and d["project"] == "fitdata" and d["title"] == "Garmin archive"
    jobs = {j["name"]: j for j in d["jobs"]}
    assert set(jobs) == {"activities", "daily", "backfill"}
    assert jobs["backfill"]["id"] == "fitdata-garmin.backfill" and jobs["backfill"]["app"] == "fitdata-garmin"
    assert jobs["backfill"]["run"] == "backfill --max-days 90 --local" and jobs["backfill"]["trigger"] == "schedule"
    assert jobs["daily"]["schedule"] == "*-*-* 07,13,22:30:00" and jobs["daily"]["timeout"] == "30m"


def test_describe_applies_the_projects_settings(run, tmp_path, monkeypatch):
    project = tmp_path / "project.toml"
    project.write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin"]\n'
                       '[settings.fitdata-garmin]\njobs.backfill.enabled = false\n'
                       'jobs.daily.schedule = "*-*-* 08:00:00"\njobs.daily.timeout = "45m"\n'
                       'jobs.activities.add_network = ["example.org"]\n')
    monkeypatch.setattr(describe, "PROJECT_TOML", project)
    jobs = {j["name"]: j for j in _describe(run)["jobs"]}
    assert set(jobs) == {"activities", "daily"}  # backfill is switched off
    assert jobs["daily"]["schedule"] == "*-*-* 08:00:00" and jobs["daily"]["timeout"] == "45m"
    assert jobs["activities"]["network"][-1] == "example.org" and jobs["activities"]["memory"] == "512M"


def test_describe_text_and_guide(run):
    code, out, _ = run("describe")
    assert code == 0 and "login" in out and "admin" in out and "This session may run" in out
    code, out, _ = run("--json", "guide")
    guide = json.loads(out)
    assert code == 0 and guide["app"] == "fitdata-garmin" and guide["guide"].startswith("# Garmin archive")


def test_describe_never_forwards(run, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.setenv("FITDATA_REMOTE", "my-server")
    from fitdata_garmin import config
    config.load.cache_clear()
    assert run("--json", "describe")[0] == 0 and not calls


def test_describe_as_mes_runs_it(archive):
    """mes runs `<program> --json describe` as a separate process with MES_ALLOW=read."""
    import os
    env = {**os.environ, "MES_ALLOW": "read"}
    res = subprocess.run([sys.executable, "-m", "fitdata_garmin.cli", "--json", "describe"], capture_output=True, env=env)
    assert res.returncode == 0 and json.loads(res.stdout)["allowed"] == "read"


# ------------------------------------------------------------------ MES_ALLOW

@pytest.fixture
def no_garmin(monkeypatch):
    """Any attempt to start a Garmin session is recorded (and never reaches the network)."""
    started = []

    class Recorder(FakeSession):
        def __init__(self):
            started.append(True)
            super().__init__()
    monkeypatch.setattr(client, "Session", Recorder)
    return started


@pytest.mark.parametrize("level, argv", [
    ("read", ["sync"]), ("read", ["backfill"]), ("read", ["index"]), ("read", ["account"]),
    ("read", ["activities-sync"]), ("read", ["migrate-storage", "--to", "bucket"]), ("read", ["login"]),
    ("write", ["login"]), ("read", ["decode", "x.fit", "--csv"]),
])
def test_command_above_the_allowed_level_exits_4_before_running(run, monkeypatch, no_garmin, level, argv):
    monkeypatch.setenv("MES_ALLOW", level)
    paths.LOCK.unlink(missing_ok=True)
    db_mtime = paths.DB.stat().st_mtime_ns
    code, out, err = run("--json", *argv)
    assert code == 4 and out == "" and json.loads(err)["kind"] == "not_allowed"
    assert not no_garmin and not paths.LOCK.exists() and paths.DB.stat().st_mtime_ns == db_mtime


@pytest.mark.parametrize("level", ["read", "write", "admin", " READ "])
def test_reads_allowed_at_every_level(run, monkeypatch, level):
    monkeypatch.setenv("MES_ALLOW", level)
    assert run("--json", "day", DAY)[0] == 0
    assert run("--json", "sql", "SELECT 1 AS one")[0] == 0


def test_write_level_runs_write_commands(run, monkeypatch, no_garmin):
    monkeypatch.setenv("MES_ALLOW", "write")
    assert run("index")[0] == 0
    assert run("sync", "--days", "1")[0] == 0 and no_garmin


@pytest.mark.parametrize("value", ["", "  ", "root", "readonly"])
def test_unknown_or_empty_level_allows_nothing(run, monkeypatch, value):
    monkeypatch.setenv("MES_ALLOW", value)
    for argv in (["describe"], ["summary"], ["sync"]):
        code, _, err = run("--json", *argv)
        assert code == 4 and "MES_ALLOW" in json.loads(err)["error"]


def test_level_checked_before_forwarding(run, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.setenv("FITDATA_REMOTE", "my-server")
    monkeypatch.setenv("MES_ALLOW", "read")
    from fitdata_garmin import config
    config.load.cache_clear()
    assert run("sync")[0] == 4 and not calls
    assert run("summary")[0] == 0 and len(calls) == 1


def test_decode_is_refused_through_the_gate(run, monkeypatch, tmp_path):
    monkeypatch.setenv("MES_VIA_GATE", "1")
    code, _, err = run("decode", str(tmp_path / "x.fit"))
    assert code == 5 and "gate" in err


def test_decode_refuses_the_saved_login(run, archive):
    code, out, err = run("decode", str(archive / ".state" / "garmin-tokens.json"))
    assert code == 5 and "login" in err


# ------------------------------------------------------------------ exit codes

def test_not_found_is_3(run, archive):
    assert run("day", "2001-01-01")[0] == 3
    assert run("activity", "999")[0] == 3
    assert run("day", DAY, "--source", "hrv")[0] == 3
    paths.DB.unlink()
    code, _, err = run("--json", "summary")
    assert code == 3 and json.loads(err)["kind"] == "not_found" and "fitdata index" in json.loads(err)["hint"]


def test_usage_errors_are_2_and_follow_json(run):
    code, out, err = run("--json", "days", "--days", "many")
    assert code == 2 and out == "" and json.loads(err)["kind"] == "bad_input"
    assert run("no-such-command")[0] == 2
    assert run("migrate-storage", "--to", "s3")[0] == 2
    assert run()[0] == 2


def test_help_still_exits_0(run):
    with pytest.raises(SystemExit) as e:
        run("--help")
    assert e.value.code == 0


def test_login_needed_is_6_with_a_needs_human_result(run, archive):
    (archive / ".state" / "garmin-tokens.json").unlink()
    code, out, err = run("--json", "sync")
    assert code == 6
    payload = json.loads(out)
    assert payload["status"] == "needs_human" and payload["id"] == "garmin_login" and "fitdata login" in payload["action"]
    assert payload["rerun"] == "fitdata sync"
    code, out, err = run("sync")
    assert code == 6 and out == "" and "fitdata login" in err


def test_lock_held_is_75(run, archive, no_garmin):
    paths.STATE.mkdir(parents=True, exist_ok=True)
    with open(paths.LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _, err = run("--json", "activities-sync")
    assert code == 75 and json.loads(err)["kind"] == "temporary" and not no_garmin


def test_rate_limit_is_75(run, archive, monkeypatch):
    monkeypatch.setattr(client, "Session", lambda: FakeSession(fail_with=client.RateLimited("Garmin is limiting")))
    code, _, err = run("--json", "sync")
    assert code == 75 and json.loads(err)["kind"] == "temporary"


def test_garmin_rate_limit_after_retries_becomes_rate_limited(monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    s = client.Session()

    def limited(*a):
        raise GarminConnectTooManyRequestsError("429")
    with pytest.raises(client.RateLimited):
        s.call(limited)
    assert s.calls == 4  # the first try and three waits, as before


def test_errors_carry_their_exit_code(run, archive):
    code, out, err = run("--json", "day", "2001-01-01")
    assert json.loads(err)["exit_code"] == 3 == code
