"""Archive layout under the configured data_dir. The files are the source of truth; the SQLite
index is rebuilt from them at any time.

    <data_dir>/garmin/days/YYYY/YYYY-MM-DD/
        day.json                         manifest: what was fetched, when, status per source, key numbers
        raw/wellness/*.fit               the watch's original all-day files (monitoring, sleep, HRV, metrics)
        processed/<source>.json          Garmin's computed daily data, one file per Garmin Connect source
        activities/<HHMM>-<type>-<id>/
            activity.json                manifest: what the activity is (type, name, times, totals, files)
            raw/*.fit                    the original file the watch uploaded
            processed/<source>.json      Garmin's analysis (summary, splits, zones, weather, ...)
            derived/records.csv          one row per recorded sample, decoded from the FIT
            derived/laps.csv, session.json
    <data_dir>/garmin/account/<source>.json   profile, devices, zones, gear, records (latest)
    <data_dir>/fitdata.sqlite                 index: days, activities, files
    <data_dir>/.state/                        login token, sync state, lock (owner-only)
    <data_dir>/logs/fitdata.log
    <data_dir>/store/                         only with storage = "bucket" (see storage.py)

Everything under garmin/ is reached through `storage.archive()`, by key: a key is the file's path
relative to data_dir, written with "/" (for example garmin/days/2026/2026-11-14/day.json), so the
same keys work whether the archive is plain files or a my-bucket-storage bucket. The index, the
logs and .state/ are always plain files.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import data_dir_only
from .validate import check_activity_id, check_date

ROOT: Path = data_dir_only()
GARMIN = ROOT / "garmin"
DAYS = GARMIN / "days"
ACCOUNT = GARMIN / "account"
STATE = ROOT / ".state"
LOGS = ROOT / "logs"
DB = ROOT / "fitdata.sqlite"
TOKENS = STATE / "garmin-tokens.json"
LOCK = STATE / "sync.lock"


DAYS_KEY = "garmin/days"
ACCOUNT_KEY = "garmin/account"


def day_key(d: date | str) -> str:
    """The key (folder) of one day. Refuses anything but a real YYYY-MM-DD date."""
    d = check_date(d if isinstance(d, str) else d.isoformat())
    return f"{DAYS_KEY}/{d[:4]}/{d}"


def day_dir(d: date | str) -> Path:
    """The folder of one day. Refuses anything but a real YYYY-MM-DD date, so it stays inside DAYS."""
    return ROOT / day_key(d)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "activity").lower()).strip("_") or "activity"


def activity_key(d: str, start_local: str, type_key: str, activity_id: int | str) -> str:
    hhmm = re.sub(r"[^0-9]", "", (start_local or "0000")[11:16]) or "0000"
    return f"{day_key(d)}/activities/{hhmm}-{slug(type_key)}-{check_activity_id(activity_id)}"


def activity_dir(d: str, start_local: str, type_key: str, activity_id: int | str) -> Path:
    return ROOT / activity_key(d, start_local, type_key, activity_id)


# A stored activity folder: garmin/days/YYYY/YYYY-MM-DD/activities/<HHMM>-<type>-<id>
ACTIVITY_KEY_RE = re.compile(r"^garmin/days/\d{4}/\d{4}-\d{2}-\d{2}/activities/[0-9]{1,4}-[a-z0-9_]+-([0-9]{1,20})$")


def find_activity_dir(activity_id: int | str) -> Path | None:
    # Digits only: a "*" or "../" in the id would otherwise widen the search or leave the archive.
    for p in DAYS.glob(f"*/*/activities/*-{check_activity_id(activity_id)}"):
        return p
    return None


def is_secret(path: Path) -> bool:
    """True for anything inside .state/ (the saved Garmin login), even when reached through a
    symlink. Read commands refuse such paths."""
    try:
        path.resolve().relative_to(STATE.resolve())
        return True
    except ValueError:
        return False
