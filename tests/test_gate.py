"""scripts/fitdata-ssh-gate: the forced command for an SSH key. It must run fitdata only for the one
line fitdata itself sends (`<command> --forwarded-argv <base64url>`), with MES_ALLOW set from its
own argument, and refuse everything else without running anything."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fitdata_garmin import cli

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "fitdata-ssh-gate"
SHELLS = [s for s in ("/bin/sh", "/bin/dash", "/bin/bash") if Path(s).exists()]

RECORDER = """#!/bin/sh
{{ for a in "$@"; do printf '%s\\n' "$a"; done; echo ---; env | sort; }} > "{record}"
"""


@pytest.fixture
def gate(tmp_path):
    """The gate copied into a folder with a fake fitdata next to it that records how it was run."""
    folder = tmp_path / "bin"
    folder.mkdir()
    shutil.copy(GATE, folder / "fitdata-ssh-gate")
    record = tmp_path / "record.txt"
    (folder / "fitdata").write_text(RECORDER.format(record=record))
    (folder / "fitdata").chmod(0o755)
    (folder / "fitdata-ssh-gate").chmod(0o755)
    return folder / "fitdata-ssh-gate", record


def _run(gate_path, original, *args, shell="/bin/sh", env=None):
    e = {"PATH": "/usr/bin:/bin", "HOME": str(gate_path.parent.parent)}
    if original is not None:
        e["SSH_ORIGINAL_COMMAND"] = original
    e.update(env or {})
    return subprocess.run([shell, str(gate_path), *args], env=e, capture_output=True, text=True, timeout=30,
                          cwd=gate_path.parent.parent)


TOKEN = cli._encode_argv(["--json", "summary"])

HOSTILE = [
    None,                                                    # no command: an interactive shell
    "",
    "sh",
    "bash -i",
    "scp -t ~/.ssh/authorized_keys",
    "rsync --server -e.LsfxC . /",
    f"fitdata --forwarded-argv {TOKEN}; touch PWNED",
    f"fitdata --forwarded-argv {TOKEN} && touch PWNED",
    f"fitdata --forwarded-argv {TOKEN}|touch PWNED",
    "fitdata --forwarded-argv $(touch PWNED)",
    "fitdata --forwarded-argv `touch PWNED`",
    f"fitdata --forwarded-argv {TOKEN}\ntouch PWNED",
    f"fitdata --forwarded-argv {TOKEN}\rtouch PWNED",
    f"fitdata\t--forwarded-argv {TOKEN}",
    f"fitdata  --forwarded-argv {TOKEN}",
    f" fitdata --forwarded-argv {TOKEN}",
    f"fitdata --forwarded-argv {TOKEN} ",
    f"fitdata --forwarded-argv {TOKEN} --json",
    f"fitdata --forwarded-argv={TOKEN}",
    f"fitdata --forwarded-argv {TOKEN}=",
    f"fitdata --forwarded-argv {TOKEN}é",
    f"fitdata --forwarded-argv ../{TOKEN}",
    "fitdata --forwarded-argv",
    "fitdata --forwarded-argv ",
    f"--forwarded-argv {TOKEN}",
    f"fitdata sql {TOKEN}",
    "fitdata --json sql SELECT",
    f"fitdata --forwarded-argv {'A' * 70000}",
    f"uv run fitdata --forwarded-argv {TOKEN}",
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("original", HOSTILE)
def test_hostile_commands_run_nothing(gate, shell, original):
    gate_path, record = gate
    r = _run(gate_path, original, "read", shell=shell)
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert not record.exists() and not (gate_path.parent.parent / "PWNED").exists()
    assert r.stderr.startswith("fitdata-ssh-gate: ") and r.stdout == ""


@pytest.mark.parametrize("args", [(), ("admin",), ("",), ("read", "write"), ("READ",), ("read;id",)])
def test_the_gate_needs_exactly_one_known_level(gate, args):
    gate_path, record = gate
    r = _run(gate_path, f"fitdata --forwarded-argv {TOKEN}", *args)
    assert r.returncode == 1 and not record.exists()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("level", ["read", "write"])
def test_fitdatas_own_request_runs_with_the_level_and_a_clean_environment(gate, shell, level):
    gate_path, record = gate
    r = _run(gate_path, f"~/anything/at/all/fitdata --forwarded-argv {TOKEN}", level, shell=shell,
             env={"MES_ALLOW": "admin", "LC_EVIL": "x", "PYTHONPATH": "/tmp/evil", "FITDATA_REMOTE": "h",
                  "FITDATA_CONFIG": "/cfg.toml", "LD_PRELOAD": "/tmp/evil.so"})
    assert r.returncode == 0, r.stderr
    argv, env = record.read_text().split("---\n")
    assert argv.splitlines() == ["--forwarded-argv", TOKEN]
    env = dict(line.split("=", 1) for line in env.splitlines() if "=" in line)
    assert env["MES_ALLOW"] == level and env["MES_VIA_GATE"] == "1" and env["FITDATA_CONFIG"] == "/cfg.toml"
    assert not {"LC_EVIL", "PYTHONPATH", "FITDATA_REMOTE", "LD_PRELOAD", "SSH_ORIGINAL_COMMAND"} & set(env)


def test_the_gate_with_the_real_fitdata(tmp_path, archive):
    """End to end: a read key can query, cannot write, cannot decode; a write key can index."""
    folder = tmp_path / "gatebin"
    folder.mkdir()
    shutil.copy(GATE, folder / "fitdata-ssh-gate")
    (folder / "fitdata-ssh-gate").chmod(0o755)
    (folder / "fitdata").write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -c '
                                    '"import sys; from fitdata_garmin.cli import main; sys.exit(main(sys.argv[1:]))" "$@"\n')
    (folder / "fitdata").chmod(0o755)
    env = {"FITDATA_DATA_DIR": os.environ["FITDATA_DATA_DIR"], "FITDATA_CONFIG": os.environ["FITDATA_CONFIG"]}

    def ask(level, argv):
        return _run(folder / "fitdata-ssh-gate", f"fitdata {cli.FORWARDED} {cli._encode_argv(argv)}", level, env=env)

    r = ask("read", ["--json", "sql", "SELECT count(*) AS n FROM days"])
    assert r.returncode == 0 and json.loads(r.stdout) == [{"n": 1}], r.stderr
    r = ask("read", ["--json", "index"])
    assert r.returncode == 4 and json.loads(r.stderr)["kind"] == "not_allowed"
    r = ask("read", ["--json", "login"])
    assert r.returncode == 4
    r = ask("read", ["decode", str(archive / ".state" / "garmin-tokens.json")])
    assert r.returncode == 5 and "FAKE-TOKEN" not in r.stdout + r.stderr
    r = ask("write", ["--json", "index"])
    assert r.returncode == 0, r.stderr
    r = ask("write", ["--json", "login"])
    assert r.returncode == 4


def test_the_gate_prefers_fitdata_garmin(gate, tmp_path):
    """With both programs next to it (today's install), the gate runs fitdata-garmin directly."""
    gate_path, record = gate
    other = tmp_path / "other.txt"
    (gate_path.parent / "fitdata-garmin").write_text(RECORDER.format(record=other))
    (gate_path.parent / "fitdata-garmin").chmod(0o755)
    r = _run(gate_path, f"fitdata --forwarded-argv {TOKEN}", "read")
    assert r.returncode == 0, r.stderr
    assert other.read_text().split("---\n")[0].splitlines() == ["--forwarded-argv", TOKEN] and not record.exists()


def test_the_gate_without_any_program_runs_nothing(gate):
    gate_path, record = gate
    (gate_path.parent / "fitdata").unlink()
    r = _run(gate_path, f"fitdata --forwarded-argv {TOKEN}", "read")
    assert r.returncode == 1 and "fitdata-garmin or fitdata" in r.stderr and not record.exists()


VENV_BIN = REPO / ".venv" / "bin"


@pytest.mark.parametrize("names", [("fitdata",), ("fitdata", "fitdata-garmin")])
def test_the_gate_through_the_installed_programs(tmp_path, archive, names):
    """End to end with the programs `uv sync` installs, linked next to the gate as the README says:
    an older install with only `fitdata` (the project command, which hands the request to
    fitdata-garmin) and today's with both. Read stays read; nothing extra reaches stdout."""
    if not all((VENV_BIN / n).exists() for n in ("fitdata", "fitdata-garmin")):
        pytest.skip("the project's .venv has no fitdata programs (run: uv sync)")
    folder = tmp_path / "gatebin"
    folder.mkdir()
    shutil.copy(GATE, folder / "fitdata-ssh-gate")
    (folder / "fitdata-ssh-gate").chmod(0o755)
    for n in names:
        (folder / n).symlink_to(VENV_BIN / n)
    env = {"FITDATA_DATA_DIR": os.environ["FITDATA_DATA_DIR"], "FITDATA_CONFIG": os.environ["FITDATA_CONFIG"]}

    def ask(level, argv):
        return _run(folder / "fitdata-ssh-gate", f"fitdata {cli.FORWARDED} {cli._encode_argv(argv)}", level, env=env)

    r = ask("read", ["--json", "sql", "SELECT count(*) AS n FROM days"])
    assert r.returncode == 0 and json.loads(r.stdout) == [{"n": 1}] and r.stderr == "", r.stderr
    r = ask("read", ["days", "--days", "3"])  # no --json: still no note from the project command
    assert r.returncode == 0 and "Note:" not in r.stderr, r.stderr
    r = ask("read", ["--json", "index"])
    assert r.returncode == 4 and json.loads(r.stderr)["kind"] == "not_allowed"
