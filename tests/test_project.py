"""project.toml and apps/fitdata_garmin/app.toml (the mes project and its app, apps contract
section 8) and systemd/ (the standalone way) describe the same three jobs."""
from __future__ import annotations

import configparser
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from fitdata_garmin import cli, describe, index

REPO = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((REPO / "project.toml").read_text())
APP_DIR = REPO / "apps" / "fitdata_garmin"
APP = tomllib.loads((APP_DIR / "app.toml").read_text())
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
# job name -> systemd unit name (the fitness repo's own user units keep their names)
UNITS = {"activities": "fitdata-activities", "daily": "fitdata-daily", "backfill": "fitdata-backfill"}
JOB_KEYS = {"run", "schedule", "after", "manual", "webhook", "memory", "timeout", "network", "writes"}


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
    assert set(PROJECT) <= {"project", "tables", "views", "settings", "ui", "storage"}
    assert p["name"] == "fitdata" and re.fullmatch(r"[a-z][a-z0-9-]{1,30}", p["name"])
    assert p["title"] == "Fitness" and p["version"] and p["apps"] == ["fitdata_garmin"]
    assert set(p) == {"name", "title", "version", "apps"}
    assert not (REPO / "app.toml").exists()  # each app's app.toml lives in its package folder


def test_shared_tables():
    tables = PROJECT["tables"]
    assert list(tables) == ["days", "activities"]
    types = {"text", "integer", "real", "number", "boolean", "json"}
    for name, t in tables.items():
        assert set(t) <= {"columns", "key", "prefer"}
        assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,62}", c) and c != "source" for c in t["columns"])
        assert set(t["columns"].values()) <= types and 1 <= len(t["key"]) <= 4
        assert all(t["columns"][k] in ("text", "integer") for k in t["key"])
    assert tables["days"]["key"] == ["date"] and tables["activities"]["key"] == ["id"]
    assert tables["days"]["prefer"] == ["fitdata-garmin"] and "prefer" not in tables["activities"]
    assert tables["days"]["columns"]["training_status"] == "json"
    assert tables["days"]["columns"]["has_data"] == tables["activities"]["columns"]["complete"] == "boolean"


def test_app_toml():
    assert set(APP) <= {"app", "data", "jobs", "ui"}
    assert APP["app"] == {"name": "fitdata-garmin", "title": "Garmin archive", "version": "0.1.0",
                          "command": "fitdata-garmin"}
    assert APP["app"]["name"] == describe.APP_NAME == index.APP
    assert APP["data"] == {"folder": "garmin", "writes": ["days", "activities"], "tables": ["garmin_files"],
                           "rebuild": "index",
        "no_backup": ["data/.state/garmin-tokens.json"]}
    assert describe.COMMAND_KINDS[APP["data"]["rebuild"]] == "write"
    assert tuple(APP["data"]["writes"]) == index.SHARED and APP["data"]["tables"] == [index.FILES_TABLE]
    assert set(APP["data"]["writes"]) <= set(PROJECT["tables"])
    assert all(t.startswith("garmin_") for t in APP["data"]["tables"])  # the app's table prefix
    assert (APP_DIR / "__init__.py").is_file() and (APP_DIR / "AGENTS.md").is_file()


def test_the_console_scripts():
    scripts = PYPROJECT["project"]["scripts"]
    assert scripts == {"fitdata": "fitdata.command:main", "fitdata-garmin": "fitdata_garmin.cli:main"}
    assert APP["app"]["command"] in scripts and PROJECT["project"]["name"] in scripts
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/fitdata", "apps/fitdata_garmin"]
    assert "apps.lock.json" in (REPO / ".gitignore").read_text().splitlines()


@pytest.mark.parametrize("job", sorted(UNITS))
def test_jobs_match_the_systemd_units(job):
    j = APP["jobs"][job]
    timer = _unit(f"{UNITS[job]}.timer")
    service = _unit(f"{UNITS[job]}.service")
    on_calendar = set()
    for line in (REPO / "systemd" / f"{UNITS[job]}.timer").read_text().splitlines():
        if line.startswith("OnCalendar="):
            on_calendar |= _calendar(line.split("=", 1)[1])
    assert _calendar(j["schedule"]) == on_calendar
    assert timer["Timer"]["Persistent"] == "true"
    # The unit runs the app's own program with the job's words ("--local" may sit anywhere).
    exec_argv = shlex.split(service["Service"]["ExecStart"])
    assert exec_argv[0] == "%h/.local/bin/fitdata-garmin"
    run_argv = shlex.split(j["run"])
    assert sorted(run_argv) == sorted(exec_argv[1:]) and "--local" in run_argv
    assert service["Service"]["SuccessExitStatus"] == "75"
    # The same limits as the mes job, and the owner-only, no-new-privileges settings.
    assert service["Service"]["MemoryMax"] == j["memory"]
    assert service["Service"]["TimeoutStartSec"] == j["timeout"]
    assert service["Service"]["NoNewPrivileges"] == "yes"
    assert service["Service"]["UMask"] == "0077"


@pytest.mark.parametrize("job", sorted(UNITS))
def test_jobs_run_write_commands_and_list_their_hosts(job):
    j = APP["jobs"][job]
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,30}", job)
    command = shlex.split(j["run"])[0]
    assert command != "fitdata-garmin"  # run starts with the app's command, not its name
    assert describe.COMMAND_KINDS[command] == "write"  # jobs run with write access, never admin
    cli._parser().parse_args(shlex.split(j["run"]))  # a command line fitdata-garmin accepts
    assert j["network"] and all(h.endswith(".garmin.com") for h in j["network"])
    assert re.fullmatch(r"[1-9][0-9]{0,6}[KMG]", j["memory"])
    assert re.fullmatch(r"[1-9][0-9]{0,5}[smh]", j["timeout"])
    assert set(j) <= JOB_KEYS


def test_ui_table():
    ui = PROJECT["ui"]
    assert set(ui) <= {"accent", "dashboards", "tables", "refresh_after"}
    assert "data" not in ui and "tables" not in ui  # the allowlist is the shared views: days, activities
    assert ui["refresh_after"] == [f"fitdata-garmin.{j}" for j in APP["jobs"]]
    assert all(re.fullmatch(r"[a-z][a-z0-9-]{1,30}\.[a-z][a-z0-9-]{0,30}", r) for r in ui["refresh_after"])
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", ui["accent"])


def test_app_ui_table():
    ui = APP["ui"]
    assert set(ui) <= {"dashboards", "tables", "contributes"}
    for slug in ui["dashboards"]:
        assert (APP_DIR / "dashboards" / f"{slug}.yaml").is_file()
    for slug, c in ui["contributes"].items():
        assert set(c) == {"sections"} and re.fullmatch(r"dashboards/[a-z0-9][a-z0-9-]{0,60}\.yaml", c["sections"])
        assert (APP_DIR / c["sections"]).is_file()
        assert c["sections"] not in {f"dashboards/{s}.yaml" for s in ui["dashboards"]}
    assert set(ui.get("tables", [])) <= set(APP["data"]["tables"])


def _tracked() -> list[str]:
    import shutil
    import subprocess
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "-z"], capture_output=True, check=True).stdout
    return [e.decode("utf-8", "surrogateescape") for e in out.split(b"\0") if e]


def test_no_links_are_committed():
    """`mes deploy` refuses a project with any link in it (link_in_project): git mode 120000."""
    entries = _tracked()
    assert entries
    links = [e.split("\t", 1)[1] for e in entries if e.startswith("120000 ")]
    assert links == [], f"links in git (mes deploy refuses them): {links}"
    on_disk = [n for n in (e.split("\t", 1)[1] for e in entries) if (REPO / n).is_symlink()]
    assert on_disk == [], f"tracked files that are links on disk: {on_disk}"


def test_claude_md_imports_agents_md():
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@AGENTS.md" in text.splitlines()
