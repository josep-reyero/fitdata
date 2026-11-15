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
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import load

ROOT: Path = load().data_dir
GARMIN = ROOT / "garmin"
DAYS = GARMIN / "days"
ACCOUNT = GARMIN / "account"
STATE = ROOT / ".state"
LOGS = ROOT / "logs"
DB = ROOT / "fitdata.sqlite"
TOKENS = STATE / "garmin-tokens.json"
LOCK = STATE / "sync.lock"


def day_dir(d: date | str) -> Path:
    d = d if isinstance(d, str) else d.isoformat()
    return DAYS / d[:4] / d


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "activity").lower()).strip("_") or "activity"


def activity_dir(d: str, start_local: str, type_key: str, activity_id: int | str) -> Path:
    hhmm = (start_local or "0000")[11:16].replace(":", "") or "0000"
    return day_dir(d) / "activities" / f"{hhmm}-{slug(type_key)}-{activity_id}"


def find_activity_dir(activity_id: int | str) -> Path | None:
    for p in DAYS.glob(f"*/*/activities/*-{activity_id}"):
        return p
    return None
