"""project.toml (the mes project) and systemd/ (the standalone way) describe the same three jobs."""
from __future__ import annotations

import configparser
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from fitdata import cli, describe

REPO = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((REPO / "project.toml").read_text())
# job name -> systemd unit name
UNITS = {"activities": "fitdata-activities", "daily": "fitdata-daily", "backfill": "fitdata-backfill"}


def _unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string((REPO / "systemd" / name).read_text())
    return cp


def _calendar(text: str) -> set[tuple[str, str]]:
    """Expand the few OnCalendar forms used here into (hour, minute) pairs, or ('*', step)."""
    text = text.strip()
    if m := re.fullmatch(r"\*:0/(\d+)", text):
        return {("*", f"/{m.group(1)}")}
    m = re.fullmatch(r"\*-\*-\* ([0-9,]+):(\d\d)(?::00)?", text)
    assert m, text
    return {(h.zfill(2), m.group(2)) for h in m.group(1).split(",")}


def test_project_table():
    p = PROJECT["project"]
    assert p["name"] == "fitdata" and re.fullmatch(r"[a-z][a-z0-9-]{1,30}", p["name"])
    assert p["title"] == "Fitness" and p["version"]
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert PROJECT["apps"]["fitdata"]["cli"] in pyproject["project"]["scripts"]
    assert not (REPO / "app.toml").exists()  # replaced by project.toml


@pytest.mark.parametrize("job", sorted(UNITS))
def test_jobs_match_the_systemd_units(job):
    j = PROJECT["jobs"][job]
    timer = _unit(f"{UNITS[job]}.timer")
    service = _unit(f"{UNITS[job]}.service")
    on_calendar = set()
    for line in (REPO / "systemd" / f"{UNITS[job]}.timer").read_text().splitlines():
        if line.startswith("OnCalendar="):
            on_calendar |= _calendar(line.split("=", 1)[1])
    assert _calendar(j["schedule"]) == on_calendar
    assert timer["Timer"]["Persistent"] == "true"
    # Same command and arguments ("--local" may sit before or after the command).
    exec_argv = shlex.split(service["Service"]["ExecStart"])[1:]
    run_argv = shlex.split(j["run"])
    assert run_argv[0] == "fitdata"
    assert sorted(run_argv[1:]) == sorted(exec_argv) and "--local" in run_argv
    assert service["Service"]["SuccessExitStatus"] == "75"


@pytest.mark.parametrize("job", sorted(UNITS))
def test_jobs_run_write_commands_and_list_their_hosts(job):
    j = PROJECT["jobs"][job]
    command = shlex.split(j["run"])[1]
    assert describe.COMMAND_KINDS[command] == "write"  # jobs run with write access, never admin
    cli._parser().parse_args(shlex.split(j["run"])[1:])  # a command line fitdata accepts
    assert j["network"] and all(h.endswith(".garmin.com") for h in j["network"])
    assert re.fullmatch(r"[1-9][0-9]{0,6}[KMG]", j["memory"])
    assert re.fullmatch(r"[1-9][0-9]{0,5}[smh]", j["timeout"])
    assert set(j) <= {"run", "schedule", "after", "manual", "memory", "timeout", "network", "writes"}


def test_ui_table():
    ui = PROJECT["ui"]
    assert ui["data"] == "fitdata.sqlite" and ui["tables"] == ["days", "activities"]
    assert set(ui["refresh_after"]) == set(PROJECT["jobs"])
    assert sorted(ui["dashboards"]) == sorted(p.stem for p in (REPO / "ui" / "dashboards").glob("*.yaml"))
    assert set(ui) <= {"data", "tables", "accent", "dashboards", "refresh_after"}
