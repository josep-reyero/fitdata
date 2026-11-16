"""Read-only access to the archive, shaped for people and for AI agents (`--json`).

The archive is opened read-only (storage.archive()), whether it is plain files or a bucket, and
every key is checked (storage.check_key), so no query can reach the saved login in .state/."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from typing import Any

from . import endpoints, index, paths, storage
from .errors import NotFound
from .safefs import hold_lock
from .validate import BadInput, check_activity_id, check_date, check_source

# `fitdata sql`: rows returned unless --limit says otherwise, and how long one query may run.
SQL_DEFAULT_LIMIT = 1000
SQL_TIMEOUT_S = 30
# How often (in SQLite VM steps) the time limit is checked. Small, so one slow step can't hide
# for long between checks.
SQL_PROGRESS_STEPS = 1000
# The largest text or blob one value may grow to inside a query. Real values in the index are a
# few hundred bytes; the cap stops zeroblob()/printf()/replace() from building gigabytes in one
# step (a single step can't be interrupted by the time limit).
SQL_MAX_VALUE_BYTES = 1_000_000
# Most memory SQLite itself may use for one query, and most bytes of result data handed back.
SQL_MAX_HEAP_BYTES = 256 * 1024 * 1024
SQL_MAX_RESULT_BYTES = 64 * 1024 * 1024

# Extra files sync writes next to the Garmin sources of an activity.
ACTIVITY_EXTRA_SOURCES = ("list_entry",)


NO_INDEX = "There is no index in the data folder yet, so there is nothing to query."
NO_INDEX_HINT = "Run: fitdata index (or fitdata sync)"


def _db() -> sqlite3.Connection:
    if not paths.DB.exists():
        raise NotFound(NO_INDEX, hint=NO_INDEX_HINT)
    # as_uri() escapes characters such as "?" or "#" in the folder name, so they can't add URI options.
    db = sqlite3.connect(f"{paths.DB.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def readable() -> dict[str, str]:
    """What fitdata's queries may read, by name: the shared views (from schema.sql, in its order)
    and fitdata-garmin's own table. Never the views' __rows tables directly."""
    views = re.findall(r'^CREATE VIEW "([a-z][a-z0-9_]*)" AS$', index.SCHEMA_SQL.read_text(encoding="utf-8"), re.M)
    return {**{v: "view" for v in views}, index.FILES_TABLE: "table"}


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _db() as db:
        try:
            return [dict(r) for r in db.execute(sql, params)]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                # The file exists but holds no index of fitdata-garmin yet (made by another app, say).
                raise NotFound(NO_INDEX, hint=NO_INDEX_HINT) from None
            raise



MAX_DAYS = 100 * 366  # longer than any real archive; bigger numbers are a typo


def _range(frm: str | None, to: str | None, days: int | None) -> tuple[str, str]:
    if days is not None and not 1 <= days <= MAX_DAYS:
        raise BadInput(f"--days must be between 1 and {MAX_DAYS}, but it was {days}. It is how many "
                       "days back to look, for example --days 30.")
    end = check_date(to, "--to date") if to else date.today().isoformat()
    try:
        start = check_date(frm, "--from date") if frm else (date.fromisoformat(end) - timedelta(days=(days or 7) - 1)).isoformat()
    except OverflowError:
        start = date.min.isoformat()
    return start, end


def days(frm: str | None = None, to: str | None = None, last: int | None = None,
         fields: str | None = None) -> list[dict[str, Any]]:
    start, end = _range(frm, to, last)
    rows = _rows("SELECT * FROM days WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))
    if fields:
        keep = ["date", *[f.strip() for f in fields.split(",") if f.strip()]]
        rows = [{k: r.get(k) for k in keep} for r in rows]
    else:
        rows = [{k: v for k, v in r.items() if k not in ("source", "dir", "fetched_at", "complete")} for r in rows]
    return rows


def activities(frm: str | None = None, to: str | None = None, last: int | None = None,
               type_: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    start, end = _range(frm, to, last or 3650)
    sql = "SELECT * FROM activities WHERE date BETWEEN ? AND ?"
    params: list[Any] = [start, end]
    if type_:
        sql += " AND type LIKE ?"
        params.append(f"%{type_}%")
    sql += " ORDER BY start_local DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    # The view has a "source" column (the app a row came from); the output stays as it always was.
    return [{k: v for k, v in r.items() if k != "source"} for r in _rows(sql, tuple(params))]


def summary(last: int = 7) -> dict[str, Any]:
    start, end = _range(None, None, last)
    return {"from": start, "to": end, "days": days(start, end),
            "activities": activities(start, end)}


def day_sources() -> list[str]:
    return list(endpoints.DAILY)


def activity_sources() -> list[str]:
    return [*endpoints.ACTIVITY, *ACTIVITY_EXTRA_SOURCES]


def day(ds: str, source: str | None = None) -> Any:
    ds = check_date(ds)
    if source is not None:
        source = check_source(source, day_sources(), "day")
    a = storage.archive()
    dkey = paths.day_key(ds)
    manifest = _read(f"{dkey}/day.json")
    if manifest is None:
        raise NotFound(f"Nothing stored for {ds}", hint="`fitdata status` shows which days the archive has.")
    if source:
        data = _read(f"{dkey}/processed/{source}.json")
        if data is None:
            raise NotFound(f"No '{source}' for {ds}. Available: {', '.join(available_day_sources(ds))}")
        return data
    return {"date": ds, "headline": manifest.get("headline"), "complete": manifest.get("complete"),
            "fetched_at": manifest.get("fetched_at"), "activities": manifest.get("activities"),
            "sources_with_data": available_day_sources(ds),
            "raw_files": [f for f in a.children(f"{dkey}/raw/wellness")[1] if f.endswith(".fit")],
            "dir": a.location(dkey)}


def available_day_sources(ds: str) -> list[str]:
    m = _read(f"{paths.day_key(ds)}/day.json") or {}
    return [k for k, v in (m.get("sources") or {}).items() if v.get("status") == "ok"]


def activity(activity_id: str, source: str | None = None, records: bool = False,
             every: int = 1, columns: str | None = None) -> Any:
    activity_id = check_activity_id(activity_id)
    if source is not None:
        source = check_source(source, activity_sources(), "activity")
    a = storage.archive()
    akey = storage.find_activity(a, activity_id)
    if akey is None:
        raise NotFound(f"No activity {activity_id}", hint="Run `fitdata activities` to see the ids you have.")
    if records:
        data = a.read(f"{akey}/derived/records.csv")
        if data is None:
            raise NotFound("No decoded records for this activity")
        keep = [c.strip() for c in columns.split(",")] if columns else None
        reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"), newline=""))
        rows = [r for i, r in enumerate(reader) if i % max(every, 1) == 0]
        if keep:
            rows = [{k: r.get(k) for k in keep} for r in rows]
        return rows
    if source:
        data = _read(f"{akey}/processed/{source}.json")
        if data is None:
            raise NotFound(f"No '{source}' for activity {activity_id}")
        return data
    m = _read(f"{akey}/activity.json") or {}
    m = {k: v for k, v in m.items() if k not in ("sources",)}
    m["sources_with_data"] = sorted(f[:-5] for f in a.children(f"{akey}/processed")[1] if f.endswith(".json"))
    m["files"] = sorted(e.key[len(akey) + 1:] for e in a.walk(akey))
    m["dir"] = a.location(akey)
    return m


# Everything `fitdata sql` may do: read the shared views and fitdata-garmin's own table, and call
# ordinary SQL functions. Anything else (ATTACH, PRAGMA, writes, VACUUM INTO, loading extensions,
# transactions, reading another app's tables or sqlite_master) is refused by SQLite itself before
# the query runs.
_SQL_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}
# SQLite's information tables that are not named sqlite_* or pragma_*.
_SQL_DENIED_TABLES = {"dbstat"}


def _names_rows_table(query: str) -> bool:
    """True when the query itself names a shared table's rows (days__rows...), in any spelling.

    SQLite resolves the names: the query is prepared (EXPLAIN, never run) on an empty in-memory
    database where the views exist but read from tables of other names. Any name SQLite cannot
    find there that ends in __rows was written in the query, not reached through a view."""
    text = index.SCHEMA_SQL.read_text(encoding="utf-8").replace('__rows"', '__sealed"')
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(text)
        mem.execute("PRAGMA query_only = ON")
        mem.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        mem.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in _SCRATCH_ACTIONS else sqlite3.SQLITE_DENY)
        try:
            mem.execute("EXPLAIN " + query).fetchmany(1)
        except (sqlite3.Error, sqlite3.Warning) as e:
            return "__rows" in str(e).lower()
        return False
    finally:
        mem.close()


_SCRATCH_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _sql_authorizer_for(names: dict[str, str], stored: set[str]):
    """`names`: what may be read (readable()); `stored`: every table and view in the file."""
    views = {n for n, kind in names.items() if kind == "view"}

    def authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None,
                   view: str | None) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # ATTACH is refused and the attach limit is 0, so the index ("main") is the only database
            # open anyway; SQLite names no database for count(*) and for reads of a WITH table.
            if dbname not in ("main", None):
                return sqlite3.SQLITE_DENY
            table = (arg1 or "").lower()
            if table in names:
                return sqlite3.SQLITE_OK
            # A shared view reads its own rows table (days reads days__rows). SQLite also gives a
            # WITH table's name here, so a query that names a rows table itself is refused before
            # it runs (_names_rows_table); this check is the second line.
            if table.endswith("__rows") and view in views and view == table[: -len("__rows")]:
                return sqlite3.SQLITE_OK
            # Anything else stored in the file (another app's table, a rows table read directly,
            # SQLite's own tables) and SQLite's built-in information tables are refused.
            if table in stored or table.startswith(("sqlite_", "pragma_")) or table in _SQL_DENIED_TABLES:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK  # a WITH table of the query itself, or json_each / json_tree
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _SQL_DENIED_FUNCTIONS else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return authorizer


def sql(query: str, limit: int | None = SQL_DEFAULT_LIMIT, timeout_s: float = SQL_TIMEOUT_S) -> list[dict[str, Any]]:
    """Run one read-only SELECT on the index and return at most `limit` rows."""
    if limit is not None and limit < 1:
        raise BadInput("--limit must be 1 or more: it is the largest number of rows to show.")
    db = _db()
    try:
        with hold_lock(paths.STATE / "sql.lock", busy=SQL_BUSY, hint=SQL_BUSY_HINT):
            return _sql(db, query, limit, timeout_s)
    finally:
        db.close()


# One `fitdata sql` at a time per archive: each may use up to SQL_MAX_HEAP_BYTES of memory, so
# many at once could still exhaust the machine. The lock lives in .state/, which no read command
# can otherwise write.
SQL_BUSY = "Another fitdata sql query is running on this archive, so this one did not start."
SQL_BUSY_HINT = f"Try again in a moment: a query runs for {SQL_TIMEOUT_S} seconds at most."


def _sql(db: sqlite3.Connection, query: str, limit: int | None, timeout_s: float) -> list[dict[str, Any]]:
    db.execute("PRAGMA query_only = ON")
    # Caps the memory SQLite may use for this query (this CLI runs one query per process).
    db.execute(f"PRAGMA hard_heap_limit = {int(SQL_MAX_HEAP_BYTES)}")
    # Sorting and grouping stay in that capped memory instead of spilling to temporary files.
    db.execute("PRAGMA temp_store = MEMORY")
    db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_VALUE_BYTES)
    names = readable()
    stored = {str(n).lower() for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    db.set_authorizer(_sql_authorizer_for(names, stored))
    deadline = time.monotonic() + timeout_s
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), SQL_PROGRESS_STEPS)
    try:
        if _names_rows_table(query):
            raise sqlite3.DatabaseError("not authorized")
        cur = db.execute(query)
        rows = _fetch(cur, limit)
    except sqlite3.ProgrammingError as e:
        if "one statement" in str(e):
            raise BadInput("fitdata sql runs one statement at a time. Remove the extra ';' "
                           "statements and run each SELECT on its own.") from None
        raise BadInput(f"SQLite could not run that query ({e}). Check it and try again.") from None
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if any(w in msg for w in ("not authorized", "authorization denied", "prohibited", "readonly", "read-only",
                                  "cannot modify")):
            raise BadInput(f"fitdata sql can only read the index ({', '.join(names)}). That query tries "
                           "to read something else, change something, attach another file, change a "
                           "setting (PRAGMA) or load code, so it was refused. Use a single SELECT; "
                           "`fitdata schema` lists the tables and columns.") from None
        if "no such table" in msg:
            db.set_authorizer(None)  # the sandbox is done with; this only looks at the file's own list
            if not _has_index(db):
                raise NotFound(NO_INDEX, hint=NO_INDEX_HINT) from None
        if "interrupted" in msg:
            raise BadInput(f"That query was still running after {timeout_s:g} seconds, so fitdata "
                           "stopped it. Narrow it down, for example with a WHERE on date.") from None
        if "too big" in msg or "out of memory" in msg or "no memory" in msg:
            raise BadInput("That query builds a value or a result far larger than anything in the "
                           f"index (one value may be at most {SQL_MAX_VALUE_BYTES // 1000} KB), so "
                           "fitdata stopped it to protect this computer's memory. Ask for fewer or "
                           "smaller values.") from None
        raise BadInput(f"SQLite could not run that query: {msg}. Check the table and column names "
                       "with `fitdata schema` and try again.") from None
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        print(f"Showing the first {limit} rows; there are more. Add --limit N to see more, "
              f"or narrow the query.", file=sys.stderr)
    return [dict(r) for r in rows]


def _value_size(v: Any) -> int:
    return len(v) if isinstance(v, (str, bytes)) else 8


def _fetch(cur: sqlite3.Cursor, limit: int | None) -> list[sqlite3.Row]:
    """At most limit + 1 rows, refusing results larger than SQL_MAX_RESULT_BYTES in total."""
    rows: list[sqlite3.Row] = []
    total = 0
    while limit is None or len(rows) <= limit:
        batch = cur.fetchmany(min(256, (limit + 1 - len(rows)) if limit is not None else 256))
        if not batch:
            break
        for r in batch:
            total += sum(_value_size(v) for v in r)
            if total > SQL_MAX_RESULT_BYTES:
                raise BadInput(f"That query's result is larger than {SQL_MAX_RESULT_BYTES // 2**20} MB, so "
                               "fitdata stopped it. Ask for fewer rows or columns (for example with "
                               "--limit or a WHERE on date).")
        rows.extend(batch)
    return rows


def _has_index(db: sqlite3.Connection) -> bool:
    return db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({', '.join('?' * len(SHARED_VIEWS))})",
                      SHARED_VIEWS).fetchone()[0] == len(SHARED_VIEWS)


SHARED_VIEWS = index.SHARED


def schema() -> dict[str, Any]:
    with _db() as db:
        if not _has_index(db):
            raise NotFound(NO_INDEX, hint=NO_INDEX_HINT)
        tables = {}
        for name in readable():
            cols = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
            if cols:
                tables[name] = cols
    return {"database": str(paths.DB), "tables": tables,
            "notes": "Read-only. days and activities are the project's shared views: one row per day and per "
                     "activity, from every app of the project (source says which); garmin_files lists "
                     "fitdata-garmin's stored files. days.date and activities.date are local calendar dates "
                     "(YYYY-MM-DD). Full detail lives in the JSON/FIT files: use `fitdata day DATE --source "
                     "NAME` and `fitdata activity ID --source NAME|--records`."}


def sources() -> dict[str, Any]:
    d = endpoints.DESCRIPTIONS
    return {"daily": {k: d.get(k, "") for k in [*endpoints.DAILY, "raw_wellness_fit"]},
            "activity": {k: d.get(k, "") for k in [*endpoints.ACTIVITY, "raw_fit"]},
            "account": list(endpoints.ACCOUNT)}


def _read(key: str) -> Any:
    # The archive checks the key and never hands out the saved login, even through a symlink.
    return storage.archive().read_json(key)
