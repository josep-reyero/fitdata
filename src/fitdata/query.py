"""Read-only access to the archive, shaped for people and for AI agents (`--json`)."""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from typing import Any

from . import endpoints, paths
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


def _db() -> sqlite3.Connection:
    if not paths.DB.exists():
        raise SystemExit(f"No index at {paths.DB}. Run: fitdata index (or fitdata sync)")
    # as_uri() escapes characters such as "?" or "#" in the folder name, so they can't add URI options.
    db = sqlite3.connect(f"{paths.DB.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _db() as db:
        return [dict(r) for r in db.execute(sql, params)]


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
        rows = [{k: v for k, v in r.items() if k not in ("dir", "fetched_at", "complete")} for r in rows]
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
    return _rows(sql, tuple(params))


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
    ddir = paths.day_dir(ds)
    manifest = _read(ddir / "day.json")
    if manifest is None:
        raise SystemExit(f"Nothing stored for {ds}")
    if source:
        data = _read(ddir / "processed" / f"{source}.json")
        if data is None:
            raise SystemExit(f"No '{source}' for {ds}. Available: {', '.join(available_day_sources(ds))}")
        return data
    return {"date": ds, "headline": manifest.get("headline"), "complete": manifest.get("complete"),
            "fetched_at": manifest.get("fetched_at"), "activities": manifest.get("activities"),
            "sources_with_data": available_day_sources(ds),
            "raw_files": sorted(p.name for p in (ddir / "raw" / "wellness").glob("*.fit")),
            "dir": str(ddir)}


def available_day_sources(ds: str) -> list[str]:
    m = _read(paths.day_dir(ds) / "day.json") or {}
    return [k for k, v in (m.get("sources") or {}).items() if v.get("status") == "ok"]


def activity(activity_id: str, source: str | None = None, records: bool = False,
             every: int = 1, columns: str | None = None) -> Any:
    activity_id = check_activity_id(activity_id)
    if source is not None:
        source = check_source(source, activity_sources(), "activity")
    adir = paths.find_activity_dir(activity_id)
    if adir is None:
        raise SystemExit(f"No activity {activity_id}")
    if records:
        path = adir / "derived" / "records.csv"
        if not path.exists() or paths.is_secret(path):
            raise SystemExit("No decoded records for this activity")
        keep = [c.strip() for c in columns.split(",")] if columns else None
        with open(path, newline="") as fh:
            rows = [r for i, r in enumerate(csv.DictReader(fh)) if i % max(every, 1) == 0]
        if keep:
            rows = [{k: r.get(k) for k in keep} for r in rows]
        return rows
    if source:
        data = _read(adir / "processed" / f"{source}.json")
        if data is None:
            raise SystemExit(f"No '{source}' for activity {activity_id}")
        return data
    m = _read(adir / "activity.json") or {}
    m = {k: v for k, v in m.items() if k not in ("sources",)}
    m["sources_with_data"] = sorted(p.stem for p in (adir / "processed").glob("*.json"))
    m["files"] = sorted(str(p.relative_to(adir)) for p in adir.rglob("*") if p.is_file())
    m["dir"] = str(adir)
    return m


# Everything `fitdata sql` may do: read columns of the index's own tables and call ordinary
# SQL functions. Anything else (ATTACH, PRAGMA, writes, VACUUM INTO, loading extensions,
# transactions) is refused by SQLite itself before the query runs.
_SQL_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


def _sql_authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None,
                    _trigger: str | None) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # SQLite names no database for count(*) and for reads of a WITH table. ATTACH is refused
        # and the attach limit is 0, so the index ("main") is the only database open anyway.
        return sqlite3.SQLITE_OK if dbname in ("main", None) else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _SQL_DENIED_FUNCTIONS else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def sql(query: str, limit: int | None = SQL_DEFAULT_LIMIT, timeout_s: float = SQL_TIMEOUT_S) -> list[dict[str, Any]]:
    """Run one read-only SELECT on the index and return at most `limit` rows."""
    if limit is not None and limit < 1:
        raise BadInput("--limit must be 1 or more: it is the largest number of rows to show.")
    db = _db()
    try:
        db.execute("PRAGMA query_only = ON")
        # Caps the memory SQLite may use for this query (this CLI runs one query per process).
        db.execute(f"PRAGMA hard_heap_limit = {int(SQL_MAX_HEAP_BYTES)}")
        db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_VALUE_BYTES)
        db.set_authorizer(_sql_authorizer)
        deadline = time.monotonic() + timeout_s
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), SQL_PROGRESS_STEPS)
        try:
            cur = db.execute(query)
            rows = _fetch(cur, limit)
        except sqlite3.ProgrammingError as e:
            if "one statement" in str(e):
                raise BadInput("fitdata sql runs one statement at a time. Remove the extra ';' "
                               "statements and run each SELECT on its own.") from None
            raise BadInput(f"SQLite could not run that query ({e}). Check it and try again.") from None
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "not authorized" in msg or "authorization denied" in msg or "readonly" in msg or "read-only" in msg:
                raise BadInput("fitdata sql can only read the index tables (days, activities, files). "
                               "That query tries to change something, attach another file, change a "
                               "setting (PRAGMA) or load code, so it was refused. Use a single SELECT; "
                               "`fitdata schema` lists the tables and columns.") from None
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
    finally:
        db.close()
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


def schema() -> dict[str, Any]:
    with _db() as db:
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            tables[name] = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
    return {"database": str(paths.DB), "tables": tables,
            "notes": "Read-only. days.date and activities.date are local calendar dates (YYYY-MM-DD). "
                     "Full detail lives in the JSON/FIT files: use `fitdata day DATE --source NAME` and "
                     "`fitdata activity ID --source NAME|--records`."}


def sources() -> dict[str, Any]:
    d = endpoints.DESCRIPTIONS
    return {"daily": {k: d.get(k, "") for k in [*endpoints.DAILY, "raw_wellness_fit"]},
            "activity": {k: d.get(k, "") for k in [*endpoints.ACTIVITY, "raw_fit"]},
            "account": list(endpoints.ACCOUNT)}


def _read(path) -> Any:
    if paths.is_secret(path):  # never hand out the saved login, even through a symlink
        return None
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
