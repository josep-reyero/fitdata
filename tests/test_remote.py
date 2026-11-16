"""Issue 2: remote mode runs `ssh host "<command string>"`; the host, the command and every argument
must reach the other machine exactly as typed, whatever that machine's shell is."""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from fitdata_garmin import cli
from fitdata_garmin.validate import BadInput

NASTY = ["--json", "sql", "SELECT 'it''s' -- $(touch /tmp/pwned) `id` \\' ; rm -rf ~ \n \"x\" * ? !"]


@pytest.fixture
def fake_ssh(monkeypatch, archive):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.setenv("FITDATA_REMOTE", "my-server")
    from fitdata_garmin import config
    config.load.cache_clear()
    return calls


def test_forward_shape(fake_ssh, run):
    assert run(*NASTY)[0] == 0
    (argv,) = fake_ssh
    assert argv[:3] == ["ssh", "--", "my-server"] and len(argv) == 4
    words = argv[3].split(" ")
    assert words[:2] == ["~/.local/bin/fitdata", cli.FORWARDED] and len(words) == 3
    assert re.fullmatch(r"[A-Za-z0-9_-]+", words[2])  # nothing any shell would interpret
    assert cli._decode_argv(words[2]) == NASTY


def test_login_gets_a_terminal(fake_ssh, run):
    run("login")
    assert fake_ssh[0][:4] == ["ssh", "-t", "--", "my-server"]


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/pwned", "-p", "my server", "host;id", "$(id)", ""])
def test_bad_host_refused(fake_ssh, run, monkeypatch, host):
    monkeypatch.setenv("FITDATA_REMOTE", host)
    from fitdata_garmin import config
    config.load.cache_clear()
    code, _, err = run("summary")
    if host == "":
        assert code == 0 and not fake_ssh  # empty means "stay local"
        return
    assert code == 2 and not fake_ssh and "host" in err


@pytest.mark.parametrize("command", ["fitdata; rm -rf ~", "$(id)", "fit`id`", "-x", "a  b", "fitdata\n"])
def test_bad_remote_command_refused(command):
    with pytest.raises(BadInput):
        cli._remote_command(["summary"], command)


@pytest.mark.parametrize("command", ["~/.local/bin/fitdata", "/opt/fitdata/bin/fitdata",
                                     "uv --directory ~/fitdata-garmin run fitdata"])
def test_ordinary_remote_commands_allowed(command):
    assert cli._remote_command(["summary"], command).startswith(command + " ")


def test_round_trip_through_a_real_shell(tmp_path, archive):
    """Run the remote string with /bin/sh, as sshd would, and check fitdata gets the exact argv."""
    fake = tmp_path / "bin" / "fitdata"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\0" "$a"; done\n')
    fake.chmod(0o755)
    remote = cli._remote_command(NASTY, "fitdata")
    out = subprocess.run(["/bin/sh", "-c", remote], capture_output=True, check=True,
                         env={**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}"}).stdout
    received = out.decode().split("\0")[:-1]
    assert received[0] == cli.FORWARDED and cli._decode_argv(received[1]) == NASTY


def test_forwarded_request_runs_locally(fake_ssh, run):
    """The receiving side unpacks the request and never forwards it again."""
    token = cli._encode_argv(["--json", "sql", "SELECT count(*) AS n FROM days"])
    code, out, _ = run(cli.FORWARDED, token)
    assert code == 0 and json.loads(out) == [{"n": 1}] and not fake_ssh


@pytest.mark.parametrize("argv", [[cli.FORWARDED], [cli.FORWARDED, "not base64!"], [cli.FORWARDED, "e30"],
                                  [cli.FORWARDED, cli._encode_argv(["summary"]), "extra"]])
def test_damaged_forwarded_request_refused(fake_ssh, run, argv):
    code, out, err = run(*argv)
    assert code == 2 and out == "" and "same version" in err and not fake_ssh


def test_forwarded_decode_is_refused(fake_ssh, run, archive):
    """The sender never forwards decode, so a forwarded one could only be used to read any file."""
    token = cli._encode_argv(["decode", str(archive / ".state" / "garmin-tokens.json")])
    code, out, err = run(cli.FORWARDED, token)
    assert code == 5 and out == "" and "decode" in err and "Traceback" not in err and not fake_ssh  # refused


def test_huge_days_is_a_plain_error(run):
    code, out, err = run("--json", "summary", "--days", "999999999")
    assert code == 2 and out == "" and "--days" in err and "Traceback" not in err


def test_value_errors_are_plain(run, tmp_path):
    bad = tmp_path / "not.fit"
    bad.write_text("hello")
    code, out, err = run("decode", str(bad))
    assert code == 2 and "Traceback" not in err and "could not use" in err
