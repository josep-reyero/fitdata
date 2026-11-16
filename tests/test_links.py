"""Links in the archive (audit B1, B2): read commands must never print the saved login or any file
outside <data_dir>/garmin, and syncs must never write outside it, whatever links the archive holds
(for example after a restore). Every access goes name by name from the opened garmin folder and
never follows a link; only regular files with one name that are not the login are read."""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

import pytest

from conftest import ACTIVITY_ID, DAY, TOKEN_TEXT
from fakegarmin import FakeApi, FakeSession, activity
from fitdata_garmin import index, paths, query, storage, sync
from fitdata_garmin.errors import NotFound, Refused

SECRET = "OUTSIDE-SECRET-DO-NOT-LEAK"


def _proc() -> Path:
    return paths.day_dir(DAY) / "processed"


def _no_leak(*texts: str) -> None:
    for t in texts:
        assert TOKEN_TEXT not in t and SECRET not in t


def _case_variant_state() -> Path:
    """.STATE: the same folder as .state on a case-insensitive disk (macOS), nothing elsewhere."""
    return paths.ROOT / ".STATE"


# ------------------------------------------------------------------ B1: the saved login

def test_hard_link_to_token_is_not_read(run, archive):
    os.link(paths.TOKENS, _proc() / "hrv.json")
    code, out, err = run("--json", "day", DAY, "--source", "hrv")
    assert code == 3
    _no_leak(out, err)
    records = paths.find_activity_dir(ACTIVITY_ID) / "derived" / "records.csv"
    records.unlink()
    os.link(paths.TOKENS, records)
    code, out, err = run("--json", "activity", ACTIVITY_ID, "--records")
    assert code == 3
    _no_leak(out, err)


def test_hard_link_to_token_is_not_listed_or_indexed(run, archive, caplog):
    os.link(paths.TOKENS, _proc() / "hrv.json")
    caplog.set_level(logging.WARNING, "fitdata")
    keys = [e.key for e in storage.archive().walk("garmin")]
    assert not any(k.endswith("hrv.json") for k in keys)
    assert "hard link" in caplog.text and "processed/hrv.json" in caplog.text
    assert str(paths.ROOT) not in caplog.text  # keys, not this machine's paths
    index.rebuild()
    code, out, _ = run("--json", "sql", "SELECT path FROM files")
    assert code == 0 and not any(r["path"].endswith("hrv.json") for r in json.loads(out))


def test_case_variant_symlink_to_state_is_not_read(run, archive):
    os.symlink(_case_variant_state() / "garmin-tokens.json", _proc() / "hrv.json")
    code, out, err = run("--json", "day", DAY, "--source", "hrv")
    assert code == 3
    _no_leak(out, err)


def test_case_variant_folder_link_to_state_is_not_followed(run, archive):
    """A folder of the archive that is a link to .STATE (or to .state) is never entered."""
    proc = _proc()
    for f in proc.iterdir():
        f.unlink()
    proc.rmdir()
    os.symlink(_case_variant_state(), proc)
    (paths.STATE / "sleep.json").write_text(json.dumps({"leak": TOKEN_TEXT}))
    code, out, err = run("--json", "day", DAY, "--source", "sleep")
    assert code == 3
    _no_leak(out, err)
    code, out, err = run("--json", "day", DAY)
    assert code == 0
    _no_leak(out, err)


@pytest.mark.parametrize("how", ["hard link", "symlink", "case variant"])
def test_decode_refuses_the_token_however_it_is_named(run, archive, tmp_path, how):
    if how == "hard link":
        target = tmp_path / "x.fit"
        os.link(paths.TOKENS, target)
    elif how == "symlink":
        target = tmp_path / "x.fit"
        os.symlink(paths.TOKENS, target)
    else:
        target = _case_variant_state() / "garmin-tokens.json"
    code, out, err = run("decode", str(target))
    _no_leak(out, err)
    assert "Traceback" not in err
    if how == "case variant" and not target.exists():
        assert code == 3  # a case-sensitive disk: there is no such file
    else:
        assert code == 5 and "saved Garmin login" in err


def test_decode_of_a_missing_file_is_not_found(run, archive, tmp_path):
    code, out, err = run("--json", "decode", str(tmp_path / "nope.fit"))
    assert code == 3 and out == "" and json.loads(err)["kind"] == "not_found"
    assert str(tmp_path) not in err and "Traceback" not in err


def test_decode_refuses_folders_pipes_and_huge_files(run, archive, tmp_path, monkeypatch):
    assert run("decode", str(tmp_path))[0] == 2
    fifo = tmp_path / "pipe.fit"
    os.mkfifo(fifo)
    code, _, err = run("decode", str(fifo))
    assert code == 2 and "pipe" in err
    from fitdata_garmin import fit
    monkeypatch.setattr(fit, "MAX_FIT_BYTES", 10)
    big = tmp_path / "big.fit"
    big.write_bytes(b"0" * 100)
    code, _, err = run("decode", str(big))
    assert code == 2 and "larger than" in err


# ------------------------------------------------------------------ B2: links pointing out

@pytest.fixture
def outside(tmp_path) -> Path:
    o = tmp_path / "elsewhere"
    (o / "processed").mkdir(parents=True)
    (o / "day.json").write_text(json.dumps({"date": "2026-11-15", "headline": {"secret": SECRET}}))
    (o / "processed" / "sleep.json").write_text(json.dumps({"secret": SECRET}))
    (o / "notes.json").write_text(json.dumps({"secret": SECRET}))
    # Shaped like an archive, for a garmin folder that is itself a link here.
    d = o / "days" / DAY[:4] / DAY
    (d / "processed").mkdir(parents=True)
    (d / "day.json").write_text(json.dumps({"date": DAY, "headline": {"secret": SECRET}}))
    (d / "processed" / "sleep.json").write_text(json.dumps({"secret": SECRET}))
    return o


def test_symlinked_file_to_outside_is_not_read(run, archive, outside):
    os.symlink(outside / "notes.json", _proc() / "hrv.json")
    code, out, err = run("--json", "day", DAY, "--source", "hrv")
    assert code == 3
    _no_leak(out, err)


def test_symlinked_day_folder_is_not_followed(run, archive, outside):
    os.symlink(outside, paths.DAYS / "2026" / "2026-11-15")
    code, out, err = run("--json", "day", "2026-11-15", "--source", "sleep")
    assert code == 3
    _no_leak(out, err)
    code, out, err = run("--json", "day", "2026-11-15")
    assert code == 3
    code, out, _ = run("--json", "status")
    assert code == 0 and json.loads(out)["days"] == 1  # the linked folder is not counted
    index.rebuild()
    code, out, err = run("--json", "sql", "SELECT * FROM days")
    _no_leak(out, err)
    assert [r["date"] for r in json.loads(out)] == [DAY]


def test_symlinked_year_folder_hides_its_activities(run, archive, tmp_path):
    other = tmp_path / "other-year"
    adir = other / "2025-01-01" / "activities" / "0700-running-222"
    (adir / "derived").mkdir(parents=True)
    (adir / "activity.json").write_text(json.dumps({"id": 222, "secret": SECRET}))
    (adir / "derived" / "records.csv").write_text(f"timestamp,x\n1,{SECRET}\n")
    os.symlink(other, paths.DAYS / "2025")
    for argv in (("activity", "222"), ("activity", "222", "--records")):
        code, out, err = run("--json", *argv)
        assert code == 3
        _no_leak(out, err)
    assert paths.find_activity_dir("222") is None
    code, out, _ = run("--json", "status")
    assert json.loads(out)["activities"] == 1


def test_symlinked_garmin_folder_is_not_followed(run, archive, outside, tmp_path):
    real = paths.ROOT / "garmin"
    real.rename(tmp_path / "moved-garmin")
    os.symlink(outside, real)
    for argv in (("day", DAY), ("day", DAY, "--source", "sleep"), ("status",)):
        code, out, err = run("--json", *argv)
        assert code in (0, 3) and (code == 0) == (argv == ("status",))
        _no_leak(out, err)


def test_sync_does_not_write_through_a_symlinked_folder(tmp_path, monkeypatch, outside):
    from conftest import use
    use(tmp_path, monkeypatch)
    today = date.today()
    dkey_dir = paths.day_dir(today)
    dkey_dir.mkdir(parents=True)
    before = sorted(p.name for p in (outside / "processed").iterdir())
    os.symlink(outside / "processed", dkey_dir / "processed")
    with pytest.raises(Refused) as e:
        sync.sync_day(FakeSession(FakeApi()), today)
    assert "symbolic link" in e.value.message and str(outside) not in e.value.message
    assert sorted(p.name for p in (outside / "processed").iterdir()) == before
    assert json.loads((outside / "processed" / "sleep.json").read_text()) == {"secret": SECRET}


def test_sync_replaces_a_symlinked_file_without_writing_through_it(tmp_path, monkeypatch, outside):
    from conftest import use
    use(tmp_path, monkeypatch)
    today = date.today()
    proc = paths.day_dir(today) / "processed"
    proc.mkdir(parents=True)
    os.symlink(outside / "notes.json", proc / "sleep.json")
    sync.sync_day(FakeSession(FakeApi()), today)
    assert json.loads((outside / "notes.json").read_text()) == {"secret": SECRET}
    assert not (proc / "sleep.json").is_symlink()
    assert "dailySleepDTO" in json.loads((proc / "sleep.json").read_text())


def test_links_are_reported_in_plain_words(archive, outside, caplog):
    os.symlink(outside / "notes.json", _proc() / "hrv.json")
    caplog.set_level(logging.WARNING, "fitdata")
    assert storage.archive().read(f"{paths.day_key(DAY)}/processed/hrv.json") is None
    assert "Skipped garmin/days/2026/2026-11-14/processed/hrv.json in the archive: it is a symbolic link" in caplog.text


def test_normal_archive_reads_are_unchanged(run, archive):
    code, out, _ = run("--json", "day", DAY, "--source", "sleep")
    assert code == 0 and json.loads(out) == {"sleep": "ok"}
    code, out, _ = run("--json", "activity", ACTIVITY_ID)
    info = json.loads(out)
    assert code == 0 and info["files"] == ["activity.json", "derived/records.csv", "processed/splits.json"]
    code, out, _ = run("--json", "status")
    st = json.loads(out)
    assert st["days"] == 1 and st["activities"] == 1


def test_a_full_sync_still_works(tmp_path, monkeypatch, cli_run):
    from conftest import use
    use(tmp_path, monkeypatch)
    today = date.today().isoformat()
    sync.sync_recent(FakeSession(FakeApi([activity(5, f"{today} 07:00:00")])), days=2)
    index.rebuild()
    a = storage.archive()
    akey = storage.find_activity(a, 5)
    assert a.read(f"{akey}/derived/records.csv").decode().count("\n") == 6
    code, out, _ = cli_run("--json", "activity", "5", "--records", "--columns", "heart_rate")
    assert code == 0 and len(json.loads(out)) == 5


def test_query_helpers_raise_not_found_for_links(archive, outside):
    os.symlink(outside / "notes.json", _proc() / "hrv.json")
    with pytest.raises(NotFound):
        query.day(DAY, "hrv")
