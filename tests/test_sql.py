"""Issue 3: `fitdata sql` must only read the index: one SELECT, no ATTACH, PRAGMA, writes or extensions."""
from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import DAY
from fitdata_garmin import paths, query
from fitdata_garmin.validate import BadInput


def test_select_works(run):
    code, out, _ = run("--json", "sql", "SELECT date, steps FROM days")
    assert code == 0 and json.loads(out) == [{"date": DAY, "steps": 1234}]


@pytest.mark.parametrize("q", [
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5) SELECT sum(x) FROM c",
    "SELECT json_extract('{\"a\": 1}', '$.a'), upper('x'), count(*) FROM garmin_files",
    "SELECT j.value FROM days, json_each('[1, 2]') j",
    "SELECT count(*) FROM days",
    "WITH x AS (SELECT date FROM days) SELECT count(*) FROM x JOIN activities a ON a.date = x.date",
    "SELECT * FROM activities a JOIN days d ON a.date = d.date",
])
def test_ordinary_reads_allowed(archive, q):
    assert query.sql(q)


@pytest.mark.parametrize("q", [
    "ATTACH '{tmp}/new.db' AS x",
    "ATTACH 'file:{tmp}/new.db?mode=rwc' AS x",
    "VACUUM INTO '{tmp}/new.db'",
    "SELECT load_extension('{tmp}/new')",
    "PRAGMA journal_mode = WAL",
    "PRAGMA user_version = 5",
    "PRAGMA table_info(days)",
    "SELECT * FROM pragma_table_info('days')",
    "SELECT name FROM sqlite_master",
    "SELECT * FROM days__rows",
    "SELECT count(*) FROM activities__rows",
    "SELECT * FROM days WHERE date IN (SELECT date FROM days__rows)",
    "WITH days AS (SELECT * FROM days__rows) SELECT * FROM days",
    "WITH activities AS (SELECT * FROM \"ACTIVITIES__ROWS\") SELECT count(*) FROM activities",
    "WITH x AS (SELECT 1) SELECT * FROM x, [days__rows]",
    "SELECT * FROM days WHERE source IN (SELECT source FROM `activities__rows`)",
    "WITH days AS (SELECT * FROM other_app_secrets) SELECT * FROM days",
    "SELECT * FROM other_app_secrets",
    "DELETE FROM days",
    "INSERT INTO days(date) VALUES ('2000-01-01')",
    "CREATE TABLE t(x)",
    "CREATE TEMP TABLE t(x)",
    "DROP TABLE days",
    "BEGIN",
    "SELECT 1; SELECT 2",
    "SELECT 1; ATTACH '{tmp}/new.db' AS x",
])
def test_everything_else_refused(archive, q):
    tmp = archive.parent
    with sqlite3.connect(paths.DB) as db:  # a table of another app, in the same shared database
        db.execute("CREATE TABLE IF NOT EXISTS other_app_secrets (x)")
        db.execute("INSERT INTO other_app_secrets VALUES ('not for fitdata sql')")
    with pytest.raises(BadInput) as e:
        query.sql(q.format(tmp=tmp))
    assert "only read the index" in str(e.value) or "one statement" in str(e.value)
    assert not (tmp / "new.db").exists()
    with sqlite3.connect(paths.DB) as db:  # the index is untouched
        assert db.execute("SELECT count(*) FROM days").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM other_app_secrets").fetchone()[0] == 1


def test_refusal_is_plain_language(run):
    code, out, err = run("--json", "sql", "ATTACH 'x.db' AS x")
    assert code == 2 and out == ""
    msg = json.loads(err)["error"]
    assert "only read the index" in msg and "fitdata schema" in msg


def test_row_limit(archive, capsys):
    q = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 50) SELECT x FROM c"
    assert len(query.sql(q, limit=10)) == 10
    assert "first 10 rows" in capsys.readouterr().err
    assert len(query.sql(q, limit=50)) == 50
    assert capsys.readouterr().err == ""
    with pytest.raises(BadInput):
        query.sql(q, limit=0)


def test_limit_flag(run):
    q = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 50) SELECT x FROM c"
    code, out, err = run("--json", "sql", q, "--limit", "3")
    assert code == 0 and len(json.loads(out)) == 3 and "--limit" in err


def test_runaway_query_stopped(archive):
    q = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(BadInput, match="seconds"):
        query.sql(q, timeout_s=0.2)


def test_odd_folder_name_does_not_change_uri(tmp_path, monkeypatch, archive):
    """A '?' in the data folder name must not be read as SQLite URI options."""
    odd = tmp_path / "odd?mode=rwc#x"
    odd.mkdir()
    (odd / "fitdata.sqlite").write_bytes(paths.DB.read_bytes())
    monkeypatch.setattr(paths, "DB", odd / "fitdata.sqlite")
    assert query.sql("SELECT count(*) AS n FROM days") == [{"n": 1}]


@pytest.mark.parametrize("q", [
    "SELECT length(zeroblob(900000000)) AS a FROM garmin_files LIMIT 3",
    "SELECT printf('%.*c', 100000000, 'a') AS s",
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 600) "
    "SELECT sum(instr(printf('%.*c', 100000000 + x, 'a'), 'b')) AS s FROM c",
    "SELECT replace(hex(zeroblob(400000)), '0', '0000000000') AS s",
])
def test_huge_values_are_stopped_quickly(archive, q):
    import time
    t0 = time.monotonic()
    try:
        rows = query.sql(q)  # SQLite's printf() gives NULL instead of a value over the cap
        assert all(v is None for r in rows for v in r.values())
    except BadInput as e:
        assert "memory" in str(e) or "larger" in str(e)
    assert time.monotonic() - t0 < 5


def test_slow_query_hits_the_time_limit(archive):
    q = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
         "SELECT sum(length(printf('%.*c', 900000, 'a'))) FROM c")
    import time
    t0 = time.monotonic()
    with pytest.raises(BadInput, match="still running"):
        query.sql(q, timeout_s=1)
    assert time.monotonic() - t0 < 4


def test_result_size_is_capped(archive, monkeypatch):
    monkeypatch.setattr(query, "SQL_MAX_RESULT_BYTES", 10_000)
    q = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 100) SELECT printf('%.*c', 500, 'a') FROM c"
    with pytest.raises(BadInput, match="larger than"):
        query.sql(q)


def test_a_query_on_files_points_to_garmin_files(run):
    code, _, err = run("--json", "sql", "SELECT * FROM files LIMIT 2")
    assert code == 2
    e = json.loads(err.strip().splitlines()[-1])
    assert "no table files" in e["error"] and "FROM garmin_files" in e["error"]
    code, out, _ = run("--json", "sql", "SELECT count(*) AS n FROM garmin_files")
    assert code == 0 and json.loads(out)[0]["n"] > 0
