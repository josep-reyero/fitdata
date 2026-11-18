"""The index: fitdata-garmin's rows in the project's shared database, rebuilt from the files.

The database is <data_dir>/fitdata.sqlite, shared by every app of the fitdata project (apps
contract, section 4). fitdata-garmin keeps in it:

    days__rows        its rows (source = 'fitdata-garmin') of the shared table days: one per archived
                      day, columns = day.json "headline" and a few more (see project.toml)
    activities__rows  its rows of the shared table activities: one per activity
    garmin_files      its own table: every stored file (path, kind, date, activity_id, bytes)
    garmin_sleep, garmin_daily, garmin_training, garmin_body_battery_events
                      its own tables of Garmin's daily detail, one row per day (or per event),
                      read from processed/<source>.json by extract.py
    garmin_samples, garmin_sleep_stages
                      its own tables of the series through the day and the night, for the most
                      recent intraday_days days of the archive only (config.py; default 60)

Readers use the views days and activities, which schema.sql creates when they are missing (once
they exist, `mes db sync` keeps them in line with project.toml); never the __rows tables.
The columns are fixed by project.toml [tables]; schema.sql next to this file is the exact text
`mes db schema --sql` prints for it. fitdata does not import mes: it follows the contract's
plain-SQL rules (section 4.4). It writes only rows whose source is its own name, deletes only its
own rows, and does everything in one BEGIN IMMEDIATE transaction, so a reader sees the old index
or the new one, never half of each, and the rows of other apps are never touched.

The index is derived: the files under garmin/ are the source of truth and `fitdata index`
rebuilds it at any time. A file whose tables do not match the project's schema (a plain table
where the schema has the view days or activities) has those tables replaced by the schema's.

The archive is read through storage.archive() (plain files or a bucket); paths in the index are
the archive keys, which are the same as the files' paths relative to data_dir.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path
from typing import Any

from . import config, extract, paths, storage
from .errors import FitdataError, Temporary
from .safefs import make_lock_file

log = logging.getLogger("fitdata")

APP = "fitdata-garmin"           # the source of every row this app writes ([app] name in app.toml)
SHARED = ("days", "activities")  # [data] writes in app.toml
FILES_TABLE = "garmin_files"     # [data] tables in app.toml, with extract.TABLES
FILES_COLUMNS = ("path", "kind", "date", "activity_id", "bytes")
APP_TABLES = (FILES_TABLE, *extract.TABLES)  # every table of this app, in app.toml's order
# The processed sources the index reads for every day, and those it reads only for the days whose
# series it keeps (extract.day). Other sources stay in the files.
DAY_SOURCES = ("daily_summary", "sleep", "stress", "body_battery", "body_battery_events", "respiration", "spo2",
               "hrv", "resting_hr", "intensity_minutes", "naps", "training_readiness",
               "morning_training_readiness", "training_status", "training_load_balance", "max_metrics",
               "endurance_score", "hill_score", "race_predictions", "fitness_age", "heat_altitude_acclimation",
               "weigh_ins", "hydration")
INTRADAY_SOURCES = ("heart_rate", "steps")
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_S = 60              # the contract asks for at least 30 seconds

_FILES_DDL = (
    f'CREATE TABLE "{FILES_TABLE}" ("path" TEXT PRIMARY KEY, "kind" TEXT, "date" TEXT, '
    '"activity_id" TEXT, "bytes" INTEGER)',
    f'CREATE INDEX "{FILES_TABLE}_date" ON "{FILES_TABLE}" ("date")',
)


class SchemaMismatch(FitdataError):
    kind = "schema_mismatch"


def schema_statements() -> list[str]:
    """schema.sql, one statement at a time (executescript would commit the open transaction)."""
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise SchemaMismatch(f"{SCHEMA_SQL} ends in the middle of a statement, so the index was not rebuilt.",
                             hint="Reinstall fitdata: git checkout -- apps/fitdata_garmin/schema.sql, then "
                             "uv sync --locked --no-dev.")
    return out


_VIEW_STATEMENT = re.compile(r'\A(?:DROP VIEW IF EXISTS|CREATE VIEW) "([a-z][a-z0-9_]*)"')


def schema_to_run(db: sqlite3.Connection, statements: list[str]) -> list[str]:
    """The statements of schema.sql to run on this file (apps contract 4.4, rule 2): every CREATE
    TABLE / CREATE INDEX (they are IF NOT EXISTS, so they never change what is there), and a view's
    DROP VIEW / CREATE VIEW only when that view does not exist yet. Existing views belong to mes
    (`mes db sync`): this copy of the script lists the apps of the day it was made, and running its
    views again would hide the rows of every app added since."""
    views = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    out = []
    for s in statements:
        m = _VIEW_STATEMENT.match(s)
        if m and m.group(1) in views:
            continue
        out.append(s)
    return out


def _columns(db: sqlite3.Connection, table: str) -> list[tuple]:
    """(name, declared type, not null, primary key position) of each column of a table."""
    return [(r[1], r[2].upper(), r[3], r[5]) for r in db.execute(f'PRAGMA table_info("{table}")')]


def expected_columns() -> dict[str, list[tuple]]:
    """The columns schema.sql gives each shared table's __rows table."""
    mem = sqlite3.connect(":memory:")
    try:
        for s in schema_statements():
            mem.execute(s)
        return {t: _columns(mem, f"{t}__rows") for t in SHARED}
    finally:
        mem.close()


# ------------------------------------------------------------------ reading the archive

def _manifests(a: storage.Archive, entries: list[storage.Entry]) -> tuple[list, list]:
    """(day key, day.json) and (activity key, activity.json) pairs, in path order."""
    days, acts = [], []
    for e in entries:
        parts = e.key.split("/")  # garmin, days, YYYY, date, ...
        if len(parts) == 5 and parts[4] == "day.json":
            target = days
        elif len(parts) == 7 and parts[4] == "activities" and parts[6] == "activity.json":
            target = acts
        else:
            continue
        m = a.read_json(e.key)
        if isinstance(m, dict):
            target.append((e.key.rsplit("/", 1)[0], m))
    return days, acts


INT64 = (-(2**63), 2**63 - 1)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _value(v: Any) -> Any:
    """A value as SQLite stores it. Values come from Garmin, so anything unusual is made safe:
    lists and objects become JSON text (as the contract writes its json type), True/False 1/0,
    and a whole number too large for SQLite is left out (NULL) rather than stopping the index."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v if INT64[0] <= v <= INT64[1] else None
    if v is None or isinstance(v, (float, str)):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _key_ok(v: Any) -> bool:
    if isinstance(v, str):
        return v != ""
    return isinstance(v, int) and not isinstance(v, bool) and INT64[0] <= v <= INT64[1]


def _read_sources(a: storage.Archive, folder: str, names: tuple[str, ...], present: set[str]) -> dict[str, Any]:
    """processed/<name>.json of one day folder, parsed; a file that is missing, unreadable or not
    JSON is left out (logged), so one bad file never stops the rebuild."""
    out: dict[str, Any] = {}
    for name in names:
        key = f"{folder}/processed/{name}.json"
        if key not in present:
            continue
        try:
            data = a.read_json(key)
        except Exception as e:  # noqa: BLE001  (a damaged file or object: skip it, keep the rest)
            log.warning("index: could not read %s (%s); the rest of the day is indexed", key, type(e).__name__)
            continue
        if data is not None:
            out[name] = data
    return out


def _detail(date: str, sources: dict[str, Any], intraday: bool) -> dict[str, Any]:
    try:
        return extract.day(date, sources, intraday)
    except Exception:  # noqa: BLE001  (extract never raises on bad data; this is the last guard)
        log.warning("index: Garmin's files for %s had a shape fitdata could not read; that day's detail is "
                    "left out", date, exc_info=True)
        return {"days": {}, "sleep": None, "daily": None, "training": None, "events": [], "samples": [], "stages": []}


def intraday_days() -> int:
    return config.load().intraday_days


def _collect() -> tuple[list[dict], list[dict], list[dict], dict[str, list]]:
    """Day rows, activity rows, file rows and the rows of the app's detail tables, all read before
    anything is written."""
    a = storage.archive()
    entries = a.walk(paths.DAYS_KEY)
    day_rows, act_rows = _manifests(a, entries)
    keys = {e.key for e in entries}

    days: dict[Any, dict] = {}
    skipped = 0
    for key, m in day_rows:
        headline = m.get("headline") if isinstance(m.get("headline"), dict) else {}
        row = {"date": m.get("date"), "has_data": m.get("has_data"), "complete": m.get("complete"),
               "fetched_at": m.get("fetched_at"), **headline,
               "activity_count": len(m.get("activities") or []), "dir": key}
        if not _key_ok(row["date"]):
            skipped += 1
            continue
        days[row["date"]] = row  # a date seen twice: the later folder wins

    # Garmin's detail for each day, from its processed files; the series through the day only for
    # the most recent days of the archive.
    detail: dict[str, list] = {t: [] for t in extract.TABLES}
    keep = intraday_days()
    newest = max((d for d in days if isinstance(d, str) and _DATE.fullmatch(d)), default=None)
    try:
        first_kept = (_date.fromisoformat(newest) - _timedelta(days=keep - 1)).isoformat() if newest and keep > 0 else None
    except ValueError:  # a folder named like a date that is not one (2026-02-30)
        first_kept = None
    for d, row in sorted(days.items(), key=lambda kv: str(kv[0])):
        if not (isinstance(d, str) and _DATE.fullmatch(d)):
            continue  # no detail for a day whose date is not a calendar date
        intraday = first_kept is not None and d >= first_kept
        names = DAY_SOURCES + (INTRADAY_SOURCES if intraday else ())
        got = _detail(d, _read_sources(a, row["dir"], names, keys), intraday)
        for col, v in (got.get("days") or {}).items():
            if row.get(col) is None:  # day.json's headline first; the files fill what it lacks
                row[col] = v
        for table, part in (("garmin_sleep", "sleep"), ("garmin_daily", "daily"), ("garmin_training", "training")):
            if got.get(part):
                detail[table].append(got[part])
        detail["garmin_body_battery_events"] += got.get("events") or []
        detail["garmin_sleep_stages"] += got.get("stages") or []
        detail["garmin_samples"] += [dict(zip(("date", "series", "at", "value"), s)) for s in got.get("samples") or []]

    raw_fits: dict[str, list[str]] = {}
    for k in keys:
        folder, name = k.rsplit("/", 1)
        if name.endswith(".fit") and folder.endswith("/raw"):
            raw_fits.setdefault(folder[:-4], []).append(k)
    acts: dict[Any, dict] = {}
    for key, m in act_rows:
        fit = min(raw_fits.get(key, []), default=None)
        row = ({k: v for k, v in m.items() if k not in ("sources", "derived", "layout_version")}
               | {"dir": key, "fit": fit, "records": (m.get("derived") or {}).get("records")})
        if not _key_ok(row.get("id")):
            skipped += 1
            continue
        acts[row["id"]] = row
    if skipped:
        log.warning("index: left out %d day or activity file(s) without a usable date or id; the "
                    "others are indexed", skipped)

    files = []
    for e in entries:
        rel = e.key.split("/")[2:]  # YYYY, date, ...
        if len(rel) < 3:
            continue  # only files inside a day's folder
        kind = "/".join(x for x in rel[2:-1] if not x[:4].isdigit()) or "day"
        aid = rel[3].rsplit("-", 1)[-1] if len(rel) > 3 and rel[2] == "activities" else None
        files.append({"path": e.key, "kind": kind, "date": rel[1], "activity_id": aid, "bytes": e.size})
    return list(days.values()), list(acts.values()), files, detail


# ------------------------------------------------------------------ writing

def connect() -> sqlite3.Connection:
    """The shared database, for writing: busy timeout, rollback journal, no implicit transactions."""
    paths.ROOT.mkdir(parents=True, exist_ok=True)  # a new machine: an empty index is still an index
    db = sqlite3.connect(paths.DB, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
    if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        # The project's database uses the rollback journal, so read-only openers never need -shm files.
        db.execute("PRAGMA journal_mode = DELETE")
    return db


def _drop_tables_in_place_of_views(db: sqlite3.Connection) -> list[str]:
    """A plain table named like one of the shared views (days, activities) does not match the
    project's schema and would keep the view from being made. Everything in the index is rebuilt
    from the files, so such a table is dropped; the views themselves are left alone."""
    marks = ", ".join("?" * len(SHARED))
    kinds = dict(db.execute(f"SELECT name, type FROM sqlite_master WHERE name IN ({marks})", SHARED).fetchall())
    dropped = [t for t in SHARED if kinds.get(t) == "table"]
    for t in dropped:
        db.execute(f'DROP TABLE "{t}"')
    return dropped


def _check_columns(db: sqlite3.Connection, expected: dict[str, list[tuple]]) -> None:
    for t, cols in expected.items():
        found = _columns(db, f"{t}__rows")
        if found == cols:
            continue
        have, want = {c[0] for c in found}, {c[0] for c in cols}
        change = (f"it lacks the column(s) {', '.join(sorted(want - have))}" if want - have and not have - want
                  else "its columns or key are not the ones this version expects")
        raise SchemaMismatch(
            f"The shared database {paths.DB} has a table {t}__rows that does not match this version of "
            f"fitdata ({change}), so the index was not rebuilt. Nothing was changed, and the files under "
            "garmin/ are untouched; the commands that read the index show the previous one.",
            hint=f"With my-easy-software: mes db sync (or mes db sync --reset {t}). Without it, if "
            f"fitdata-garmin is the only app of this project, delete {paths.DB} and run: fitdata index")


def _insert(db: sqlite3.Connection, table: str, columns: list[str] | tuple[str, ...], rows: list[dict],
            source: str | None) -> None:
    names = (["source"] if source else []) + list(columns)
    sql = (f'INSERT OR REPLACE INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in names)}) '
           f'VALUES ({", ".join("?" * len(names))})')
    db.executemany(sql, [([source] if source else []) + [_value(r.get(c)) for c in columns] for r in rows])


def rebuild() -> None:
    days, acts, files, detail = _collect()  # read everything before the write transaction starts
    statements = schema_statements()
    expected = expected_columns()
    (paths.DB.parent / (paths.DB.name + ".tmp")).unlink(missing_ok=True)  # a stray temporary file, never used
    # The lock of `fitdata sql`, made here so that sql can take it where it may only read the data folder.
    make_lock_file(paths.SQL_LOCK)
    try:
        _write(statements, expected, days, acts, files, detail)
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            raise Temporary(f"The shared database {paths.DB} was busy for more than {BUSY_TIMEOUT_S} seconds "
                            "(another app or a long query was using it), so the index was not rebuilt this "
                            "time. The files under garmin/ are up to date.",
                            hint="Nothing to do: the next run rebuilds it. Or run: fitdata index") from None
        raise _broken(e) from None
    except sqlite3.DatabaseError as e:
        raise _broken(e) from None


def _broken(e: sqlite3.DatabaseError) -> FitdataError:
    return FitdataError(f"The shared database {paths.DB} could not be updated ({str(e)[:200]}), so the index "
                        "was not rebuilt. The files under garmin/ are untouched.",
                        hint=f"Everything in it can be rebuilt from the apps' files: move {paths.DB} aside, then "
                        "run: fitdata index")


def _write(statements: list[str], expected: dict[str, list[tuple]], days: list[dict], acts: list[dict],
           files: list[dict], detail: dict[str, list]) -> None:
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            dropped = _drop_tables_in_place_of_views(db)
            for s in schema_to_run(db, statements):
                db.execute(s)
            _check_columns(db, expected)
            for table, rows in (("days", days), ("activities", acts)):
                columns = [c[0] for c in expected[table] if c[0] != "source"]
                db.execute(f'DELETE FROM "{table}__rows" WHERE "source" = ?', (APP,))
                _insert(db, f"{table}__rows", columns, rows, APP)
            db.execute(f'DROP TABLE IF EXISTS "{FILES_TABLE}"')
            for s in _FILES_DDL:
                db.execute(s)
            _insert(db, FILES_TABLE, FILES_COLUMNS, files, None)
            for table, rows in detail.items():  # the app's own tables: made again from the files
                db.execute(f'DROP TABLE IF EXISTS "{table}"')
                for s in extract.ddl(table):
                    db.execute(s)
                _insert(db, table, extract.columns(table), rows, None)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    finally:
        db.close()
    if dropped:
        log.info("index: rebuilt the index: its tables did not match the project's schema (%s)", ", ".join(dropped))
    log.info("index: %d days, %d activities, %d files, %d samples -> %s", len(days), len(acts), len(files),
             len(detail.get("garmin_samples") or []), paths.DB.name)
