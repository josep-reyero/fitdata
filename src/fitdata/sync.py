"""Fetch from Garmin Connect into the per-day folders. Safe to re-run: complete days and
activities are skipped; recent days are refreshed because Garmin keeps updating them
(sleep, HRV and readiness land the next morning)."""
from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import endpoints, fit, paths
from .client import NotLoggedIn, Session

log = logging.getLogger("fitdata")

# A day counts as complete once fetched this many days after it ended.
SETTLE_DAYS = 2
VERSION = 1


# ------------------------------------------------------------------ file helpers

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    tmp.replace(path)


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


def _fetch(s: Session, fn, *args) -> tuple[str, Any, str | None]:
    """-> (status, data, error). status: ok | empty | error."""
    try:
        data = s.call(fn, s.api, *args)
    except NotLoggedIn:
        raise
    except Exception as e:  # Garmin answers "no data" in many ways (404, 204, errors)
        return "error", None, f"{type(e).__name__}: {str(e)[:300]}"
    return ("empty" if _is_empty(data) else "ok"), data, None


# Safe unpacking of Garmin's downloads: only the plain file name of each entry is used (never its
# folders), the name must be ordinary, and the total size is capped so a broken or hostile zip
# can't write outside `dest` or fill the disk.
UNZIP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
UNZIP_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB per download; real FIT downloads are a few MB


def _write_new(path: Path, chunks) -> int:
    """Write to path.tmp (never following a symlink there), then move it into place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
                size += len(chunk)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return size


def _unzip(blob: bytes, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    if blob[:2] != b"PK":  # a bare FIT file
        _write_new(dest / "file.fit", [blob])
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

            def chunks(info=info):
                nonlocal budget
                with z.open(info) as src:
                    while chunk := src.read(1024 * 1024):
                        budget -= len(chunk)
                        if budget < 0:
                            raise ValueError(
                                f"A Garmin download unpacked to more than {UNZIP_MAX_BYTES // 2**20} MB, "
                                "which is far larger than any real FIT file, so fitdata stopped unpacking "
                                "it. The next sync will try again.")
                        yield chunk

            _write_new(dest / name, chunks())
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


def _day_headline(proc: Path) -> dict[str, Any]:
    """A few key numbers copied into day.json for quick reading (full data stays in processed/)."""
    summ = read_json(proc / "daily_summary.json") or {}
    sleep = read_json(proc / "sleep.json") or {}
    hrv = read_json(proc / "hrv.json") or {}
    ready = read_json(proc / "training_readiness.json")
    maxm = read_json(proc / "max_metrics.json")
    status = read_json(proc / "training_status.json") or {}
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


def sync_day(s: Session, d: date, force: bool = False) -> dict[str, Any]:
    ds = d.isoformat()
    ddir = paths.day_dir(ds)
    manifest_path = ddir / "day.json"
    manifest = read_json(manifest_path, {}) or {}
    if manifest.get("complete") and not force:
        return manifest

    proc = ddir / "processed"
    sources: dict[str, Any] = {}
    for name, fn in endpoints.DAILY.items():
        status, data, err = _fetch(s, fn, ds)
        if status != "error":
            write_json(proc / f"{name}.json", data)
        sources[name] = {"status": status, **({"error": err} if err else {})}

    # The watch's original all-day FIT files for this date.
    raw = ddir / "raw" / "wellness"
    try:
        blob = s.call(lambda g, x: g.download_health_snapshot(x), s.api, ds)
        files = _unzip(blob, raw) if blob else []
        sources["raw_wellness_fit"] = {"status": "ok" if files else "empty", "files": files}
    except NotLoggedIn:
        raise
    except Exception as e:
        sources["raw_wellness_fit"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}

    settled = (date.today() - d).days >= SETTLE_DAYS
    headline = _day_headline(proc)
    # Garmin returns zero-filled objects even for days before you had a watch, so "has data"
    # means real recordings: raw files, steps, or heart rate.
    has_data = bool(sources["raw_wellness_fit"].get("files")) or bool(headline.get("steps")) \
        or headline.get("resting_hr") is not None or (ddir / "activities").exists()
    manifest.update({
        "date": ds,
        "fetched_at": _now(),
        "complete": settled,
        "has_data": has_data,
        "headline": headline,
        "sources": sources,
        "activities": sorted(p.name for p in (ddir / "activities").glob("*") if p.is_dir()),
        "layout_version": VERSION,
    })
    write_json(manifest_path, manifest)
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


def sync_activity(s: Session, item: dict[str, Any], force: bool = False) -> Path:
    aid = item["activityId"]
    start = item.get("startTimeLocal") or ""
    d = start[:10]
    adir = paths.find_activity_dir(aid) or paths.activity_dir(d, start, (item.get("activityType") or {}).get("typeKey", ""), aid)
    manifest_path = adir / "activity.json"
    manifest = read_json(manifest_path, {}) or {}
    if manifest.get("complete") and not force:
        return adir

    write_json(adir / "processed" / "list_entry.json", item)
    sources: dict[str, Any] = {}
    for name, fn in endpoints.ACTIVITY.items():
        status, data, err = _fetch(s, fn, aid)
        if status != "error":
            write_json(adir / "processed" / f"{name}.json", data)
        sources[name] = {"status": status, **({"error": err} if err else {})}

    # Original upload from the watch (FIT, inside a zip).
    files: list[str] = []
    try:
        blob = s.call(lambda g, x: g.download_activity(x, dl_fmt=g.ActivityDownloadFormat.ORIGINAL), s.api, aid)
        files = _unzip(blob, adir / "raw")
        sources["raw_fit"] = {"status": "ok", "files": files}
    except NotLoggedIn:
        raise
    except Exception as e:
        sources["raw_fit"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}

    derived: dict[str, Any] = {}
    fits = [f for f in files if f.lower().endswith(".fit")]
    if fits:
        try:
            derived = fit.derive_activity(adir / "raw" / fits[0], adir / "derived")
        except Exception as e:
            derived = {"error": f"{type(e).__name__}: {e}"}

    summary = read_json(adir / "processed" / "summary.json") or item
    manifest.update({
        **_activity_headline(summary),
        "date": d,
        "fetched_at": _now(),
        "complete": sources["raw_fit"]["status"] == "ok" and sources.get("summary", {}).get("status") == "ok",
        "sources": sources,
        "derived": derived,
        "layout_version": VERSION,
    })
    write_json(manifest_path, manifest)
    log.info("activity %s %s %s: %s", d, manifest.get("type"), aid, manifest.get("name"))
    return adir


def sync_activities(s: Session, end: date, days: int, force: bool = False) -> int:
    start = end - timedelta(days=days - 1)
    items = s.call(lambda g, a, b: g.get_activities_by_date(a, b, sortorder="asc"), s.api, start.isoformat(), end.isoformat())
    new = 0
    touched_days = set()
    for item in items or []:
        existing = paths.find_activity_dir(item["activityId"])
        if existing and (read_json(existing / "activity.json", {}) or {}).get("complete") and not force:
            continue
        sync_activity(s, item, force=force)
        touched_days.add((item.get("startTimeLocal") or "")[:10])
        new += 1
    for ds in touched_days:  # keep each day's list of activities current
        mp = paths.day_dir(ds) / "day.json"
        m = read_json(mp, {}) or {"date": ds}
        m["activities"] = sorted(p.name for p in (paths.day_dir(ds) / "activities").glob("*") if p.is_dir())
        write_json(mp, m)
    log.info("activities %s..%s: %d listed, %d fetched", start, end, len(items or []), new)
    return new


# ------------------------------------------------------------------ account, recent, backfill

def sync_account(s: Session) -> None:
    for name, fn in endpoints.ACCOUNT.items():
        status, data, err = _fetch(s, fn)
        if status != "error":
            write_json(paths.ACCOUNT / f"{name}.json", data)
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
    while d >= floor and done < max_days:
        m = read_json(paths.day_dir(d) / "day.json", {}) or {}
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

def status() -> int:
    state = read_json(paths.STATE / "sync-state.json", {}) or {}
    days = sorted(p.name for p in paths.DAYS.glob("*/*") if (p / "day.json").exists())
    acts = list(paths.DAYS.glob("*/*/activities/*/activity.json"))
    print(f"root:        {paths.ROOT}")
    print(f"login:       {'saved' if paths.TOKENS.exists() else 'missing (run fitdata login)'}")
    print(f"days:        {len(days)}" + (f"  ({days[0]} .. {days[-1]})" if days else ""))
    print(f"activities:  {len(acts)}")
    print(f"last sync:   {state.get('last_sync', 'never')}")
    print(f"backfill:    last run {state.get('backfill_last_run', 'never')}; floor {state.get('backfill_floor', 'not reached')}")
    return 0
