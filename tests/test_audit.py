"""Regression tests for the security audit (B3 to B10). Each one reproduces the attack or the
failure first, then checks that it no longer works. Nothing here talks to Garmin or ssh."""
from __future__ import annotations

import fcntl
import http.server
import io
import json
import logging
import os
import sqlite3
import stat
import threading
import zipfile
from datetime import date, timedelta

import pytest

from conftest import DAY, TOKEN_TEXT, use
from fakegarmin import FakeApi, FakeSession, activity
from fitdata_garmin import cli, client, config, index, paths, query, storage, sync
from fitdata_garmin.errors import Refused, Temporary

TODAY = date.today().isoformat()


def _sync_one() -> None:
    sync.sync_recent(FakeSession(FakeApi([activity(77, f"{TODAY} 07:00:00")])), days=2)
    index.rebuild()


def _save_token() -> None:
    paths.STATE.mkdir(parents=True, exist_ok=True)
    paths.TOKENS.write_text(json.dumps({"token": TOKEN_TEXT}))


# ------------------------------------------------------------------ B3: migrate-storage

@pytest.fixture
def files_archive(tmp_path, monkeypatch):
    root = tmp_path / "data"
    use(tmp_path, monkeypatch, config_text=f'data_dir = "{root}"\n')
    _save_token()
    _sync_one()
    return root


def _proc(root):
    return root / paths.day_key(TODAY) / "processed"


@pytest.mark.parametrize("plant", ["hard link to token", "symlink outside", "symlink to token", "tmp leftover",
                                   "odd name", "odd folder", "pipe"])
def test_migrate_refuses_anything_it_would_not_copy_as_is(files_archive, cli_run, tmp_path, plant):
    proc = _proc(files_archive)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE")
    {
        "hard link to token": lambda: os.link(paths.TOKENS, proc / "leak.json"),
        "symlink outside": lambda: os.symlink(outside, proc / "leak.json"),
        "symlink to token": lambda: os.symlink(paths.TOKENS, proc / "leak.json"),
        "tmp leftover": lambda: (proc / "sleep.json.tmp").write_text("{}"),
        "odd name": lambda: (proc / "my notes.json").write_text("{}"),
        "odd folder": lambda: (proc / ".hidden").mkdir(),
        "pipe": lambda: os.mkfifo(proc / "leak.json"),
    }[plant]()
    cfg_before = (tmp_path / "config.toml").read_text()
    code, out, err = cli_run("--json", "migrate-storage", "--to", "bucket")
    assert code == 5 and out == ""
    e = json.loads(err)
    assert e["kind"] == "refused" and "Nothing was copied" in e["error"]
    assert "garmin/days/" in e["error"] and str(files_archive) not in e["error"]
    assert "rm -rf" not in err
    assert (tmp_path / "config.toml").read_text() == cfg_before
    assert not (files_archive / "store").exists()  # nothing was copied
    assert config.load().storage == "files"


def test_migrate_lists_at_most_twenty_problems(files_archive, cli_run):
    proc = _proc(files_archive)
    for i in range(25):
        (proc / f"x{i}.json.tmp").write_text("{}")
    code, _, err = cli_run("migrate-storage", "--to", "bucket")
    assert code == 5 and "25 thing(s)" in err and "and 5 more" in err


def test_migrate_gives_the_removal_hint_only_when_every_file_was_copied(files_archive, cli_run, monkeypatch):
    pytest.importorskip("mybucket")
    from fitdata_garmin import migrate
    real = migrate._raw_files
    monkeypatch.setattr(migrate, "_raw_files", lambda: (real()[0] + 1, []))  # one file the copy missed
    code, out, err = cli_run("--json", "migrate-storage", "--to", "bucket")
    result = json.loads(out)
    assert code == 0 and result["remove_source_with"] is None and "rm -rf" not in out + err
    assert "does not suggest removing it" in result["message"]


def test_migrate_after_the_links_are_gone_copies_and_never_takes_the_token(files_archive, cli_run):
    pytest.importorskip("mybucket")
    leak = _proc(files_archive) / "leak.json"
    os.link(paths.TOKENS, leak)
    assert cli_run("migrate-storage", "--to", "bucket")[0] == 5
    leak.unlink()
    code, out, _ = cli_run("--json", "migrate-storage", "--to", "bucket")
    result = json.loads(out)
    assert code == 0 and result["remove_source_with"].startswith("rm -rf -- ")
    a = storage.archive()
    assert not any(TOKEN_TEXT.encode() in (a.read(e.key) or b"") for e in a.walk("garmin"))


def test_migrate_does_not_claim_a_switch_that_did_not_happen(tmp_path, monkeypatch, cli_run):
    pytest.importorskip("mybucket")
    root = tmp_path / "data"
    use(tmp_path, monkeypatch, config_text=f'data_dir = "{root}"\nnote = """\nstorage = "files"\n"""\n')
    _sync_one()
    code, out, err = cli_run("--json", "migrate-storage", "--to", "bucket")
    assert code == 5 and out == ""
    e = json.loads(err)
    assert "copied" in e["error"] and "still read from" in e["error"] and "rm -rf" not in err
    assert config.load().storage == "files"


# ------------------------------------------------------------------ B4: set_storage

def test_set_storage_refuses_a_storage_line_inside_a_multiline_string(tmp_path, monkeypatch):
    text = 'data_dir = "/x"\nnote = """\nstorage = "files"\n"""\n'
    use(tmp_path, monkeypatch, config_text=text)
    with pytest.raises(Refused, match="multi-line"):
        config.set_storage("bucket")
    assert (tmp_path / "config.toml").read_text() == text
    assert config.load().storage == "files"


def test_set_storage_writes_through_a_symlinked_config(tmp_path, monkeypatch):
    use(tmp_path, monkeypatch, config_text="")
    real = tmp_path / "dotfiles" / "fitdata.toml"
    real.parent.mkdir()
    real.write_text('data_dir = "/x"\n')
    real.chmod(0o640)
    link = tmp_path / "config.toml"
    link.unlink()
    link.symlink_to(real)
    config.set_storage("bucket")
    assert link.is_symlink() and real.read_text() == 'data_dir = "/x"\nstorage = "bucket"\n'
    assert real.stat().st_mode & 0o777 == 0o640
    assert config.load().storage == "bucket"
    assert not list(real.parent.glob("*.tmp")) and not list(tmp_path.glob("*.tmp"))


def test_set_storage_never_writes_through_a_planted_temp_file(tmp_path, monkeypatch):
    use(tmp_path, monkeypatch, config_text='data_dir = "/x"\n')
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "config.toml.tmp").symlink_to(victim)
    config.set_storage("bucket")
    assert victim.read_text() == "keep"
    assert config.load().storage == "bucket"


# ------------------------------------------------------------------ B5: sql

class _Recording(sqlite3.Connection):
    log: list[str] = []

    def execute(self, sql, *args):
        _Recording.log.append(sql)
        return super().execute(sql, *args)

    def set_authorizer(self, fn):
        _Recording.log.append("<authorizer>")
        return super().set_authorizer(fn)


def test_sql_keeps_temporary_data_in_memory(archive, monkeypatch):
    _Recording.log = []
    def db():
        conn = sqlite3.connect(paths.DB, factory=_Recording)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(query, "_db", db)
    query.sql("SELECT date FROM days ORDER BY date")
    log = _Recording.log
    assert "PRAGMA temp_store = MEMORY" in log
    assert log.index("PRAGMA temp_store = MEMORY") < log.index("<authorizer>")
    assert any(s.startswith("PRAGMA hard_heap_limit") for s in log)


def test_only_one_sql_at_a_time(run, archive):
    lock = paths.STATE / "sql.lock"
    with open(lock, "a") as fh:  # another `fitdata sql` holds it
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out, err = run("--json", "sql", "SELECT 1 AS x")
        assert code == 75 and out == "" and json.loads(err)["kind"] == "temporary"
        assert "Another fitdata sql" in json.loads(err)["error"]
        assert run("--json", "day", DAY)[0] == 0  # other read commands are not held up
    code, out, _ = run("--json", "sql", "SELECT 1 AS x")
    assert code == 0 and json.loads(out) == [{"x": 1}]
    assert stat.S_IMODE(lock.stat().st_mode) & 0o077 == 0


def test_sql_lock_is_not_followed_through_a_link(run, archive, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    os.symlink(victim, paths.STATE / "sql.lock")
    code, _, err = run("sql", "SELECT 1")
    assert code == 5 and "Traceback" not in err and str(tmp_path) not in err
    assert victim.read_text() == "keep"


# ------------------------------------------------------------------ B6: memory and size caps

def _zip(*entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def test_unzip_caps_one_file_and_says_so(tmp_path, monkeypatch, caplog):
    assert sync.UNZIP_MAX_FILE_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(sync, "UNZIP_MAX_FILE_BYTES", 1000)
    caplog.set_level(logging.WARNING, "fitdata")
    with pytest.raises(ValueError, match="1000|0 MB for one file"):
        sync._unzip(_zip(("ok.fit", b"1" * 500), ("big.fit", b"0" * 5000)), tmp_path / "raw")
    assert "'big.fit'" in caplog.text and "did not save it" in caplog.text
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == ["ok.fit"]


class _Body(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        size = int(self.path.strip("/").split("?")[0])
        self.send_response(200)
        if "nolength" not in self.path:
            self.send_header("Content-Length", str(size))
        self.end_headers()
        for _ in range(size // 65536):
            self.wfile.write(b"0" * 65536)
        self.wfile.write(b"0" * (size % 65536))

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Body)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_large_answers_are_dropped_before_they_fill_memory(local_server, monkeypatch):
    import requests
    monkeypatch.setattr(client, "MAX_RESPONSE_BYTES", 1024 * 1024)
    s = requests.Session()
    s.hooks["response"].append(client._limit_response)
    assert len(s.get(f"{local_server}/1000").content) == 1000
    for path in ("/3000000", "/3000000?nolength"):
        with pytest.raises(client.ResponseTooLarge, match="larger than 1 MB"):
            s.get(local_server + path)


def test_the_cap_is_installed_on_garminconnect(monkeypatch):
    from garminconnect import Garmin
    api = Garmin()  # nothing is sent until login
    client._cap_responses(api)
    assert client._limit_response in api.client._api_session.hooks["response"]


# ------------------------------------------------------------------ B7: broken list entries

class _ListApi(FakeApi):
    def __init__(self, entries, good):
        super().__init__(good)
        self.entries = entries

    def get_activities_by_date(self, start, end, sortorder="asc"):
        return self.entries


def test_one_broken_activity_entry_does_not_stop_the_sync(tmp_path, monkeypatch, caplog):
    use(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "_skipped_days", set())
    good = activity(500, "2020-01-05 07:00:00")
    future = (date.today() + timedelta(days=30)).isoformat()
    entries = [
        "not an activity",
        {"activityId": "1/../../x", "startTimeLocal": "2020-01-05 08:00:00"},
        {"activityId": 501},                                                  # no start time
        {"activityId": 502, "startTimeLocal": "garbage"},
        {"activityId": 503, "startTimeLocal": f"{future} 07:00:00"},          # far in the future
        {"activityId": 504, "startTimeLocal": "2020-01-06 07:00:00", "activityType": "running"},
        {"activityId": True, "startTimeLocal": "2020-01-05 09:00:00"},
        good,
    ]
    caplog.set_level(logging.WARNING, "fitdata")
    s = FakeSession(_ListApi(entries, [good]))
    assert sync.sync_activities(s, date(2020, 1, 10), 10) == 1
    a = storage.archive()
    assert storage.find_activity(a, 500)
    assert caplog.text.count("Skipped one entry of Garmin's activity list") == 7
    assert not (paths.DAYS / future[:4] / future).exists()
    # The days with a skipped entry are not marked complete; the others are.
    assert sync.sync_day(s, date(2020, 1, 6))["complete"] is False
    assert sync.sync_day(s, date(2020, 1, 5))["complete"] is False  # the '1/../../x' and True entries
    assert sync.sync_day(s, date(2020, 1, 7))["complete"] is True


def test_a_list_that_is_not_a_list_is_logged(tmp_path, monkeypatch, caplog):
    use(tmp_path, monkeypatch)
    caplog.set_level(logging.WARNING, "fitdata")
    assert sync.sync_activities(FakeSession(_ListApi({"oops": 1}, [])), date(2020, 1, 10), 3) == 0
    assert "was not a list" in caplog.text


# ------------------------------------------------------------------ B8: index column names

def test_odd_manifest_keys_do_not_reach_the_index_sql(archive, caplog):
    """The columns are fixed by project.toml: keys of a manifest (data from Garmin) never become
    columns or SQL, and a key spelled like a column in other letters changes nothing."""
    ddir = paths.day_dir(DAY)
    m = json.loads((ddir / "day.json").read_text())
    m["headline"].update({'x" TEXT); DROP TABLE garmin_files; --': 1, "Steps": 2, "a" * 65: 3, "ok_field": 4})
    (ddir / "day.json").write_text(json.dumps(m))
    index.rebuild()
    cols = query.schema()["tables"]
    expected = [c[0] for c in index.expected_columns()["days"]]
    assert "garmin_files" in cols and cols["days"] == expected
    assert query.sql("SELECT steps FROM days") == [{"steps": 1234}]


# ------------------------------------------------------------------ B10: odds and ends

@pytest.fixture
def restore_umask():
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


def test_new_files_are_owner_only(tmp_path, monkeypatch, cli_run, restore_umask):
    os.umask(0o022)
    root = use(tmp_path, monkeypatch)
    assert cli_run("index")[0] == 0
    for p in (paths.DB, paths.LOGS / "fitdata.log"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    assert stat.S_IMODE(paths.LOGS.stat().st_mode) == 0o700
    assert root.exists()


def test_read_errors_do_not_show_the_data_folder(tmp_path, monkeypatch, cli_run):
    root = use(tmp_path, monkeypatch)
    code, _, err = cli_run("--json", "sql", "SELECT 1")
    assert code == 3 and str(root) not in err and str(tmp_path) not in err
    code, _, err = cli_run("--json", "schema")
    assert code == 3 and str(tmp_path) not in err


def test_bucket_not_found_does_not_show_the_store_folder(tmp_path, monkeypatch, cli_run):
    pytest.importorskip("mybucket")
    use(tmp_path, monkeypatch, "bucket")
    code, _, err = cli_run("--json", "day", DAY)
    assert code == 3 and str(tmp_path) not in err


class _NoGarmin:
    def __init__(self, *a, **k):
        raise AssertionError("Garmin must not be contacted with a damaged login")


@pytest.mark.parametrize("content", ['{"di_token": "abc', "[1, 2]", "", '{"di_token": 123}', '{"di_token": ""}',
                                     '{"x": 1}', '{"di_token": "a", "di_refresh_token": 5}', "\x00\xff"])
def test_a_damaged_login_needs_a_new_login(tmp_path, monkeypatch, cli_run, content):
    use(tmp_path, monkeypatch)
    paths.STATE.mkdir(parents=True)
    paths.TOKENS.write_text(content)
    monkeypatch.setattr(client, "Garmin", _NoGarmin)
    code, out, err = cli_run("--json", "sync")
    assert code == 6 and json.loads(out)["status"] == "needs_human" and "fitdata login" in json.loads(out)["action"]
    assert "damaged" in json.loads(out)["why"]


def test_a_login_garminconnect_cannot_read_needs_a_new_login(tmp_path, monkeypatch, cli_run):
    from garminconnect import GarminConnectConnectionError
    use(tmp_path, monkeypatch)
    paths.STATE.mkdir(parents=True)
    paths.TOKENS.write_text('{"di_token": "x"}')

    class Broken:
        def __init__(self, *a, **k):
            pass

        def login(self, tokenstore=None):
            raise GarminConnectConnectionError("Login failed: Token path not loading cleanly: boom")
    monkeypatch.setattr(client, "Garmin", Broken)
    assert cli_run("sync")[0] == 6


def test_garmin_unreachable_is_still_temporary(tmp_path, monkeypatch, cli_run):
    from garminconnect import GarminConnectConnectionError
    use(tmp_path, monkeypatch)
    paths.STATE.mkdir(parents=True)
    paths.TOKENS.write_text('{"di_token": "x"}')

    class Offline:
        def __init__(self, *a, **k):
            pass

        def login(self, tokenstore=None):
            raise GarminConnectConnectionError("Login failed: connection refused")
    monkeypatch.setattr(client, "Garmin", Offline)
    assert cli_run("sync")[0] == 75


def test_symlinked_sync_lock_is_not_truncated(tmp_path, monkeypatch, cli_run):
    use(tmp_path, monkeypatch)
    paths.STATE.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    os.symlink(victim, paths.LOCK)
    monkeypatch.setattr(client, "Garmin", _NoGarmin)
    code, _, err = cli_run("sync")
    assert code == 5 and "symbolic link" in err and victim.read_text() == "keep"


def test_lock_helper_reports_busy_as_temporary(tmp_path):
    from fitdata_garmin.safefs import hold_lock
    lock = tmp_path / ".state" / "x.lock"
    with hold_lock(lock):
        with pytest.raises(Temporary):
            with hold_lock(lock):
                pass
    with hold_lock(lock):
        pass


def test_project_toml_lists_only_hosts_the_jobs_reach():
    import tomllib
    data = tomllib.loads((cli.desc.APP_TOML).read_text())
    for job in data["jobs"].values():
        # mobile.integration.garmin.com is only reached by a password sign-in (`fitdata login`).
        assert "mobile.integration.garmin.com" not in job["network"]
