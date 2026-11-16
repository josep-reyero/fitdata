"""The storage interface (storage.py): plain files or a my-bucket-storage bucket, same keys, same
guarantees. Bucket tests are skipped when mybucket is not installed. Nothing here talks to Garmin."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

from conftest import TOKEN_TEXT, use
from fakegarmin import FakeApi, FakeSession, activity
from fitdata_garmin import config, index, paths, query, storage, sync
from fitdata_garmin.errors import NotFound
from fitdata_garmin.validate import BadInput

TODAY = date.today().isoformat()
AID = 424242


def _sync(n_activities: int = 1) -> FakeApi:
    api = FakeApi([activity(AID + i, f"{TODAY} 07:{10 + i:02d}:00") for i in range(n_activities)])
    sync.sync_recent(FakeSession(api), days=2)
    index.rebuild()
    return api


def _save_token() -> None:
    paths.STATE.mkdir(parents=True, exist_ok=True)
    paths.TOKENS.write_text(json.dumps({"token": TOKEN_TEXT}))


def _hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------ keys

@pytest.mark.parametrize("key", ["garmin/days/2026/2026-11-14/day.json", "garmin/account/profile.json",
                                 "garmin/days/2026/2026-11-14/activities/0700-running-1/raw/1_ACTIVITY.fit",
                                 "garmin"])
def test_good_keys(key):
    assert storage.check_key(key) == key


@pytest.mark.parametrize("key", [".state/garmin-tokens.json", "garmin/../.state/garmin-tokens.json",
                                 "garmin/.state/x", "garmin//x", "/garmin/x", "garmin/x/", "garmin/a b",
                                 "logs/fitdata.log", "fitdata.sqlite", "garmin/x.json.tmp", "", "garmin/café",
                                 "garmin/" + "a" * 600, "garmin/x\n", None, 5])
def test_bad_keys_refused(key):
    with pytest.raises(BadInput):
        storage.check_key(key)


# ------------------------------------------------------------------ both backends

def test_sync_query_and_index_through_the_backend(backend, cli_run):
    _save_token()
    _sync()
    a = storage.archive()
    assert a.kind == backend
    dkey = paths.day_key(TODAY)
    akey = storage.find_activity(a, AID)
    assert akey and akey.startswith(f"{dkey}/activities/") and akey.endswith(f"-{AID}")
    # Raw FIT stored, records decoded from it by Garmin's SDK, processed sources stored.
    assert a.exists(f"{akey}/raw/{AID}_ACTIVITY.fit")
    assert a.read(f"{akey}/derived/records.csv").decode().count("\n") == 6  # header + 5 samples

    code, out, _ = cli_run("--json", "day", TODAY)
    day = json.loads(out)
    assert code == 0 and day["headline"]["steps"] == 4321 and day["raw_files"] == [f"{TODAY.replace('-', '')}_WELLNESS.fit"]
    assert day["activities"] == [akey.rsplit("/", 1)[1]]
    assert day["dir"] == (str(paths.ROOT / dkey) if backend == "files" else f"bucket:archive/{dkey}")
    code, out, _ = cli_run("--json", "day", TODAY, "--source", "sleep")
    assert json.loads(out)["dailySleepDTO"]["sleepTimeSeconds"] == 25200
    code, out, _ = cli_run("--json", "activity", str(AID), "--records", "--columns", "heart_rate")
    assert code == 0 and json.loads(out) == [{"heart_rate": str(120 + i)} for i in range(5)]
    code, out, _ = cli_run("--json", "activity", str(AID))
    info = json.loads(out)
    assert "derived/laps.csv" in info["files"] and "summary" in info["sources_with_data"]
    code, out, _ = cli_run("--json", "sql", "SELECT fit, records FROM activities")
    assert json.loads(out) == [{"fit": f"{akey}/raw/{AID}_ACTIVITY.fit", "records": 5}]
    code, out, _ = cli_run("--json", "sql", "SELECT count(*) AS n FROM garmin_files WHERE kind = 'raw/wellness'")
    assert json.loads(out) == [{"n": 2}]
    code, out, _ = cli_run("--json", "status")
    st = json.loads(out)
    assert st["days"] == 2 and st["activities"] == 1 and st["problem"] is None


def test_both_backends_hold_the_same_keys(tmp_path, monkeypatch):
    pytest.importorskip("mybucket")
    keys = {}
    for kind in ("files", "bucket"):
        use(tmp_path / kind, monkeypatch, kind)
        _sync(2)
        keys[kind] = [(e.key, e.size) for e in storage.archive().walk("garmin")]
        storage.close_all()
    files_keys = [k for k, _ in keys["files"]]
    assert files_keys == [k for k, _ in keys["bucket"]] and len(files_keys) > 40
    # Byte for byte the same, except the manifests, which hold the time of the sync.
    sizes = dict(keys["files"])
    assert all(sizes[k] == s for k, s in keys["bucket"] if not k.endswith(("day.json", "activity.json")))


def test_read_commands_open_the_archive_read_only(backend, cli_run, monkeypatch):
    _sync()
    storage.close_all()
    opened = []
    real = storage.open_archive
    monkeypatch.setattr(storage, "open_archive", lambda kind, write=False: opened.append(write) or real(kind, write))
    for argv in (["day", TODAY], ["activity", str(AID), "--records"], ["status"], ["summary"]):
        assert cli_run("--json", *argv)[0] == 0
    assert opened and not any(opened)
    with pytest.raises(Exception, match="read-only"):
        storage.archive().write(f"{paths.day_key(TODAY)}/x.json", b"{}")


def test_writes_are_refused_outside_garmin(backend):
    a = storage.archive(write=True)
    for key in (".state/garmin-tokens.json", "garmin/../.state/garmin-tokens.json", "fitdata.sqlite"):
        with pytest.raises(BadInput):
            a.write(key, b"x")


def test_missing_things_are_not_found(backend, cli_run):
    _sync()
    assert cli_run("day", "2001-01-01")[0] == 3
    assert cli_run("activity", "999")[0] == 3
    assert cli_run("day", TODAY, "--source", "naps")[0] == 0  # stored by the fake as a made-up dict


# ------------------------------------------------------------------ the saved login

def test_bucket_never_holds_the_login(tmp_path, monkeypatch, cli_run):
    mybucket = pytest.importorskip("mybucket")
    use(tmp_path, monkeypatch, "bucket")
    _save_token()
    _sync()
    storage.close_all()
    b = mybucket.open_store(config.load().store_dir, readonly=True).bucket("archive")
    keys = [o.key for o in b.list().objects]
    assert keys and all(k.startswith("garmin/") for k in keys)
    assert not any(TOKEN_TEXT.encode() in b.get(k) for k in keys)
    # A read command cannot name the token: the key is refused before the bucket is asked.
    code, out, err = cli_run("--json", "day", TODAY, "--source", "../../../../../.state/garmin-tokens")
    assert code == 2 and TOKEN_TEXT not in out + err


def test_symlink_into_state_is_never_listed_or_copied(tmp_path, monkeypatch, cli_run):
    pytest.importorskip("mybucket")
    root = use(tmp_path, monkeypatch, config_text="")
    _save_token()
    _sync()
    proc = paths.day_dir(TODAY) / "processed"
    os.symlink(paths.TOKENS, proc / "leak.json")
    assert all(not e.key.endswith("leak.json") for e in storage.archive().walk("garmin"))
    # The copy would not match what is on disk, so migrate-storage refuses and names the link.
    code, _, err = cli_run("--json", "migrate-storage", "--to", "bucket")
    assert code == 5 and "processed/leak.json" in json.loads(err)["error"] and TOKEN_TEXT not in err
    assert config.load().storage == "files"
    (proc / "leak.json").unlink()
    assert cli_run("--json", "migrate-storage", "--to", "bucket")[0] == 0
    a = storage.archive()
    assert a.kind == "bucket" and not a.exists(f"{paths.day_key(TODAY)}/processed/leak.json")
    assert not any(TOKEN_TEXT.encode() in (a.read(e.key) or b"") for e in a.walk("garmin"))
    assert (root / ".state" / "garmin-tokens.json").exists()  # the login stays where it was


# ------------------------------------------------------------------ migrate-storage

CONFIG = """# my settings
data_dir = "{root}"

[remote]
command = "~/.local/bin/fitdata"
"""


def test_migrate_round_trip(tmp_path, monkeypatch, cli_run):
    pytest.importorskip("mybucket")
    root = tmp_path / "data"
    use(tmp_path, monkeypatch, config_text=CONFIG.format(root=root))
    _save_token()
    _sync(2)
    before = _hashes(root / "garmin")
    rows_before = query.sql("SELECT * FROM garmin_files ORDER BY path")

    code, out, err = cli_run("--json", "migrate-storage", "--to", "bucket")
    result = json.loads(out)
    assert code == 0 and result["changed"] and result["verified"] == result["objects"] == len(before)
    assert result["remove_source_with"] == f"rm -rf -- {root / 'garmin'}"
    cfg_text = (tmp_path / "config.toml").read_text()
    assert cfg_text.startswith("# my settings\n") and 'storage = "bucket"\n[remote]' in cfg_text
    assert config.load().storage == "bucket"
    assert _hashes(root / "garmin") == before  # the source is untouched
    assert query.sql("SELECT * FROM garmin_files ORDER BY path") == rows_before  # same index from the bucket
    code, out, _ = cli_run("--json", "day", TODAY)
    assert json.loads(out)["dir"].startswith("bucket:archive/")

    # A sync now writes into the bucket only.
    _sync(3)
    a = storage.archive()
    new = storage.find_activity(a, AID + 2)
    assert new and not (root / new).exists()

    code, out, _ = cli_run("--json", "migrate-storage", "--to", "files")
    result = json.loads(out)
    assert code == 0 and result["to"] == "files" and result["remove_source_with"].startswith("mybucket --store ")
    assert config.load().storage == "files" and (root / new / "activity.json").exists()
    assert 'storage = "files"' in (tmp_path / "config.toml").read_text()
    code, out, _ = cli_run("--json", "migrate-storage", "--to", "files")
    assert code == 0 and json.loads(out)["changed"] is False


def test_migrate_refuses_to_switch_when_a_copy_does_not_verify(tmp_path, monkeypatch, cli_run):
    pytest.importorskip("mybucket")
    root = tmp_path / "data"
    use(tmp_path, monkeypatch, config_text=CONFIG.format(root=root))
    _sync()
    before = _hashes(root / "garmin")
    real_read = storage.BucketArchive.read

    def damaged(self, key):
        data = real_read(self, key)
        return data + b"!" if key.endswith("sleep.json") else data
    monkeypatch.setattr(storage.BucketArchive, "read", damaged)
    code, out, err = cli_run("--json", "migrate-storage", "--to", "bucket")
    assert code == 5 and json.loads(err)["kind"] == "refused" and "did not switch" in json.loads(err)["error"]
    assert "storage" not in (tmp_path / "config.toml").read_text()
    assert config.load().storage == "files" and _hashes(root / "garmin") == before


def test_migrate_waits_for_a_running_sync(tmp_path, monkeypatch, cli_run):
    import fcntl
    use(tmp_path, monkeypatch)
    _sync()
    paths.STATE.mkdir(parents=True, exist_ok=True)
    with open(paths.LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _, err = cli_run("migrate-storage", "--to", "bucket")
    assert code == 75 and "Another fitdata sync" in err
    assert config.load().storage == "files"


def test_without_mybucket_bucket_mode_says_how_to_install(tmp_path, monkeypatch, cli_run):
    use(tmp_path, monkeypatch)
    _sync()
    monkeypatch.setitem(sys.modules, "mybucket", None)  # as if it were not installed
    code, _, err = cli_run("migrate-storage", "--to", "bucket")
    assert code == 1 and "fitdata[bucket]" not in err and "--extra bucket" in err
    assert config.load().storage == "files"
    monkeypatch.setenv("FITDATA_STORAGE", "bucket")
    config.load.cache_clear()
    code, _, err = cli_run("day", TODAY)
    assert code == 1 and "not installed" in err and "Traceback" not in err


def test_bucket_that_does_not_exist_yet_is_not_found(tmp_path, monkeypatch, cli_run):
    pytest.importorskip("mybucket")
    use(tmp_path, monkeypatch, "bucket")
    code, _, err = cli_run("day", TODAY)
    assert code == 3 and "does not exist yet" in err
    assert not (config.load().store_dir).exists()  # a read never creates the store


# ------------------------------------------------------------------ config

def test_unknown_storage_is_a_config_error(tmp_path, monkeypatch, cli_run):
    use(tmp_path, monkeypatch, config_text='storage = "s3"\n')
    code, _, err = cli_run("summary")
    assert code == 1 and '"files" and "bucket"' in err


@pytest.mark.parametrize("store", ["{root}/.state/store", "{root}/garmin/store"])
def test_store_inside_state_or_files_is_refused(tmp_path, monkeypatch, cli_run, store):
    root = tmp_path / "data"
    use(tmp_path, monkeypatch, config_text=f'storage = "bucket"\n[bucket]\nstore = "{store.format(root=root)}"\n')
    code, _, err = cli_run("summary")
    assert code == 1 and "will not use it" in err


@pytest.mark.parametrize("before, after", [
    ("", 'storage = "bucket"\n'),
    ('data_dir = "/x"\n', 'data_dir = "/x"\nstorage = "bucket"\n'),
    ('storage = "files"  # was\ndata_dir = "/x"\n', 'storage = "bucket"\ndata_dir = "/x"\n'),
    ('data_dir = "/x"\n[remote]\nhost = "h"\n', 'data_dir = "/x"\nstorage = "bucket"\n[remote]\nhost = "h"\n'),
    ('# storage = "files"\n[bucket]\nname = "storage"\n', '# storage = "files"\nstorage = "bucket"\n[bucket]\nname = "storage"\n'),
    ('data_dir = "/x"', 'data_dir = "/x"\nstorage = "bucket"\n'),
])
def test_set_storage_keeps_the_rest_of_the_file(tmp_path, monkeypatch, before, after):
    use(tmp_path, monkeypatch, config_text=before)
    (tmp_path / "config.toml").chmod(0o600)
    config.set_storage("bucket")
    assert (tmp_path / "config.toml").read_text() == after
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600


def test_rate_limit_stops_the_run_and_keeps_the_day_incomplete(tmp_path, monkeypatch):
    from fitdata_garmin.client import RateLimited
    use(tmp_path, monkeypatch)

    class Limited(FakeSession):
        def call(self, fn, *args, **kwargs):
            if self.calls >= 3:
                raise RateLimited("limited")
            return super().call(fn, *args, **kwargs)
    with pytest.raises(RateLimited):
        sync.sync_day(Limited(), date(2020, 1, 1))
    # The first sources were kept, but no manifest says the day is done.
    a = storage.archive()
    assert a.exists(f"{paths.day_key('2020-01-01')}/processed/daily_summary.json")
    assert a.read(f"{paths.day_key('2020-01-01')}/day.json") is None
    with pytest.raises(NotFound):
        query.day("2020-01-01")
