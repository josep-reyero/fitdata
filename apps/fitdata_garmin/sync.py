"""Fetch from Garmin Connect into the per-day folders. Safe to re-run: complete days and
activities are skipped; recent days are refreshed because Garmin keeps updating them
(sleep, HRV and readiness land the next morning).

Every file under garmin/ is written through storage.archive(write=True), by key, so the same code
fills a plain-files archive or a bucket. Sync state (.state/) is always a plain local file."""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import endpoints, fit, paths, storage
from .errors import FitdataError
from .validate import BadInput, check_activity_id, check_date
from .client import NotLoggedIn, RateLimited, Session

log = logging.getLogger("fitdata")

# A day counts as complete once fetched this many days after it ended.
SETTLE_DAYS = 2
VERSION = 1


# ------------------------------------------------------------------ file helpers

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    """A local file outside the archive (sync state), written to a new file and moved into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    storage.write_new(path, json.dumps(data, indent=1, ensure_ascii=False, default=str).encode())


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _is_empty(data: Any) -> bool:
    if data in (None, "", [], {}):
        return True
    if isinstance(data, dict) and all(v in (None, [], {}, "") for v in data.values()):
        return True
    return False


# Problems that stop the whole run instead of being recorded against one source.
STOP = (NotLoggedIn, RateLimited)


def _fetch(s: Session, fn, *args) -> tuple[str, Any, str | None]:
    """-> (status, data, error). status: ok | empty | error."""
    try:
        data = s.call(fn, s.api, *args)
    except STOP:
        raise
    except Exception as e:  # Garmin answers "no data" in many ways (404, 204, errors)
        return "error", None, f"{type(e).__name__}: {str(e)[:300]}"
    return ("empty" if _is_empty(data) else "ok"), data, None


# Safe unpacking of Garmin's downloads: only the plain file name of each entry is used (never its
# folders), the name must be ordinary, and the sizes are capped so a broken or hostile zip can't
# write outside `dest`, fill the disk or the memory. Real FIT files are a few MB.
UNZIP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
UNZIP_MAX_FILE_BYTES = 64 * 1024 * 1024    # one unpacked file
UNZIP_MAX_BYTES = 256 * 1024 * 1024        # everything unpacked from one download


_write_new = storage.write_new


def _unzip(blob: bytes, dest: Path | tuple[storage.Archive, str]) -> list[str]:
    """Unpack a Garmin download into a folder: a Path, or (archive, folder key)."""
    if isinstance(dest, Path):
        dest.mkdir(parents=True, exist_ok=True)
        write = lambda name, chunks: _write_new(dest / name, chunks)  # noqa: E731
    else:
        a, prefix = dest
        write = lambda name, chunks: a.write(f"{prefix}/{name}", chunks)  # noqa: E731
    if blob[:2] != b"PK":  # a bare FIT file
        write("file.fit", [blob])
        return ["file.fit"]
    names: list[str] = []
    seen: set[str] = set()  # case-folded: on macOS "ACT.FIT" and "act.fit" are the same file
    budget = UNZIP_MAX_BYTES
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not UNZIP_NAME_RE.fullmatch(name) or name.endswith(".tmp"):
                log.warning("Skipped a file named %r in a Garmin download: the name is not a plain file "
                            "name, so it was not saved. The other files were kept.", info.filename)
                continue
            if name.casefold() in seen:
                log.warning("Skipped a second file named %r in a Garmin download so it would not replace "
                            "the first one.", name)
                continue

            def chunks(info=info, name=name):
                nonlocal budget
                size = 0
                with z.open(info) as src:
                    while chunk := src.read(1024 * 1024):
                        budget -= len(chunk)
                        size += len(chunk)
                        if size > UNZIP_MAX_FILE_BYTES or budget < 0:
                            limit = (f"{UNZIP_MAX_FILE_BYTES // 2**20} MB for one file" if size > UNZIP_MAX_FILE_BYTES
                                     else f"{UNZIP_MAX_BYTES // 2**20} MB for one download")
                            log.warning("A Garmin download held %r, which unpacked to more than %s. That is "
                                        "far larger than any real FIT file, so fitdata did not save it and stopped "
                                        "unpacking this download; the next sync tries again.",
                                        name, limit)
                            raise ValueError(
                                f"A Garmin download unpacked to more than {limit}, which is far larger than "
                                "any real FIT file, so fitdata stopped unpacking it. The next sync will try again.")
                        yield chunk

            write(name, chunks())
            names.append(name)
            seen.add(name.casefold())
    return sorted(names)


# ------------------------------------------------------------------ days

def _pick(d: dict | None, *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _day_headline(proc: Path | str, a: storage.Archive | None = None) -> dict[str, Any]:
    """A few key numbers copied into day.json for quick reading (full data stays in processed/).
    `proc` is the processed/ folder: a key in archive `a`, or a local Path."""
    def get(name: str) -> Any:
        if a is None:
            return read_json(Path(proc) / f"{name}.json")
        return a.read_json(f"{proc}/{name}.json")
    summ = get("daily_summary") or {}
    sleep = get("sleep") or {}
    hrv = get("hrv") or {}
    ready = get("training_readiness")
    maxm = get("max_metrics")
    status = get("training_status") or {}
    return {
        "steps": summ.get("totalSteps"),
        "distance_m": summ.get("totalDistanceMeters"),
        "calories": summ.get("totalKilocalories"),
        "active_calories": summ.get("activeKilocalories"),
        "floors_up": summ.get("floorsAscended"),
        "resting_hr": summ.get("restingHeartRate"),
        "min_hr": summ.get("minHeartRate"),
        "max_hr": summ.get("maxHeartRate"),
        "stress_avg": summ.get("averageStressLevel"),
        "body_battery_high": summ.get("bodyBatteryHighestValue"),
        "body_battery_low": summ.get("bodyBatteryLowestValue"),
        "intensity_minutes": (summ.get("moderateIntensityMinutes") or 0) + 2 * (summ.get("vigorousIntensityMinutes") or 0)
        if summ else None,
        "sleep_seconds": _pick(sleep, "dailySleepDTO", "sleepTimeSeconds"),
        "sleep_score": _pick(sleep, "dailySleepDTO", "sleepScores", "overall", "value"),
        "hrv_last_night_avg": _pick(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_status": _pick(hrv, "hrvSummary", "status"),
        "training_readiness": _pick(ready, "score"),
        "vo2max_running": _pick(maxm, "generic", "vo2MaxPreciseValue"),
        "training_status": _pick(status, "mostRecentTrainingStatus", "latestTrainingStatusData"),
    }


def _activity_folders(a: storage.Archive, dkey: str) -> list[str]:
    return a.children(f"{dkey}/activities")[0]


def sync_day(s: Session, d: date, force: bool = False) -> dict[str, Any]:
    a = storage.archive(write=True)
    ds = d.isoformat()
    dkey = paths.day_key(ds)
    manifest_key = f"{dkey}/day.json"
    manifest = a.read_json(manifest_key, {}) or {}
    if manifest.get("complete") and not force:
        return manifest

    proc = f"{dkey}/processed"
    sources: dict[str, Any] = {}
    for name, fn in endpoints.DAILY.items():
        status, data, err = _fetch(s, fn, ds)
        if status != "error":
            a.write_json(f"{proc}/{name}.json", data)
        sources[name] = {"status": status, **({"error": err} if err else {})}

    # The watch's original all-day FIT files for this date.
    try:
        blob = s.call(lambda g, x: g.download_health_snapshot(x), s.api, ds)
        files = _unzip(blob, (a, f"{dkey}/raw/wellness")) if blob else []
        sources["raw_wellness_fit"] = {"status": "ok" if files else "empty", "files": files}
    except STOP:
        raise
    except Exception as e:
        sources["raw_wellness_fit"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}

    # A day with an activity entry that could not be used this run is fetched again next time.
    settled = (date.today() - d).days >= SETTLE_DAYS and ds not in _skipped_days
    headline = _day_headline(proc, a)
    activities = _activity_folders(a, dkey)
    # Garmin returns zero-filled objects even for days before you had a watch, so "has data"
    # means real recordings: raw files, steps, or heart rate.
    has_data = bool(sources["raw_wellness_fit"].get("files")) or bool(headline.get("steps")) \
        or headline.get("resting_hr") is not None or bool(activities)
    manifest.update({
        "date": ds,
        "fetched_at": _now(),
        "complete": settled,
        "has_data": has_data,
        "headline": headline,
        "sources": sources,
        "activities": activities,
        "layout_version": VERSION,
    })
    a.write_json(manifest_key, manifest)
    ok = sum(1 for v in sources.values() if v["status"] == "ok")
    log.info("day %s: %d/%d sources with data%s", ds, ok, len(sources), " (complete)" if settled else "")
    return manifest


# ------------------------------------------------------------------ activities

def _activity_headline(summary: dict[str, Any]) -> dict[str, Any]:
    dto = summary.get("summaryDTO") or summary
    atype = summary.get("activityTypeDTO") or summary.get("activityType") or {}
    return {
        "id": summary.get("activityId"),
        "name": summary.get("activityName"),
        "type": atype.get("typeKey"),
        "parent_type": (atype.get("parentTypeId")),
        "event_type": (summary.get("eventTypeDTO") or summary.get("eventType") or {}).get("typeKey"),
        "start_local": dto.get("startTimeLocal") or summary.get("startTimeLocal"),
        "start_utc": dto.get("startTimeGMT") or summary.get("startTimeGMT"),
        "duration_s": dto.get("duration"),
        "moving_s": dto.get("movingDuration"),
        "elapsed_s": dto.get("elapsedDuration"),
        "distance_m": dto.get("distance"),
        "ascent_m": dto.get("elevationGain"),
        "descent_m": dto.get("elevationLoss"),
        "avg_hr": dto.get("averageHR"),
        "max_hr": dto.get("maxHR"),
        "avg_speed_mps": dto.get("averageSpeed"),
        "calories": dto.get("calories"),
        "aerobic_te": dto.get("trainingEffect"),
        "anaerobic_te": dto.get("anaerobicTrainingEffect"),
        "te_label": dto.get("trainingEffectLabel"),
        "training_load": dto.get("activityTrainingLoad"),
        "vo2max": dto.get("vO2MaxValue") or summary.get("vO2MaxValue"),
        "avg_power": dto.get("averagePower"),
        "avg_cadence": dto.get("averageRunCadence") or dto.get("averageBikeCadence"),
        "device": (summary.get("metadataDTO") or {}).get("deviceMetaDataDTO", {}).get("deviceId"),
        "location": summary.get("locationName"),
    }


def sync_activity(s: Session, item: dict[str, Any], force: bool = False) -> str:
    """Fetch one activity into its folder; returns the folder's key."""
    a = storage.archive(write=True)
    aid = item["activityId"]
    start = item.get("startTimeLocal") or ""
    d = start[:10]
    akey = storage.find_activity(a, aid, d) or paths.activity_key(d, start, (item.get("activityType") or {}).get("typeKey", ""), aid)
    manifest_key = f"{akey}/activity.json"
    manifest = a.read_json(manifest_key, {}) or {}
    if manifest.get("complete") and not force:
        return akey

    a.write_json(f"{akey}/processed/list_entry.json", item)
    sources: dict[str, Any] = {}
    for name, fn in endpoints.ACTIVITY.items():
        status, data, err = _fetch(s, fn, aid)
        if status != "error":
            a.write_json(f"{akey}/processed/{name}.json", data)
        sources[name] = {"status": status, **({"error": err} if err else {})}

    # Original upload from the watch (FIT, inside a zip).
    files: list[str] = []
    try:
        blob = s.call(lambda g, x: g.download_activity(x, dl_fmt=g.ActivityDownloadFormat.ORIGINAL), s.api, aid)
        files = _unzip(blob, (a, f"{akey}/raw"))
        sources["raw_fit"] = {"status": "ok", "files": files}
    except STOP:
        raise
    except Exception as e:
        sources["raw_fit"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}

    derived: dict[str, Any] = {}
    fits = [f for f in files if f.lower().endswith(".fit")]
    if fits:
        try:
            data = a.read(f"{akey}/raw/{fits[0]}") or b""
            derived = fit.derive_activity(data, a, f"{akey}/derived", fits[0])
        except STOP:
            raise
        except Exception as e:
            derived = {"error": f"{type(e).__name__}: {e}"}

    summary = a.read_json(f"{akey}/processed/summary.json") or item
    manifest.update({
        **_activity_headline(summary),
        "date": d,
        "fetched_at": _now(),
        "complete": sources["raw_fit"]["status"] == "ok" and sources.get("summary", {}).get("status") == "ok",
        "sources": sources,
        "derived": derived,
        "layout_version": VERSION,
    })
    a.write_json(manifest_key, manifest)
    log.info("activity %s %s %s: %s", d, manifest.get("type"), aid, manifest.get("name"))
    return akey


# An activity dated further ahead than this is a broken entry (time zones put a real one at most a
# day ahead), not something to file under a future day.
MAX_DAYS_AHEAD = 2
# Days whose activity list had an entry that could not be used (this run): not marked complete.
_skipped_days: set[str] = set()


def _check_item(item: Any) -> tuple[str, str]:
    """(activity id, local start) of one entry of Garmin's activity list, or BadInput saying why
    it cannot be used. The entry comes from Garmin and is checked like any outside data."""
    if not isinstance(item, dict):
        raise BadInput(f"the entry is a {type(item).__name__}, not a record of an activity")
    aid = item.get("activityId")
    if isinstance(aid, bool) or not isinstance(aid, (int, str)):
        raise BadInput("it has no usable activity id")
    aid = check_activity_id(aid)
    start = item.get("startTimeLocal")
    if not isinstance(start, str):
        raise BadInput(f"activity {aid} has no usable start time")
    ds = check_date(start[:10], f"start date of activity {aid}")
    if date.fromisoformat(ds) > date.today() + timedelta(days=MAX_DAYS_AHEAD):
        _skipped_days.add(ds)
        raise BadInput(f"activity {aid} is dated {ds}, more than {MAX_DAYS_AHEAD} days in the future")
    atype = item.get("activityType")
    if atype is not None and not (isinstance(atype, dict) and isinstance(atype.get("typeKey", ""), str)):
        _skipped_days.add(ds)
        raise BadInput(f"activity {aid} has an activity type fitdata cannot read")
    return aid, start


def _item_day(item: Any) -> str | None:
    try:
        return check_date(str(item.get("startTimeLocal"))[:10])
    except (BadInput, AttributeError):
        return None


def sync_activities(s: Session, end: date, days: int, force: bool = False) -> int:
    start = end - timedelta(days=days - 1)
    items = s.call(lambda g, a, b: g.get_activities_by_date(a, b, sortorder="asc"), s.api, start.isoformat(), end.isoformat())
    if items is not None and not isinstance(items, list):
        log.warning("Garmin's list of activities for %s..%s was not a list, so fitdata could not read it "
                    "and fetched no activities from it. The next sync asks again.", start, end)
        items = []
    new = skipped = 0
    touched_days = set()
    a = storage.archive(write=True)
    for item in items or []:
        # One broken entry is logged and skipped; it never stops the other activities.
        try:
            aid, start_local = _check_item(item)
            ds = start_local[:10]
            existing = storage.find_activity(a, aid, ds)
            if existing and (a.read_json(f"{existing}/activity.json", {}) or {}).get("complete") and not force:
                continue
            sync_activity(s, item, force=force)
            touched_days.add(ds)
            new += 1
        except (BadInput, KeyError, TypeError, AttributeError, ValueError) as e:
            skipped += 1
            day = _item_day(item)
            if day:
                _skipped_days.add(day)
            why = e.message if isinstance(e, BadInput) else f"its data had an unexpected shape ({type(e).__name__})"
            log.warning("Skipped one entry of Garmin's activity list%s: %s. The other activities were "
                        "fetched; %s", f" for {day}" if day else "", why,
                        "that day stays incomplete, so a later sync tries again." if day
                        else "the entry has no date, so it cannot be tied to a day.")
    for ds in touched_days:  # keep each day's list of activities current
        mk = f"{paths.day_key(ds)}/day.json"
        m = a.read_json(mk, {}) or {"date": ds}
        m["activities"] = _activity_folders(a, paths.day_key(ds))
        a.write_json(mk, m)
    log.info("activities %s..%s: %d listed, %d fetched%s", start, end, len(items or []), new,
             f", {skipped} skipped" if skipped else "")
    return new


# ------------------------------------------------------------------ account, recent, backfill

def sync_account(s: Session) -> None:
    a = storage.archive(write=True)
    for name, fn in endpoints.ACCOUNT.items():
        status, data, err = _fetch(s, fn)
        if status != "error":
            a.write_json(f"{paths.ACCOUNT_KEY}/{name}.json", data)
        log.info("account %s: %s%s", name, status, f" ({err})" if err else "")


def sync_recent(s: Session, days: int, force: bool = False) -> None:
    today = date.today()
    sync_activities(s, today, max(days, 2), force=force)
    for i in range(days):
        sync_day(s, today - timedelta(days=i), force=force)
    state = read_json(paths.STATE / "sync-state.json", {}) or {}
    if state.get("account_synced", "") < (today - timedelta(days=7)).isoformat():
        sync_account(s)
        state["account_synced"] = today.isoformat()
    state["last_sync"] = _now()
    write_json(paths.STATE / "sync-state.json", state)


def backfill(s: Session, max_days: int, until: str | None = None) -> None:
    """Fill in missing or incomplete days going back in time, newest first.
    Stops at `until`, or after 120 consecutive days with no data at all (before the watch/account)."""
    state = read_json(paths.STATE / "sync-state.json", {}) or {}
    floor = date.fromisoformat(until or state.get("backfill_floor") or "2000-01-01")
    d = date.today() - timedelta(days=1)
    done, empty_run = 0, 0
    a = storage.archive(write=True)
    while d >= floor and done < max_days:
        m = a.read_json(f"{paths.day_key(d)}/day.json", {}) or {}
        if not m.get("complete"):
            if done % 30 == 0:  # activities for the next month of history, in one listing
                sync_activities(s, d, 31)
            m = sync_day(s, d)
            done += 1
        empty_run = 0 if m.get("has_data", True) else empty_run + 1
        if empty_run >= 120:
            state["backfill_floor"] = (d + timedelta(days=120)).isoformat()
            log.info("backfill: no data for 120 days before %s; treating that as the start of history", state["backfill_floor"])
            break
        d -= timedelta(days=1)
    state["backfill_last_run"] = _now()
    write_json(paths.STATE / "sync-state.json", state)
    log.info("backfill: %d days fetched this run (%d Garmin calls)", done, s.calls)


# ------------------------------------------------------------------ reading

def status() -> dict[str, Any]:
    from .config import load
    cfg = load()
    state = read_json(paths.STATE / "sync-state.json", {}) or {}
    days, acts, problem = [], 0, None
    try:
        a = storage.archive()
        if a.kind == "files":  # cheaper than a full walk on a large archive; never follows links
            for year in a.children(paths.DAYS_KEY)[0]:
                for d in a.children(f"{paths.DAYS_KEY}/{year}")[0]:
                    dkey = f"{paths.DAYS_KEY}/{year}/{d}"
                    folders, files = a.children(dkey)
                    if "day.json" in files:
                        days.append(d)
                    if "activities" in folders:
                        acts += sum(1 for name in a.children(f"{dkey}/activities")[0]
                                    if "activity.json" in a.children(f"{dkey}/activities/{name}")[1])
        else:
            for e in a.walk(paths.DAYS_KEY):
                parts = e.key.split("/")
                if len(parts) == 5 and parts[4] == "day.json":
                    days.append(parts[3])
                elif len(parts) == 7 and parts[4] == "activities" and parts[6] == "activity.json":
                    acts += 1
    except FitdataError as e:  # say what is wrong instead of failing the overview
        days, acts, problem = [], 0, e.message
    days.sort()
    return {
        "root": str(paths.ROOT),
        "storage": cfg.storage if cfg.storage == "files" else f"bucket {cfg.bucket_name} in {cfg.store_dir}",
        "login": "saved" if paths.TOKENS.exists() else "missing (run fitdata login)",
        "days": len(days), "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
        "activities": acts,
        "last_sync": state.get("last_sync"),
        "backfill_last_run": state.get("backfill_last_run"),
        "backfill_floor": state.get("backfill_floor"),
        "index": str(paths.DB) if paths.DB.exists() else None,
        # How mes and its dashboards find this archive (in ~/.config/mes/config.toml; no symlinks).
        "mes_config": f"[projects.fitdata]\ndata_dir = {json.dumps(str(paths.ROOT))}\n",
        "problem": problem,
    }


def print_status(st: dict[str, Any]) -> None:
    print(f"root:        {st['root']}")
    print(f"storage:     {st['storage']}")
    print(f"login:       {st['login']}")
    print(f"days:        {st['days']}" + (f"  ({st['first_day']} .. {st['last_day']})" if st["days"] else ""))
    print(f"activities:  {st['activities']}")
    print(f"last sync:   {st['last_sync'] or 'never'}")
    print(f"backfill:    last run {st['backfill_last_run'] or 'never'}; floor {st['backfill_floor'] or 'not reached'}")
    print(f"dashboards:  in ~/.config/mes/config.toml: {' '.join(st['mes_config'].split())}")
    if st["problem"]:
        print(f"problem:     {st['problem']}")
