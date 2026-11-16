"""The project command `fitdata` (src/fitdata/command.py): finding the project and its apps, the
built-ins, and handing every other command to the right app's own program, so that every way
fitdata was run before keeps working (apps contract, section 5)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fitdata import command

REPO = Path(__file__).resolve().parents[1]
VENV_BIN = REPO / ".venv" / "bin"
NOTE = ('Note: "fitdata {w}" was passed to fitdata-garmin, the only app of this project. '
        'Write "fitdata garmin {w}" so it keeps working when the project has more apps.\n')

DESCRIBE_SCRIPT = """#!/bin/sh
if [ "$1 $2" = "--json describe" ]; then
  printf '%s\\n' '{{"app": "{name}", "commands": [{{"name": "hello", "kind": "read", "help": "says hello"}}]}}'
elif [ "$1 $2" = "--json guide" ]; then
  printf '%s\\n' '{{"app": "{name}", "guide": "# {name} made its own guide"}}'
else
  exit 9
fi
"""


class Exec(Exception):
    def __init__(self, path, argv):
        super().__init__(path)
        self.path, self.argv = path, argv


def _app(root: Path, package: str, name: str, title: str = "An app", guide: str | None = None,
         command_line: str | None = None) -> Path:
    folder = root / "apps" / package
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text("")
    (folder / "app.toml").write_text(f'[app]\nname = "{name}"\ntitle = "{title}"\n'
                                     + (command_line or f'command = "{name}"\n'))
    if guide is not None:
        (folder / "AGENTS.md").write_text(guide)
    return folder


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A made-up project folder with one app and a bin folder holding its program."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.toml").write_text('[project]\nname = "fitdata"\ntitle = "Fitness"\nversion = "0.1.0"\n'
                                       'apps = ["fitdata_garmin"]\n')
    (root / "AGENTS.md").write_text("# The project's guide\n")
    _app(root, "fitdata_garmin", "fitdata-garmin", "Garmin archive", guide="# Garmin guide\n")
    bin_ = tmp_path / "venv" / "bin"
    bin_.mkdir(parents=True)
    for name in ("fitdata-garmin", "fitdata-strava"):
        (bin_ / name).write_text(DESCRIBE_SCRIPT.format(name=name))
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("MES_PROJECT_DIR", str(root))
    monkeypatch.setattr(sys, "executable", str(bin_ / "python"))
    monkeypatch.delenv("MES_ALLOW", raising=False)
    monkeypatch.setenv("MES_COMMAND_NAME", "x")
    monkeypatch.delenv("MES_COMMAND_NAME")  # restored (unset) after the test
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setattr(command.tempfile, "gettempdir", lambda: str(notes))

    def fake_execv(path, argv):
        raise Exec(path, argv)
    monkeypatch.setattr(command.os, "execv", fake_execv)
    return root, bin_


def _dispatch(argv, capsys):
    with pytest.raises(Exec) as e:
        command.main(argv)
    out, err = capsys.readouterr()
    return Path(e.value.path).name, e.value.argv[1:], out, err


def _second_app(root: Path) -> None:
    (root / "project.toml").write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "fitdata_strava"]\n')
    _app(root, "fitdata_strava", "fitdata-strava", "Strava")


# ------------------------------------------------------------------ handing commands to the app

@pytest.mark.parametrize("argv, note_word", [
    (["sync", "--days", "3", "--local"], "sync --days 3 --local"),
    (["status"], "status"),
    (["login"], "login"),
    (["--local", "sync"], "--local sync"),          # leading options fall through too
    (["-v", "backfill", "--max-days", "90"], "-v backfill --max-days 90"),
])
def test_old_command_lines_reach_the_only_app_unchanged_with_a_note(project, capsys, argv, note_word):
    program, args, out, err = _dispatch(argv, capsys)
    assert program == "fitdata-garmin" and args == argv
    assert out == "" and err == NOTE.format(w=note_word)
    program, args, out, err = _dispatch(argv, capsys)  # at most once per terminal session
    assert program == "fitdata-garmin" and args == argv and out == "" and err == ""


@pytest.mark.parametrize("argv", [["sync", "--help"], ["login", "-h"]])
def test_no_note_when_asking_for_help(project, capsys, argv):
    program, args, out, err = _dispatch(argv, capsys)
    assert program == "fitdata-garmin" and args == argv and err == ""


def test_with_one_app_help_and_describe_carry_its_commands(project, capsys):
    """Readers of the old `fitdata --help` and `fitdata --json describe` still find the commands."""
    assert command.main(["--help"]) == 0
    assert "fitdata garmin hello  says hello" in capsys.readouterr().out
    assert command.main(["--json", "describe"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["commands"] == [{"name": "hello", "kind": "read", "help": "says hello"}]
    _second_app(project[0])
    assert command.main(["--json", "describe"]) == 0
    assert "commands" not in json.loads(capsys.readouterr().out)


def test_the_app_is_told_how_it_was_reached(project, capsys):
    _dispatch(["garmin", "status"], capsys)
    assert os.environ["MES_COMMAND_NAME"] == "fitdata garmin"


@pytest.mark.parametrize("argv", [["--json", "days", "--days", "7"], ["days", "--json"], ["--json", "summary"]])
def test_with_json_nothing_is_added(project, capsys, argv):
    program, args, out, err = _dispatch(argv, capsys)
    assert program == "fitdata-garmin" and args == argv and out == "" and err == ""


@pytest.mark.parametrize("argv, expected", [
    (["garmin", "sync", "--days", "3"], ["sync", "--days", "3"]),
    (["--json", "garmin", "sync"], ["--json", "sync"]),
    (["fitdata-garmin", "status"], ["status"]),
    (["garmin", "describe"], ["describe"]),     # the app's own describe, not the project's
    (["garmin"], []),
])
def test_the_short_or_full_app_name_selects_the_app(project, capsys, argv, expected):
    program, args, out, err = _dispatch(argv, capsys)
    assert program == "fitdata-garmin" and args == expected and err == ""


def test_a_forwarded_ssh_request_goes_to_fitdata_garmin_without_a_note(project, capsys):
    _second_app(project[0])  # even with more apps: only fitdata-garmin sends these requests
    program, args, out, err = _dispatch(["--forwarded-argv", "WyJkYXlzIl0"], capsys)
    assert program == "fitdata-garmin" and args == ["--forwarded-argv", "WyJkYXlzIl0"] and err == ""


def test_the_environment_and_mes_allow_are_passed_unchanged(project, capsys, monkeypatch):
    """The app checks its own kinds: the project command never decides for it."""
    monkeypatch.setenv("MES_ALLOW", "read")
    program, args, _, _ = _dispatch(["garmin", "sync"], capsys)
    assert program == "fitdata-garmin" and os.environ["MES_ALLOW"] == "read"


def test_programs_come_only_from_the_environments_bin(project, capsys, monkeypatch, tmp_path):
    root, bin_ = project
    (bin_ / "fitdata-garmin").unlink()
    elsewhere = tmp_path / "path"
    elsewhere.mkdir()
    (elsewhere / "fitdata-garmin").write_text("#!/bin/sh\n")
    (elsewhere / "fitdata-garmin").chmod(0o755)
    monkeypatch.setenv("PATH", str(elsewhere))
    assert command.main(["--json", "status"]) == 3
    err = json.loads(capsys.readouterr().err)
    assert err["kind"] == "not_found" and "not installed in the project's environment" in err["error"]
    assert err["hint"] == "Run: uv sync"


def test_several_apps_need_the_apps_name(project, capsys):
    _second_app(project[0])
    assert command.main(["sync"]) == 2
    err = capsys.readouterr().err
    assert '"sync" is not one of the apps of fitdata (garmin, strava), so nothing was run.' in err
    assert "fitdata garmin --help" in err
    assert command.main(["stava", "hello"]) == 2
    assert 'Did you mean "fitdata strava hello"?' in capsys.readouterr().err
    assert command.main(["--json", "sync"]) == 2
    assert json.loads(capsys.readouterr().err)["exit_code"] == 2
    program, args, _, _ = _dispatch(["strava", "hello"], capsys)
    assert program == "fitdata-strava" and args == ["hello"]


# ------------------------------------------------------------------ built-ins

def test_help_lists_the_builtins_and_the_apps(project, capsys):
    for argv in ([], ["help"], ["--help"], ["-h"]):
        assert command.main(argv) == 0
        out = capsys.readouterr().out
        assert "fitdata describe" in out and "fitdata garmin <command>" in out and "Garmin archive" in out


def test_describe(project, capsys):
    assert command.main(["--json", "describe"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["project"] == "fitdata" and d["command"] == "fitdata" and d["allowed"] == "admin" and d["jobs"] == []
    assert d["exit_codes"]["75"] == "temporary failure, retry"
    (app,) = d["apps"]
    assert app == {"name": "fitdata-garmin", "short": "garmin", "package": "fitdata_garmin", "title": "Garmin archive",
                   "program": "fitdata-garmin", "describe": {"app": "fitdata-garmin",
                   "commands": [{"name": "hello", "kind": "read", "help": "says hello"}]}}
    assert command.main(["--json"]) == 0 and json.loads(capsys.readouterr().out) == d
    assert command.main(["describe"]) == 0
    out = capsys.readouterr().out
    assert "garmin: Garmin archive (fitdata-garmin)" in out and "hello" in out


def test_describe_fails_when_an_app_does_not_answer(project, capsys):
    (project[1] / "fitdata-garmin").write_text("#!/bin/sh\necho not json\n")
    assert command.main(["--json", "describe"]) == 1
    assert json.loads(capsys.readouterr().err)["kind"] == "app_broken"


def test_guide_joins_the_projects_and_each_apps(project, capsys):
    _second_app(project[0])  # fitdata-strava has no AGENTS.md: its program's guide is used
    assert command.main(["--json", "guide"]) == 0
    g = json.loads(capsys.readouterr().out)
    assert g["guide"] == ("# The project's guide\n\n## Garmin archive (fitdata garmin ...)\n\n# Garmin guide\n\n"
                          "## Strava (fitdata strava ...)\n\n# fitdata-strava made its own guide\n")
    assert g["parts"] == [{"source": "project", "file": "AGENTS.md"}, {"source": "fitdata-garmin", "file": "AGENTS.md"},
                          {"source": "fitdata-strava", "file": "generated"}]


def test_apps(project, capsys):
    assert command.main(["apps", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["apps"] == [
        {"name": "fitdata-garmin", "short": "garmin", "package": "fitdata_garmin", "title": "Garmin archive",
         "program": "fitdata-garmin", "location": "in-project"}]


@pytest.mark.parametrize("value", ["", "  ", "root", "everything"])
def test_builtins_check_mes_allow(project, capsys, monkeypatch, value):
    monkeypatch.setenv("MES_ALLOW", value)
    for word in command.BUILTINS:
        assert command.main(["--json", word]) == 4
        assert json.loads(capsys.readouterr().err)["kind"] == "not_allowed"


def test_builtins_run_at_read(project, capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    assert command.main(["--json", "describe"]) == 0 and json.loads(capsys.readouterr().out)["allowed"] == "read"


def test_builtins_take_only_json(project, capsys):
    assert command.main(["describe", "--all"]) == 2 and "takes no arguments" in capsys.readouterr().err


# ------------------------------------------------------------------ finding the project and its apps

def test_the_project_folder_is_the_parent_of_the_environment(project, monkeypatch, tmp_path):
    monkeypatch.delenv("MES_PROJECT_DIR")
    monkeypatch.setattr(sys, "prefix", str(project[0] / ".venv"))
    assert command.load().root == project[0]
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere" / ".venv"))
    with pytest.raises(command.Problem, match="could not find its project folder"):
        command.load()


def test_mes_project_dir_must_hold_project_toml(project, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MES_PROJECT_DIR", str(tmp_path))
    assert command.main(["status"]) == 1 and "has no project.toml" in capsys.readouterr().err


def test_an_app_outside_the_apps_folder_needs_the_lock(project, capsys):
    (project[0] / "project.toml").write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "other_app"]\n')
    assert command.main(["--json", "status"]) == 3
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == ("The app other_app is not inside this project's apps folder and the list of installed "
                            "apps has not been written yet.") and err["hint"] == "Run: mes apps sync"


def _lock(root: Path, apps: list[dict], **over) -> None:
    body = {"v": 1, "project": "fitdata",
            "project_toml_sha256": hashlib.sha256((root / "project.toml").read_bytes()).hexdigest(), "apps": apps}
    body.update(over)
    (root / "apps.lock.json").write_text(json.dumps(body))


def test_an_installed_app_is_found_through_the_lock(project, tmp_path, capsys):
    root, _ = project
    installed = _app(tmp_path / "site-packages", "fitdata_strava", "fitdata-strava", "Strava")
    (root / "project.toml").write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "fitdata_strava"]\n')
    _lock(root, [{"package": "fitdata_garmin", "name": "fitdata-garmin", "path": str(root / "apps" / "fitdata_garmin"),
                  "in_project": True, "program": "fitdata-garmin"},
                 {"package": "fitdata_strava", "name": "fitdata-strava", "path": str(installed),
                  "in_project": False, "program": "fitdata-strava"}])
    p = command.load()
    assert [(a.name, a.in_project, a.folder) for a in p.apps] == [
        ("fitdata-garmin", True, root / "apps" / "fitdata_garmin"), ("fitdata-strava", False, installed)]
    assert command.main(["apps", "--json"]) == 0
    assert [a["location"] for a in json.loads(capsys.readouterr().out)["apps"]] == ["in-project", "installed"]


GOOD = {"package": "fitdata_garmin", "name": "fitdata-garmin", "in_project": True, "program": "fitdata-garmin"}


@pytest.mark.parametrize("change", [
    {"project_toml_sha256": "0" * 64}, {"apps": []}, {"apps": [dict(GOOD, package="other_app")]},
])
def test_a_lock_out_of_date_only_because_project_toml_changed_is_ignored_with_every_app_in_apps(project, change):
    """Every app is in apps/, so the project is read exactly as without a lock (the same rule as mes)."""
    root, _ = project
    apps = [dict(a, path=a.get("path", str(root / "apps" / "fitdata_garmin"))) for a in change.pop("apps", [GOOD])]
    _lock(root, apps, **change)
    assert [a.name for a in command.load().apps] == ["fitdata-garmin"]


def test_a_changed_project_toml_with_an_installed_app_still_needs_mes_apps_sync(project, tmp_path, capsys):
    root, _ = project
    installed = _app(tmp_path / "site-packages", "fitdata_strava", "fitdata-strava", "Strava")
    (root / "project.toml").write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "fitdata_strava"]\n')
    _lock(root, [dict(GOOD, path=str(root / "apps" / "fitdata_garmin")),
                 {"package": "fitdata_strava", "name": "fitdata-strava", "path": str(installed),
                  "in_project": False, "program": "fitdata-strava"}], project_toml_sha256="0" * 64)
    assert command.main(["--json", "status"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"].startswith("project.toml changed after the list of apps")
    assert err["hint"].startswith("Run: mes apps sync")


def test_two_apps_may_not_name_the_same_program(project, capsys):
    root, _ = project
    _app(root, "fitdata_strava", "fitdata-strava", "Strava", command_line='command = "fitdata-garmin"\n')
    (root / "project.toml").write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "fitdata_strava"]\n')
    assert command.main(["--json", "apps"]) == 1
    assert "both name the program" in json.loads(capsys.readouterr().err)["error"]


@pytest.mark.parametrize("change", [
    {"v": 2}, {"project": "other"},
    {"apps": [dict(GOOD, path="relative/apps/fitdata_garmin")]},
    {"apps": [dict(GOOD, path="/x/../apps/fitdata_garmin")]},
    {"apps": [dict(GOOD, path="/does/not/exist")]},
    {"apps": [dict(GOOD, name="fitdata-other")]},
    {"apps": [dict(GOOD, program="python3")]},
    {"apps": [dict(GOOD, in_project="yes")]},
])
def test_a_stale_or_odd_lock_is_refused(project, capsys, change):
    root, _ = project
    apps = [dict(GOOD, path=str(root / "apps" / "fitdata_garmin"))]
    if "apps" in change:
        apps = [dict(a, path=a.get("path", str(root / "apps" / "fitdata_garmin"))) for a in change.pop("apps")]
    _lock(root, apps, **change)
    assert command.main(["--json", "status"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"].startswith(command.STALE_LOCK) and err["hint"] == "Run: mes apps sync"


def test_a_lock_that_is_not_json_is_refused(project, capsys):
    (project[0] / "apps.lock.json").write_text("{not json")
    assert command.main(["status"]) == 1 and command.STALE_LOCK in capsys.readouterr().err


def test_an_in_project_app_must_be_in_the_projects_apps_folder(project, tmp_path, capsys):
    root, _ = project
    other = _app(tmp_path / "elsewhere", "fitdata_garmin", "fitdata-garmin")
    _lock(root, [dict(GOOD, path=str(other))])
    assert command.main(["status"]) == 1 and "its folder is elsewhere" in capsys.readouterr().err


def test_links_are_never_followed(project, tmp_path, capsys):
    root, _ = project
    real = tmp_path / "real-app.toml"
    real.write_text('[app]\nname = "fitdata-garmin"\ncommand = "fitdata-garmin"\n')
    app_toml = root / "apps" / "fitdata_garmin" / "app.toml"
    app_toml.unlink()
    app_toml.symlink_to(real)
    assert command.main(["status"]) == 1 and "is a link" in capsys.readouterr().err
    app_toml.unlink()
    app_toml.write_text(real.read_text())
    (root / "project.toml").rename(tmp_path / "project.toml")
    (root / "project.toml").symlink_to(tmp_path / "project.toml")
    assert command.main(["status"]) == 1 and "is a link" in capsys.readouterr().err


def test_an_app_folder_that_is_a_link_is_not_used(project, tmp_path, capsys):
    root, _ = project
    folder = root / "apps" / "fitdata_garmin"
    folder.rename(tmp_path / "moved")
    folder.symlink_to(tmp_path / "moved")
    assert command.main(["status"]) == 3 and "not inside this project's apps folder" in capsys.readouterr().err


def test_large_files_are_not_read(project, capsys):
    (project[0] / "apps" / "fitdata_garmin" / "app.toml").write_text("# x\n" * 20000)
    assert command.main(["status"]) == 1 and "larger than 64 KB" in capsys.readouterr().err


@pytest.mark.parametrize("text, message", [
    ('[project]\nname = "fitdata"\napps = ["fitdata_garmin"]\n[apps.fitdata-garmin]\ncli = "x"\n',
     "project.toml still has the old [apps.<name>] section."),
    ('[project]\nname = "fitdata"\napps = ["fitdata_garmin"]\n[jobs.daily]\nrun = "x"\n',
     "project.toml still has [jobs.daily]. Jobs now belong to the app that runs them"),
    ('[project]\nname = "fitdata"\napps = ["fitdata_garmin"]\n[ui]\ndata = "fitdata.sqlite"\n',
     "[ui] data is no longer used: every project has one shared database, fitdata.sqlite, in its data folder."),
    ('[project]\nname = "fitdata"\n', "project.toml does not list its apps"),
    ('[project]\nname = "fitdata"\napps = ["Bad-Name"]\n', "[project] apps in project.toml must list"),
    ('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "fitdata_garmin"]\n', "each once"),
    ('[project]\nname = "../x"\napps = ["fitdata_garmin"]\n', "is not a name fitdata can use"),
    ("[project\n", "is not valid TOML"),
])
def test_old_or_broken_project_toml_is_refused_in_plain_words(project, capsys, text, message):
    (project[0] / "project.toml").write_text(text)
    assert command.main(["status"]) == 1 and message in capsys.readouterr().err


@pytest.mark.parametrize("app_toml, message", [
    ('[app]\nname = "fitdata"\ncommand = "fitdata"\n', "not the project's own name"),
    ('[app]\nname = "fitdata-help"\ncommand = "fitdata-help"\n', "one of the project command's own words"),
    ('[app]\nname = "fitdata-garmin"\ncommand = "bash"\n', "is not a program name fitdata can run"),
    ('[app]\nname = "fitdata-garmin"\ncommand = "../../bin/sh"\n', "is not a program name fitdata can run"),
    ('[app]\nname = "fitdata-garmin"\n', "exactly one"),
    ('[app]\nname = "fitdata-garmin"\ncommand = "fitdata-garmin"\ncli = "x.cli:app"\n', "exactly one"),
    ('[app]\nname = "fitdata-garmin"\ncli = "os; rm -rf /"\n', "is not of the form module:object"),
    ('[app]\nname = "fitdata-garmin"\ntitle = "two\\nlines"\ncommand = "fitdata-garmin"\n', "one line of text"),
    ('[data]\nfolder = "garmin"\n', "has no [app] table"),
])
def test_odd_app_toml_is_refused(project, capsys, app_toml, message):
    (project[0] / "apps" / "fitdata_garmin" / "app.toml").write_text(app_toml)
    assert command.main(["status"]) == 1 and message in capsys.readouterr().err


def test_a_cli_app_runs_the_program_named_like_the_app(project, capsys):
    (project[0] / "apps" / "fitdata_garmin" / "app.toml").write_text(
        '[app]\nname = "fitdata-garmin"\ncli = "fitdata_garmin.cli:app"\n')
    program, args, _, _ = _dispatch(["garmin", "x"], capsys)
    assert program == "fitdata-garmin" and args == ["x"]


def test_two_apps_may_not_share_a_name(project, capsys):
    root, _ = project
    (root / "project.toml").write_text('[project]\nname = "fitdata"\napps = ["fitdata_garmin", "garmin"]\n')
    _app(root, "garmin", "garmin")  # its name is fitdata-garmin's short name
    assert command.main(["status"]) == 1 and "would both be called 'garmin'" in capsys.readouterr().err


# ------------------------------------------------------------------ this repository, for real

def test_this_repository(monkeypatch):
    monkeypatch.delenv("MES_PROJECT_DIR", raising=False)
    p = command.load(REPO)
    (app,) = p.apps
    assert (p.name, p.title, app.name, app.short, app.program, app.in_project) == (
        "fitdata", "Fitness", "fitdata-garmin", "garmin", "fitdata-garmin", True)


def test_the_project_command_uses_only_the_standard_library():
    tree = ast.parse((REPO / "src" / "fitdata" / "command.py").read_text())
    imported = {(n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) and n.level == 0 else
                ("." if isinstance(n, ast.ImportFrom) else None) for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)}
    imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported.discard(".")
    assert imported and imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


def _installed():
    if not all((VENV_BIN / n).exists() for n in ("fitdata", "fitdata-garmin")):
        pytest.skip("the project's .venv has no fitdata programs (run: uv sync)")


def test_the_installed_fitdata_gives_the_same_answers_as_fitdata_garmin(archive, tmp_path):
    """Through the real console scripts: every existing way of asking keeps its output."""
    _installed()
    env = {k: v for k, v in os.environ.items() if k not in ("MES_PROJECT_DIR", "MES_COMMAND_NAME")}
    (tmp_path / "tmp").mkdir()
    env["TMPDIR"] = str(tmp_path / "tmp")  # the once-per-session note mark

    def run(program, *argv):
        r = subprocess.run([str(VENV_BIN / program), *argv], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    for argv in (["--json", "days", "--days", "7"], ["--json", "summary"], ["days", "--json", "--from", "2026-11-01"]):
        code, out, err = run("fitdata", *argv)
        assert (code, out, err) == run("fitdata-garmin", *argv)[:2] + ("",)
    code, out, err = run("fitdata", "status")
    assert code == 0 and "days:" in out and err == NOTE.format(w="status")
    code, out, err = run("fitdata", "garmin", "--json", "sql", "SELECT count(*) AS n FROM days")
    assert code == 0 and json.loads(out) == [{"n": 1}] and err == ""
    code, out, err = run("fitdata", "--json", "describe")
    d = json.loads(out)
    assert code == 0 and d["apps"][0]["describe"] == json.loads(run("fitdata-garmin", "--json", "describe")[1])
    code, out, err = run("fitdata", "--json", "no-such-command")
    assert code == 2 and json.loads(err)["kind"] == "bad_input" and out == ""
    code, out, err = run("fitdata", "guide")
    assert code == 0 and out.startswith("# Fitness (fitdata)") and "## Garmin archive (fitdata garmin ...)" in out


def test_remote_requests_go_through_both_project_commands(archive, tmp_path):
    """A laptop's `fitdata days` reaches the archive machine's `~/.local/bin/fitdata` (the default
    [remote] command), which is now the project command there: the answer is the archive's, and
    the only note is the one the laptop prints for its own command line."""
    _installed()
    bin_ = tmp_path / "home" / ".local" / "bin"
    bin_.mkdir(parents=True)
    for n in ("fitdata", "fitdata-garmin"):
        (bin_ / n).symlink_to(VENV_BIN / n)
    fake_ssh = tmp_path / "ssh-bin" / "ssh"
    fake_ssh.parent.mkdir()
    # Like sshd: the last argument is one command line for the remote login shell. Locally the
    # remote side is the same machine, with FITDATA_REMOTE unset so it reads its own archive.
    calls = tmp_path / "ssh-calls.txt"
    fake_ssh.write_text(f'#!/bin/sh\nfor last; do :; done\nprintf "%s\\n" "$last" >> "{calls}"\n'
                        'unset FITDATA_REMOTE\nexec /bin/sh -c "$last"\n')
    fake_ssh.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("MES_PROJECT_DIR", "MES_COMMAND_NAME")}
    (tmp_path / "tmp").mkdir()
    env.update(HOME=str(tmp_path / "home"), PATH=f"{fake_ssh.parent}:/usr/bin:/bin", FITDATA_REMOTE="archive-host",
               TMPDIR=str(tmp_path / "tmp"))

    def run(*argv):
        r = subprocess.run([str(VENV_BIN / "fitdata"), *argv], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr
    code, out, err = run("--json", "sql", "SELECT count(*) AS n FROM days")
    assert code == 0 and json.loads(out) == [{"n": 1}] and err == ""
    code, out, err = run("days", "--from", "2026-11-14", "--to", "2026-11-14")
    assert code == 0 and "2026-11-14" in out and err == NOTE.format(w="days --from 2026-11-14 --to 2026-11-14")
    sent = calls.read_text().splitlines()
    assert len(sent) == 2 and all(s.startswith("~/.local/bin/fitdata --forwarded-argv ") for s in sent)
