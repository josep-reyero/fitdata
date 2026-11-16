"""Issue 1: `day` and `activity` built file paths from unchecked DATE, ID and --source values."""
from __future__ import annotations

import json
import os

import pytest

from conftest import ACTIVITY_ID, DAY, TOKEN_TEXT
from fitdata import paths, query
from fitdata.validate import BadInput

TOKEN_FROM_DAY = "../../../../../.state/garmin-tokens"
TOKEN_FROM_ACTIVITY = "../../../../../../../.state/garmin-tokens"


def test_good_reads_still_work(run):
    code, out, _ = run("--json", "day", DAY, "--source", "sleep")
    assert code == 0 and json.loads(out) == {"sleep": "ok"}
    code, out, _ = run("--json", "activity", ACTIVITY_ID, "--source", "splits")
    assert code == 0 and json.loads(out) == {"splits": [1, 2]}
    code, out, _ = run("--json", "activity", ACTIVITY_ID, "--records")
    assert code == 0 and json.loads(out)[0]["heart_rate"] == "100"
    code, out, _ = run("--json", "day", DAY)
    assert code == 0 and json.loads(out)["sources_with_data"] == ["sleep"]


@pytest.mark.parametrize("argv", [
    ("day", DAY, "--source", TOKEN_FROM_DAY),
    ("activity", ACTIVITY_ID, "--source", TOKEN_FROM_ACTIVITY),
    ("day", DAY, "--source", "Sleep"),
    ("day", DAY, "--source", "not_a_source"),
    ("day", DAY, "--source", ""),
    ("activity", ACTIVITY_ID, "--source", "sleep"),   # a day source, not an activity one
])
def test_bad_sources_refused(run, argv):
    code, out, err = run("--json", *argv)
    assert code == 2
    assert TOKEN_TEXT not in out + err
    assert json.loads(err)["kind"] == "bad_input"
    assert "fitdata sources" in json.loads(err)["error"]


@pytest.mark.parametrize("bad", ["", "../../../outside", "/tmp", "2026-13-01", "2026-02-30", "20261114",
                                 "2026-W46-1", "2026-11-14/../../..", "2026-11-14\n", " 2026-11-14"])
def test_bad_dates_refused(run, archive, bad):
    code, out, err = run("day", bad, "--source", "sleep")
    assert code == 2 and out == ""
    assert "YYYY-MM-DD" in err and TOKEN_TEXT not in err


def test_absolute_date_cannot_reach_outside(run, archive):
    outside = archive.parent / "outside"
    code, out, err = run("day", str(outside), "--source", "leak")
    assert code == 2 and TOKEN_TEXT not in out + err


@pytest.mark.parametrize("bad", ["*", "1*", f"{ACTIVITY_ID}/../../../../../../..", "-1", "1e3", "", "12 3",
                                 "1" * 21])
def test_bad_activity_ids_refused(run, bad):
    code, out, err = run("activity", bad)
    assert code == 2 and out == ""
    assert "numbers only" in err


def test_from_to_dates_checked(run):
    assert run("days", "--from", "yesterday")[0] == 2
    assert run("activities", "--to", "2026-02-31")[0] == 2
    assert run("--json", "days", "--from", DAY, "--to", DAY)[0] == 0


def test_path_helpers_refuse_bad_values(archive):
    with pytest.raises(BadInput):
        paths.day_dir("../x")
    with pytest.raises(BadInput):
        paths.find_activity_dir("*")
    with pytest.raises(BadInput):
        paths.activity_dir(DAY, "", "running", "1/../2")
    p = paths.activity_dir(DAY, "2026-11-14T07:/0:00", "../Trail Run", 42)
    assert p.parent == paths.day_dir(DAY) / "activities" and p.name == "070-trail_run-42"


def test_symlink_to_token_is_not_read(archive):
    """Even a known source name won't read the token if the file is a link to it."""
    proc = paths.day_dir(DAY) / "processed"
    os.symlink(paths.TOKENS, proc / "hrv.json")
    with pytest.raises(SystemExit) as e:
        query.day(DAY, "hrv")
    assert TOKEN_TEXT not in str(e.value)
    records = paths.find_activity_dir(ACTIVITY_ID) / "derived" / "records.csv"
    records.unlink()
    os.symlink(paths.TOKENS, records)
    with pytest.raises(SystemExit):
        query.activity(ACTIVITY_ID, records=True)


def test_backfill_until_checked(run):
    code, _, err = run("backfill", "--until", "../../x")
    assert code == 2 and "--until date" in err
