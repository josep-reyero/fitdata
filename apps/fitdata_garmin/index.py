"""The index: fitdata-garmin's rows in the project's shared database, rebuilt from the files.

The database is <data_dir>/fitdata.sqlite, shared by every app of the fitdata project (apps
contract, section 4). fitdata-garmin keeps in it:

    days__rows        its rows (source = 'fitdata-garmin') of the shared table days: one per archived
                      day, columns = day.json "headline" and a few more (see project.toml)
    activities__rows  its rows of the shared table activities: one per activity
    garmin_files      its own table: every stored file (path, kind, date, activity_id, bytes)

Readers use the views days and activities, which schema.sql creates when they are missing (once
they exist, `mes db sync` keeps them in line with project.toml); never the __rows tables.
The columns are fixed by project.toml [tables]; schema.sql next to this file is the exact text
`mes db schema --sql` prints for it. fitdata does not import mes: it follows the contract's
plain-SQL rules (section 4.4). It writes only rows whose source is its own name, deletes only its
own rows, and does everything in one BEGIN IMMEDIATE transaction, so a reader sees the old index
or the new one, never half of each, and the rows of other apps are never touched.

The index is derived: the files under garmin/ are the source of truth and `fitdata index`
rebuilds it at any time. An index made by an earlier fitdata (plain tables days, activities and
files, all its own) is replaced the first time, automatically.

The archive is read through storage.archive() (plain files or a bucket); paths in the index are
the archive keys, which are the same as the files' paths relative to data_dir.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import paths, storage
from .errors import FitdataError, Temporary

log = logging.getLogger("fitdata")

APP = "fitdata-garmin"           # the source of every row this app writes ([app] name in app.toml)
SHARED = ("days", "activities")  # [data] writes in app.toml
FILES_TABLE = "garmin_files"     # [data] tables in app.toml
FILES_COLUMNS = ("path", "kind", "date", "activity_id", "bytes")
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_S = 60              # the contract asks for at least 30 seconds
# The tables an index made before the shared database had: all fitdata's own and rebuildable.
OLD_TABLES = ("days", "activities", "files")

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


def _collect() -> tuple[list[dict], list[dict], list[dict]]:
    """Day rows, activity rows and file rows, all read before anything is written."""
    a = storage.archive()
    entries = a.walk(paths.DAYS_KEY)
    day_rows, act_rows = _manifests(a, entries)
    keys = {e.key for e in entries}

    days: dict[Any, dict] = {}
    skipped = 0
    for key, m in day_rows:
        row = {"date": m.get("date"), "has_data": m.get("has_data"), "complete": m.get("complete"),
               "fetched_at": m.get("fetched_at"), **(m.get("headline") or {}),
               "activity_count": len(m.get("activities") or []), "dir": key}
        if not _key_ok(row["date"]):
            skipped += 1
            continue
        days[row["date"]] = row  # a date seen twice: the later folder wins, as before

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
    return list(days.values()), list(acts.values()), files


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


def _drop_old_index(db: sqlite3.Connection) -> list[str]:
    """An index from before the shared database: its plain tables days, activities and files were
    only ever fitdata's own, rebuildable from the files. They are dropped so the views can take
    their names. Views of those names (the new layout) are left alone."""
    kinds = dict(db.execute("SELECT name, type FROM sqlite_master WHERE name IN (?, ?, ?)", OLD_TABLES).fetchall())
    old = [t for t in OLD_TABLES if kinds.get(t) == "table"]
    for t in old:
        db.execute(f'DROP TABLE "{t}"')
    return old


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
    days, acts, files = _collect()  # read everything before the write transaction starts
    statements = schema_statements()
    expected = expected_columns()
    (paths.DB.parent / (paths.DB.name + ".tmp")).unlink(missing_ok=True)  # left by an earlier fitdata
    try:
        _write(statements, expected, days, acts, files)
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
           files: list[dict]) -> None:
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = _drop_old_index(db)
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
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    finally:
        db.close()
    if old:
        log.info("index: replaced the index of an earlier fitdata (its tables %s) with the shared tables",
                 ", ".join(old))
    log.info("index: %d days, %d activities, %d files -> %s", len(days), len(acts), len(files), paths.DB.name)
