"""The index in the project's shared database (apps contract, sections 4.4 and 8.4): fitdata-garmin
writes only its own rows, in one transaction, replaces an old-format index by itself once, and
never touches what other apps keep in the same file."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from conftest import ACTIVITY_ID, DAY
from fitdata_garmin import index, paths, query
from fitdata_garmin.errors import FitdataError, Temporary

OTHER = "fitdata-strava"  # a made-up second app of the project


def _db():
    return sqlite3.connect(paths.DB)


def _objects() -> dict[str, str]:
    with _db() as db:
        return dict(db.execute("SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def test_the_layout(archive):
    objects = _objects()
    assert {k: v for k, v in objects.items() if v in ("table", "view")} == {
        "days__rows": "table", "activities__rows": "table", "garmin_files": "table",
        "days": "view", "activities": "view"}
    assert objects["days__rows_key"] == objects["activities__rows_key"] == objects["garmin_files_date"] == "index"
    with _db() as db:
        assert db.execute("SELECT source, date, steps FROM days__rows").fetchall() == [("fitdata-garmin", DAY, 1234)]
        assert db.execute("SELECT source, id, type FROM activities").fetchall() == [
            ("fitdata-garmin", int(ACTIVITY_ID), "running")]
        cols = [r[1] for r in db.execute('PRAGMA table_info("garmin_files")')]
        assert cols == ["path", "kind", "date", "activity_id", "bytes"]
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_other_apps_rows_and_tables_are_never_touched(archive):
    with _db() as db:
        db.execute("INSERT INTO days__rows (source, date, steps, sleep_score) VALUES (?, ?, ?, ?)", (OTHER, DAY, 99, 80))
        db.execute("INSERT INTO days__rows (source, date, steps) VALUES (?, '2026-01-01', 5)", (OTHER,))
        db.execute("INSERT INTO activities__rows (source, id, date, type) VALUES (?, 7, ?, 'cycling')", (OTHER, DAY))
        db.execute("CREATE TABLE strava_tokens (x)")
        db.execute("INSERT INTO strava_tokens VALUES ('theirs')")
    index.rebuild()
    index.rebuild()  # twice: nothing doubles
    with _db() as db:
        assert db.execute("SELECT count(*) FROM days__rows WHERE source = ?", (OTHER,)).fetchone()[0] == 2
        assert db.execute("SELECT * FROM strava_tokens").fetchall() == [("theirs",)]
        assert db.execute("SELECT count(*) FROM days__rows WHERE source = 'fitdata-garmin'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM activities__rows").fetchone()[0] == 2
        # The views show only the apps [project] apps lists; the made-up app is not one of them.
        assert db.execute("SELECT source, date, steps, sleep_score FROM days").fetchall() == [
            ("fitdata-garmin", DAY, 1234, None)]
        assert db.execute("SELECT count(*) FROM activities").fetchone()[0] == 1


def test_merge_view_with_a_second_installed_app(archive, tmp_path):
    """mes db sync made the views for a project that lists two apps (same generator as
    schema.sql). The index then runs its own one-app copy of schema.sql, which must keep those
    views: days merges column by column and activities keeps both apps' rows (contract 4.4, rule 2)."""
    import tomllib
    from schema_script import schema_sql
    from test_schema import REPO
    project = tomllib.loads((REPO / "project.toml").read_text())
    with _db() as db:
        db.executescript(schema_sql(project, ["fitdata-garmin", OTHER]))  # what mes db sync does
        db.execute("INSERT INTO days__rows (source, date, steps, sleep_score) VALUES (?, ?, 99, 80)", (OTHER, DAY))
        db.execute("INSERT INTO days__rows (source, date, steps) VALUES (?, '2026-01-01', 5)", (OTHER,))
        db.execute("INSERT INTO activities__rows (source, id, date, type) VALUES (?, 7, ?, 'cycling')", (OTHER, DAY))
    index.rebuild()
    rows = query.sql("SELECT source, date, steps, sleep_score FROM days ORDER BY date")
    assert rows == [{"source": OTHER, "date": "2026-01-01", "steps": 5, "sleep_score": None},
                    {"source": "fitdata-garmin", "date": DAY, "steps": 1234, "sleep_score": 80}]
    assert [r["source"] for r in query.sql("SELECT source FROM activities ORDER BY source")] == ["fitdata-garmin", OTHER]
    assert [d["date"] for d in query.days(DAY, DAY)] == [DAY]


def test_missing_views_are_made_from_schema_sql(archive):
    """A file without the views (new, or after mes db sync --reset) gets them from schema.sql."""
    with _db() as db:
        db.execute('DROP VIEW "days"')
    index.rebuild()
    with _db() as db:
        kinds = dict(db.execute("SELECT name, type FROM sqlite_master WHERE name IN ('days', 'activities')"))
        assert kinds == {"days": "view", "activities": "view"}
        assert db.execute("SELECT count(*) FROM days").fetchone()[0] == 1


def _old_index(path):
    """The index an earlier fitdata made: plain, untyped tables, all its own."""
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE "days" ("date" PRIMARY KEY, "has_data", "steps", "old_extra")')
        db.execute("INSERT INTO days VALUES ('2000-01-01', 1, 5, 'x')")
        db.execute('CREATE TABLE "activities" ("id" PRIMARY KEY, "date", "type")')
        db.execute("CREATE INDEX act_date ON activities(date)")
        db.execute('CREATE TABLE "files" ("path" PRIMARY KEY, "kind", "date", "activity_id", "bytes")')
        db.execute("CREATE INDEX files_date ON files(date)")


def test_an_old_index_is_replaced_once_and_safely(archive):
    paths.DB.unlink()
    _old_index(paths.DB)
    with _db() as db:
        db.execute("CREATE TABLE other_notes (x)")  # not one of the old index's tables: kept
    index.rebuild()
    objects = _objects()
    assert objects["days"] == "view" and objects["activities"] == "view" and "files" not in objects
    assert "act_date" not in objects and "files_date" not in objects and objects["other_notes"] == "table"
    assert [d["date"] for d in query.days(DAY, DAY)] == [DAY]  # rebuilt from the files
    assert query.sql("SELECT count(*) AS n FROM days") == [{"n": 1}]  # the old row is gone
    index.rebuild()  # the second time there is nothing old left
    assert _objects() == objects


def test_an_old_index_is_replaced_by_the_first_job(archive, cli_run, monkeypatch):
    """The first `fitdata-garmin index` (every job ends with one) after the update."""
    paths.DB.unlink()
    _old_index(paths.DB)
    code, out, err = cli_run("--local", "index")
    assert code == 0, err
    assert "replaced the index of an earlier fitdata" in err
    code, out, _ = cli_run("--json", "days", "--from", DAY, "--to", DAY)
    assert code == 0 and [d["date"] for d in json.loads(out)] == [DAY]


def test_a_rows_table_of_another_shape_is_refused_and_nothing_changes(archive, capsys, cli_run):
    with _db() as db:
        db.execute("DROP VIEW days")
        db.execute("DROP TABLE days__rows")
        db.execute('CREATE TABLE "days__rows" ("source" TEXT NOT NULL, "date" TEXT NOT NULL, "steps" INTEGER, '
                   'PRIMARY KEY ("source", "date"))')
        db.execute("INSERT INTO days__rows VALUES ('fitdata-garmin', '1999-01-01', 1)")
    before = paths.DB.read_bytes()
    code, out, err = cli_run("--json", "--local", "index")
    assert code == 1
    e = json.loads(err.strip().splitlines()[-1])
    assert e["kind"] == "schema_mismatch" and "days__rows" in e["error"] and "has_data" in e["error"]
    assert "mes db sync" in e["hint"]
    assert paths.DB.read_bytes() == before


def test_a_failure_halfway_leaves_the_previous_index(archive, monkeypatch):
    real = index._insert

    def fail_on_files(db, table, *a):
        if table == "garmin_files":
            raise RuntimeError("disk trouble")
        return real(db, table, *a)
    with _db() as db:
        before = db.execute("SELECT * FROM days__rows").fetchall()
    (paths.day_dir(DAY) / "day.json").write_text(json.dumps({"date": DAY, "headline": {"steps": 1}}))
    monkeypatch.setattr(index, "_insert", fail_on_files)
    with pytest.raises(RuntimeError):
        index.rebuild()
    with _db() as db:
        assert db.execute("SELECT * FROM days__rows").fetchall() == before
        assert db.execute("SELECT count(*) FROM garmin_files").fetchone()[0] > 0


def test_a_busy_database_is_temporary(archive, monkeypatch, cli_run):
    monkeypatch.setattr(index, "BUSY_TIMEOUT_S", 0.2)
    other = sqlite3.connect(paths.DB, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # another app is writing
    try:
        with pytest.raises(Temporary):
            index.rebuild()
        code, _, err = cli_run("--json", "--local", "index")
        assert code == 75 and json.loads(err.strip().splitlines()[-1])["kind"] == "temporary"
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_readers_wait_for_a_rebuild_instead_of_failing(archive):
    """A rebuild in one transaction: a reader started meanwhile sees the old or the new rows."""
    seen, errors = [], []

    def read():
        try:
            for _ in range(30):
                seen.append(len(query.days(DAY, DAY)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    t = threading.Thread(target=read)
    t.start()
    for _ in range(5):
        index.rebuild()
    t.join()
    assert not errors and set(seen) == {1}


def test_a_damaged_database_is_said_in_plain_words(archive, cli_run):
    paths.DB.write_bytes(b"this is not a database" * 100)
    code, _, err = cli_run("--json", "--local", "index")
    e = json.loads(err.strip().splitlines()[-1])
    assert code == 1 and "could not be updated" in e["error"] and "move" in e["hint"]


def test_values_are_stored_as_the_contract_says(archive):
    ddir = paths.day_dir(DAY)
    m = json.loads((ddir / "day.json").read_text())
    m.update({"has_data": True, "complete": False})
    m["headline"].update({"training_status": {"b": [1, 2], "a": "é"}, "resting_hr": 2**70, "steps": 10})
    (ddir / "day.json").write_text(json.dumps(m))
    index.rebuild()
    with _db() as db:
        row = db.execute("SELECT has_data, complete, training_status, resting_hr, steps FROM days").fetchone()
    assert row == (1, 0, '{"b":[1,2],"a":"é"}', None, 10)


def test_rows_without_a_key_are_left_out(archive, caplog):
    (paths.day_dir(DAY) / "day.json").write_text(json.dumps({"headline": {"steps": 1}}))  # no date
    index.rebuild()
    assert query.sql("SELECT count(*) AS n FROM days") == [{"n": 0}]
    assert "left out 1 day or activity file" in caplog.text


def test_query_output_is_unchanged(run):
    """The CLI's rows keep their keys: the views' source column is not added to them."""
    code, out, _ = run("--json", "days", "--from", DAY, "--to", DAY)
    (row,) = json.loads(out)
    assert code == 0 and "source" not in row and row["steps"] == 1234 and list(row)[:2] == ["date", "has_data"]
    code, out, _ = run("--json", "activities", "--days", "3650")
    (act,) = json.loads(out)
    assert "source" not in act and list(act)[:3] == ["id", "date", "type"]
    code, out, _ = run("--json", "schema")
    assert set(json.loads(out)["tables"]) == {"days", "activities", "garmin_files"}


def test_a_database_without_the_index_says_so(tmp_path, monkeypatch, cli_run):
    from conftest import use
    use(tmp_path, monkeypatch)
    with sqlite3.connect(paths.DB) as db:
        db.execute("CREATE TABLE someone_elses (x)")
    for argv in (["--json", "days"], ["--json", "schema"], ["--json", "sql", "SELECT * FROM days"]):
        code, _, err = cli_run(*argv)
        assert code == 3 and json.loads(err)["kind"] == "not_found", argv


def test_schema_mismatch_is_a_fitdata_error():
    assert issubclass(index.SchemaMismatch, FitdataError)
