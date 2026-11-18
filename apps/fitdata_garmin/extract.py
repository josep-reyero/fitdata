"""What the index takes from each day's processed/<source>.json files: the numbers Garmin Connect
shows, as plain columns and rows.

Garmin's files are untrusted and uneven: any of them can be missing, empty (`[]`, `{}`), have
null fields, or change shape. Every reader here works on whatever it is given and returns None
(or nothing) for what is not there; nothing in this module raises on bad input, so one odd day
never stops a rebuild. Values keep the form Garmin gave them (144 stays 144, 52.4 stays 52.4).

`day(sources, ...)` returns, for one day:

    days      columns of the shared table days (project.toml), Garmin's value or None
    sleep     a row of garmin_sleep (that night: score and its parts, sleep need, ...), or None
    daily     a row of garmin_daily (stress bands, Body Battery, HRV detail, the week's intensity...)
    training  a row of garmin_training (readiness and its factors, status, load, VO2 max, scores...)
    events    rows of garmin_body_battery_events (sleep, activities, naps and their effect)
    samples   rows of garmin_samples (date, series, at, value): the series through the day and the
              night, at the resolution Garmin gives; only when `intraday` is true
    stages    rows of garmin_sleep_stages (date, start, until, stage, seconds); only with `intraday`

Times are local wall-clock time, "YYYY-MM-DD HH:MM", worked out from the offset Garmin gives
next to each series (its *Local and *GMT timestamps).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# ------------------------------------------------------------------ tables

# Columns this module adds to the shared table days (project.toml [tables.days], after "dir").
DAY_COLUMNS = (
    "sleep_start", "sleep_end", "sleep_deep_seconds", "sleep_light_seconds", "sleep_rem_seconds",
    "sleep_awake_seconds", "nap_seconds", "sleep_hr_avg", "sleep_respiration_avg", "sleep_spo2_avg",
    "skin_temp_deviation_c", "respiration_avg", "spo2_avg", "spo2_low", "steps_goal", "floors_down",
    "moderate_minutes", "vigorous_minutes", "resting_calories", "weight_kg", "bmi", "body_fat_pct",
    "hydration_ml", "hydration_goal_ml",
)
# Columns of the headline (sync._day_headline) the processed files can fill when day.json has none.
HEADLINE_FROM_FILES = (
    "steps", "distance_m", "calories", "active_calories", "floors_up", "resting_hr", "min_hr", "max_hr",
    "stress_avg", "body_battery_high", "body_battery_low", "intensity_minutes", "sleep_seconds", "sleep_score",
    "hrv_last_night_avg", "hrv_status", "training_readiness", "vo2max_running",
)

# The app's own tables: name -> ((column, SQLite type), ...), primary key. "" is a column without
# a declared type, so numbers come back in the form they were written (as project.toml's "number").
N, I, T = "", "INTEGER", "TEXT"
TABLES: dict[str, tuple[tuple[tuple[str, str], ...], tuple[str, ...]]] = {
    "garmin_sleep": ((
        ("date", T), ("score", I), ("quality", T), ("duration_quality", T), ("stress_quality", T),
        ("awake_count_quality", T), ("restlessness_quality", T),
        ("deep_pct", N), ("deep_quality", T), ("deep_ideal_low_pct", N), ("deep_ideal_high_pct", N),
        ("light_pct", N), ("light_quality", T), ("light_ideal_low_pct", N), ("light_ideal_high_pct", N),
        ("rem_pct", N), ("rem_quality", T), ("rem_ideal_low_pct", N), ("rem_ideal_high_pct", N),
        ("awake_count", I), ("restless_moments", I), ("avg_stress", N), ("need_minutes", I),
        ("need_baseline_minutes", I), ("need_feedback", T), ("body_battery_change", I),
        ("body_battery_at_wake", I), ("avg_overnight_hrv", N), ("hrv_status", T), ("resting_hr", I),
        ("lowest_respiration", N), ("highest_respiration", N), ("lowest_spo2", N),
        ("unmeasurable_seconds", I), ("nap_count", I), ("feedback", T), ("insight", T),
        ("window_confirmed", I),
    ), ("date",)),
    "garmin_daily": ((
        ("date", T), ("stress_max", I), ("stress_rest_seconds", I), ("stress_low_seconds", I),
        ("stress_medium_seconds", I), ("stress_high_seconds", I), ("stress_activity_seconds", I),
        ("stress_unmeasured_seconds", I), ("body_battery_charged", I), ("body_battery_drained", I),
        ("body_battery_at_wake", I), ("body_battery_latest", I), ("body_battery_feedback", T),
        ("resting_hr_7d", I), ("hrv_5min_high", N), ("hrv_weekly_avg", N), ("hrv_baseline_low", N),
        ("hrv_baseline_high", N), ("hrv_feedback", T), ("respiration_low", N), ("respiration_high", N),
        ("spo2_7d_avg", N), ("intensity_week_moderate", I), ("intensity_week_vigorous", I),
        ("intensity_week_total", I), ("intensity_week_goal", I), ("highly_active_seconds", I),
        ("active_seconds", I), ("sedentary_seconds", I), ("floors_goal", N), ("sweat_loss_ml", N),
    ), ("date",)),
    "garmin_training": ((
        ("date", T), ("readiness", I), ("readiness_level", T), ("readiness_feedback", T),
        ("readiness_latest", I), ("recovery_time_minutes", I),
        ("sleep_factor_pct", N), ("sleep_factor", T), ("recovery_factor_pct", N), ("recovery_factor", T),
        ("load_factor_pct", N), ("load_factor", T), ("stress_factor_pct", N), ("stress_factor", T),
        ("hrv_factor_pct", N), ("hrv_factor", T), ("sleep_history_factor_pct", N), ("sleep_history_factor", T),
        ("status", T), ("status_code", I), ("acute_load", N), ("chronic_load", N), ("load_ratio", N),
        ("load_ratio_status", T), ("chronic_load_min", N), ("chronic_load_max", N),
        ("aerobic_low_load", N), ("aerobic_low_target_min", N), ("aerobic_low_target_max", N),
        ("aerobic_high_load", N), ("aerobic_high_target_min", N), ("aerobic_high_target_max", N),
        ("anaerobic_load", N), ("anaerobic_target_min", N), ("anaerobic_target_max", N),
        ("load_balance_feedback", T), ("vo2max_running", N), ("vo2max_cycling", N), ("fitness_age", N),
        ("chronological_age", I), ("achievable_fitness_age", N), ("endurance_score", N),
        ("endurance_class", T), ("hill_score", N), ("hill_strength", N), ("hill_endurance", N),
        ("race_5k_seconds", N), ("race_10k_seconds", N), ("race_half_seconds", N),
        ("race_marathon_seconds", N), ("heat_acclimation_pct", N), ("altitude_acclimation_m", N),
    ), ("date",)),
    "garmin_body_battery_events": ((
        ("date", T), ("start", T), ("type", T), ("duration_seconds", I), ("impact", I), ("feedback", T),
    ), ("date", "start", "type")),
    "garmin_samples": ((
        ("date", T), ("series", T), ("at", T), ("value", N),
    ), ("date", "series", "at")),
    "garmin_sleep_stages": ((
        ("date", T), ("start", T), ("until", T), ("stage", T), ("seconds", I),
    ), ("date", "start")),
}
# Which tables hold intraday detail (kept for the most recent days only).
INTRADAY_TABLES = ("garmin_samples", "garmin_sleep_stages")

# The series of garmin_samples.
SERIES = {
    "heart_rate": "heart rate through the day, beats per minute (Garmin: every 2 minutes)",
    "stress": "stress level through the day, 0 to 100 (every 3 minutes; off-wrist and moving left out)",
    "body_battery": "Body Battery through the day, 0 to 100 (every 3 minutes)",
    "respiration": "breaths per minute through the day",
    "spo2": "blood oxygen, percent (hourly averages, or each reading when the watch measures all day)",
    "steps": "steps in each 15-minute period, at its start",
    "sleep_heart_rate": "heart rate during the night",
    "sleep_hrv": "HRV during the night, milliseconds (every 5 minutes)",
    "sleep_stress": "stress during the night",
    "sleep_body_battery": "Body Battery during the night",
    "sleep_respiration": "breaths per minute during the night",
}
STAGES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def ddl(name: str) -> list[str]:
    """CREATE TABLE for one of this app's tables. Every key starts with the date, so the primary
    key is also the index for "WHERE date = ..." (no other index is needed)."""
    cols, key = TABLES[name]
    lines = [f'  "{c}" {t}'.rstrip() + (" NOT NULL" if c in key else "") for c, t in cols]
    lines.append(f'  PRIMARY KEY ({", ".join(chr(34) + k + chr(34) for k in key)})')
    return [f'CREATE TABLE "{name}" (\n' + ",\n".join(lines) + "\n)" + (" WITHOUT ROWID" if len(key) > 1 else "")]


def columns(name: str) -> tuple[str, ...]:
    return tuple(c for c, _ in TABLES[name][0])


# ------------------------------------------------------------------ safe readers

def _get(d: Any, *keys: Any) -> Any:
    """d[k1][k2]... where every step may be missing or of the wrong type: None then."""
    cur = d
    for k in keys:
        if isinstance(k, int):
            if not isinstance(cur, list) or not -len(cur) <= k < len(cur):
                return None
        elif not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _num(v: Any) -> int | float | None:
    """A finite number as Garmin gave it, else None (True/False, text, NaN are not numbers)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


def _int(v: Any) -> int | None:
    n = _num(v)
    if n is None:
        return None
    return int(round(n)) if isinstance(n, float) else n


def _pos(v: Any) -> int | float | None:
    """A reading that Garmin marks as missing with a negative number (-1 off wrist, -2 moving)."""
    n = _num(v)
    return n if n is not None and n >= 0 else None


def _text(v: Any, limit: int = 300) -> str | None:
    return v[:limit] if isinstance(v, str) and v.strip() else None


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _dict(v: Any) -> dict:
    """A dict, or the first dict of a list (several sources wrap their one record in a list)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        for x in v:
            if isinstance(x, dict):
                return x
    return {}


def _dicts(v: Any) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


# ------------------------------------------------------------------ time

_EPOCH = datetime(1970, 1, 1)


def _parse_gmt(v: Any) -> datetime | None:
    """A Garmin timestamp: epoch milliseconds, or text like 2026-11-14T22:41:00.0 (naive)."""
    n = _num(v)
    if n is not None:
        if not 0 < n < 10**14:
            return None
        return _EPOCH + timedelta(milliseconds=n)
    if isinstance(v, str) and 10 <= len(v) <= 40:
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _offset(*pairs: tuple[Any, Any]) -> timedelta | None:
    """The local time's offset from GMT, from the first (local, gmt) pair that has both."""
    for local, gmt in pairs:
        lo, gm = _parse_gmt(local), _parse_gmt(gmt)
        if lo is not None and gm is not None:
            off = lo - gm
            if abs(off) <= timedelta(hours=15):
                return off
    return None


def _local(ts: Any, offset: timedelta | None) -> str | None:
    """'YYYY-MM-DD HH:MM' of a GMT timestamp in local time (GMT when the offset is not known)."""
    dt = _parse_gmt(ts)
    if dt is None:
        return None
    return (dt + (offset or timedelta())).strftime("%Y-%m-%d %H:%M")


def _local_text(v: Any) -> str | None:
    """'YYYY-MM-DD HH:MM' of a timestamp that is already local (Garmin's *Local values)."""
    dt = _parse_gmt(v)
    return dt.strftime("%Y-%m-%d %H:%M") if dt is not None else None


# ------------------------------------------------------------------ series

def _indexes(descriptors: Any, *wanted: str) -> dict[str, int]:
    """Column positions in a values array, from its descriptor list ({index, key} or
    {...DescriptorIndex, ...DescriptorKey})."""
    out: dict[str, int] = {}
    for d in _dicts(descriptors):
        idx = next((d[k] for k in d if k.lower().endswith("index")), None)
        key = next((d[k] for k in d if k.lower().endswith("key")), None)
        if isinstance(key, str) and isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < 16:
            out[key] = idx
    return {w: out[w] for w in wanted if w in out}


def _pairs(values: Any, ts_at: int = 0, value_at: int = 1) -> Iterable[tuple[Any, Any]]:
    """(timestamp, value) from a values array of rows (lists)."""
    if not isinstance(values, list):
        return
    for row in values:
        if isinstance(row, list) and len(row) > max(ts_at, value_at):
            yield row[ts_at], row[value_at]


def _series_rows(date: str, series: str, points: Iterable[tuple[Any, Any]], offset: timedelta | None,
                 keep=_pos) -> list[tuple]:
    out = []
    for ts, v in points:
        at = _local(ts, offset)
        v = keep(v)
        if at is not None and v is not None:
            out.append((date, series, at, v))
    return out


def _dto_points(items: Any, value: str = "value", ts: str = "startGMT") -> Iterable[tuple[Any, Any]]:
    for x in _dicts(items):
        yield x.get(ts), x.get(value)


# ------------------------------------------------------------------ one day

def _qualifier(v: Any) -> str | None:
    return _text(v, 40)


def _phrase(v: Any) -> str | None:
    """Garmin's feedback keys (PRODUCTIVE_1, GOOD_SLEEP_HISTORY) without the trailing number."""
    t = _text(v, 80)
    return re.sub(r"_\d+$", "", t) if t else None


def _endurance_class(dto: dict) -> str | None:
    score = _num(dto.get("overallScore"))
    if score is None:
        return None
    name = "recreational"
    for key, label in (("classificationLowerLimitIntermediate", "intermediate"),
                       ("classificationLowerLimitTrained", "trained"),
                       ("classificationLowerLimitWellTrained", "well trained"),
                       ("classificationLowerLimitExpert", "expert"),
                       ("classificationLowerLimitSuperior", "superior"),
                       ("classificationLowerLimitElite", "elite")):
        limit = _num(dto.get(key))
        if limit is not None and score >= limit:
            name = label
    return name


def _device_entry(m: Any) -> dict:
    """Of a {deviceId: {...}} map, the primary training device's entry, else the first one."""
    if not isinstance(m, dict):
        return {}
    entries = [v for v in m.values() if isinstance(v, dict)]
    for v in entries:
        if v.get("primaryTrainingDevice") is True:
            return v
    return entries[0] if entries else {}


def _stress_bands(values: list[float], step_s: int) -> dict[str, int]:
    bands = {"rest": 0, "low": 0, "medium": 0, "high": 0}
    for v in values:
        bands["rest" if v <= 25 else "low" if v <= 50 else "medium" if v <= 75 else "high"] += step_s
    return bands


def _step_seconds(points: list[tuple[Any, Any]]) -> int:
    """The usual gap between samples, in seconds (3 minutes for stress)."""
    times = sorted(t for t in (_parse_gmt(ts) for ts, _ in points) if t is not None)
    gaps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a)
    return int(gaps[len(gaps) // 2]) if gaps else 180


def _row(date: str, values: dict[str, Any]) -> dict[str, Any] | None:
    """A table row, or None when Garmin had nothing for it that day."""
    if all(v is None for v in values.values()):
        return None
    return {"date": date, **values}


def day(date: str, sources: dict[str, Any], intraday: bool = True) -> dict[str, Any]:
    """Everything the index keeps for one day, from its processed sources (name -> parsed JSON,
    a missing file is simply not there)."""
    g = sources.get
    summ = _dict(g("daily_summary"))
    sleep_doc = _dict(g("sleep"))
    sleep = _dict(sleep_doc.get("dailySleepDTO"))
    hr = _dict(g("heart_rate"))
    stress = _dict(g("stress"))
    bb = _dict(g("body_battery"))
    resp = _dict(g("respiration"))
    spo2 = _dict(g("spo2"))
    hrv_doc = _dict(g("hrv"))
    hrv = _dict(hrv_doc.get("hrvSummary"))
    im = _dict(g("intensity_minutes"))
    weigh = _dict(g("weigh_ins"))
    hydr = _dict(g("hydration"))
    morning = _dict(g("morning_training_readiness"))
    readiness = [x for x in _dicts(g("training_readiness"))]
    if not readiness and isinstance(g("training_readiness"), dict):
        readiness = [g("training_readiness")]
    status_doc = _dict(g("training_status"))

    day_offset = _offset((hr.get("startTimestampLocal"), hr.get("startTimestampGMT")),
                         (stress.get("startTimestampLocal"), stress.get("startTimestampGMT")),
                         (summ.get("wellnessStartTimeLocal"), summ.get("wellnessStartTimeGmt")),
                         (resp.get("startTimestampLocal"), resp.get("startTimestampGMT")),
                         (bb.get("startTimestampLocal"), bb.get("startTimestampGMT")))
    sleep_offset = _offset((sleep.get("sleepStartTimestampLocal"), sleep.get("sleepStartTimestampGMT")),
                           (sleep.get("sleepEndTimestampLocal"), sleep.get("sleepEndTimestampGMT"))) or day_offset

    # ---- stress and Body Battery samples (stress.json carries both, every 3 minutes)
    st_idx = _indexes(stress.get("stressValueDescriptorsDTOList"), "timestamp", "stressLevel")
    stress_points = list(_pairs(stress.get("stressValuesArray"), st_idx.get("timestamp", 0), st_idx.get("stressLevel", 1)))
    stress_values = [v for v in (_pos(v) for _, v in stress_points) if v is not None]
    bb_idx = _indexes(stress.get("bodyBatteryValueDescriptorsDTOList"), "timestamp", "bodyBatteryLevel")
    bb_points = list(_pairs(stress.get("bodyBatteryValuesArray"), bb_idx.get("timestamp", 0),
                            bb_idx.get("bodyBatteryLevel", 2)))
    if not any(_pos(v) is not None for _, v in bb_points):
        idx = _indexes(bb.get("bodyBatteryValueDescriptorDTOList"), "timestamp", "bodyBatteryLevel")
        bb_points = list(_pairs(bb.get("bodyBatteryValuesArray"), idx.get("timestamp", 0), idx.get("bodyBatteryLevel", 1)))
    bb_values = [v for v in (_pos(v) for _, v in bb_points) if v is not None and v <= 100]
    sleep_bb = [v for v in (_pos(x.get("value")) for x in _dicts(sleep_doc.get("sleepBodyBattery"))) if v is not None]

    stress_avg = _first(_pos(summ.get("averageStressLevel")), _pos(stress.get("avgStressLevel")))
    stress_max = _first(_pos(summ.get("maxStressLevel")), _pos(stress.get("maxStressLevel")))
    computed = _stress_bands(stress_values, _step_seconds(stress_points)) if stress_values else {}

    # ---- sleep
    sleep_scores = _dict(sleep.get("sleepScores"))
    need = _dict(sleep.get("sleepNeed"))
    naps = _dicts(g("naps")) or _dicts(sleep_doc.get("dailyNapDTOS")) or _dicts(sleep.get("dailyNapDTOS"))
    nap_seconds = _first(_int(sleep.get("napTimeSeconds")),
                         sum(_int(n.get("napTimeSec")) or 0 for n in naps) if naps else None)
    skin = _first(_num(sleep_doc.get("avgSkinTempDeviationC")), _num(sleep.get("avgSkinTempDeviationC")),
                  _num(_get(sleep_doc, "skinTempData", "avgSkinTempDeviationC")))
    sleep_spo2 = _first(_num(sleep.get("averageSpO2Value")), _pos(spo2.get("avgSleepSpO2")),
                        _num(_get(sleep_doc, "wellnessSpO2SleepSummaryDTO", "averageSPO2")))
    sleep_seconds = _int(sleep.get("sleepTimeSeconds"))
    has_sleep = bool(sleep_seconds)

    # ---- weight: the day's last weigh-in (Garmin gives grams)
    weights = sorted(_dicts(weigh.get("dateWeightList")), key=lambda w: _num(w.get("timestampGMT")) or _num(w.get("date")) or 0)
    w = weights[-1] if weights else {}
    weight = _num(w.get("weight"))
    if weight is not None:
        weight = round(weight / 1000, 2) if weight > 1000 else weight

    # ---- readiness: the morning report for the day, and the latest score Garmin had
    by_time = sorted(readiness, key=lambda r: str(r.get("timestamp") or ""))
    latest = by_time[-1] if by_time else {}
    first_of_day = next((r for r in by_time if r.get("inputContext") == "AFTER_WAKEUP_RESET"), by_time[0] if by_time else {})
    ready = morning or first_of_day
    ready_score = _first(_int(ready.get("score")), _int(first_of_day.get("score")))

    # ---- training status, load, VO2 max
    ts_entry = _device_entry(_get(status_doc, "mostRecentTrainingStatus", "latestTrainingStatusData"))
    acute = _dict(ts_entry.get("acuteTrainingLoadDTO"))
    balance = _device_entry(_get(status_doc, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")) \
        or _device_entry(_get(_dict(g("training_load_balance")), "metricsTrainingLoadBalanceDTOMap"))
    maxm = _dict(g("max_metrics"))
    vo2_run = _first(_num(_get(maxm, "generic", "vo2MaxPreciseValue")), _num(_get(maxm, "generic", "vo2MaxValue")))
    vo2_ride = _first(_num(_get(maxm, "cycling", "vo2MaxPreciseValue")), _num(_get(maxm, "cycling", "vo2MaxValue")))
    recent_vo2 = _dict(status_doc.get("mostRecentVO2Max"))
    if _text(_get(recent_vo2, "generic", "calendarDate")) == date:
        vo2_run = _first(vo2_run, _num(_get(recent_vo2, "generic", "vo2MaxPreciseValue")))
    if _text(_get(recent_vo2, "cycling", "calendarDate")) == date:
        vo2_ride = _first(vo2_ride, _num(_get(recent_vo2, "cycling", "vo2MaxPreciseValue")))
    endurance = _dict(_dict(g("endurance_score")).get("enduranceScoreDTO"))
    hills = _dicts(_dict(g("hill_score")).get("hillScoreDTOList"))
    hill = next((h for h in reversed(hills) if h.get("calendarDate") == date), hills[-1] if hills else {})
    races = _dicts(g("race_predictions")) or ([g("race_predictions")] if isinstance(g("race_predictions"), dict) else [])
    race = next((r for r in reversed(races) if r.get("calendarDate") == date), races[-1] if races else {})
    fit_age = _dict(g("fitness_age"))
    accl = _dict(g("heat_altitude_acclimation")) or _dict(status_doc.get("heatAltitudeAcclimationDTO"))

    days = {
        # Headline columns, for days whose day.json lacks them (Garmin's daily summary leaves
        # some out, and Body Battery then comes from the samples).
        "steps": _int(summ.get("totalSteps")),
        "distance_m": _num(summ.get("totalDistanceMeters")),
        "calories": _num(summ.get("totalKilocalories")),
        "active_calories": _num(summ.get("activeKilocalories")),
        "floors_up": _num(summ.get("floorsAscended")),
        "resting_hr": _first(_int(summ.get("restingHeartRate")), _int(hr.get("restingHeartRate")),
                             _int(_get(g("resting_hr"), "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", 0, "value"))),
        "min_hr": _first(_int(summ.get("minHeartRate")), _int(hr.get("minHeartRate"))),
        "max_hr": _first(_int(summ.get("maxHeartRate")), _int(hr.get("maxHeartRate"))),
        "stress_avg": stress_avg,
        "body_battery_high": _first(_pos(summ.get("bodyBatteryHighestValue")), max(bb_values) if bb_values else None),
        "body_battery_low": _first(_pos(summ.get("bodyBatteryLowestValue")), min(bb_values) if bb_values else None),
        "intensity_minutes": (None if summ.get("moderateIntensityMinutes") is None and summ.get("vigorousIntensityMinutes") is None
                              else (_int(summ.get("moderateIntensityMinutes")) or 0) + 2 * (_int(summ.get("vigorousIntensityMinutes")) or 0)),
        "sleep_seconds": sleep_seconds,
        "sleep_score": _int(_get(sleep_scores, "overall", "value")),
        "hrv_last_night_avg": _num(hrv.get("lastNightAvg")),
        "hrv_status": _text(hrv.get("status"), 40),
        "training_readiness": ready_score,
        "vo2max_running": vo2_run,
        # New columns.
        "sleep_start": _local_text(sleep.get("sleepStartTimestampLocal")) if has_sleep else None,
        "sleep_end": _local_text(sleep.get("sleepEndTimestampLocal")) if has_sleep else None,
        "sleep_deep_seconds": _int(sleep.get("deepSleepSeconds")) if has_sleep else None,
        "sleep_light_seconds": _int(sleep.get("lightSleepSeconds")) if has_sleep else None,
        "sleep_rem_seconds": _int(sleep.get("remSleepSeconds")) if has_sleep else None,
        "sleep_awake_seconds": _int(sleep.get("awakeSleepSeconds")) if has_sleep else None,
        "nap_seconds": nap_seconds or None,
        "sleep_hr_avg": _num(sleep.get("avgHeartRate")),
        "sleep_respiration_avg": _first(_num(sleep.get("averageRespirationValue")), _pos(resp.get("avgSleepRespirationValue"))),
        "sleep_spo2_avg": sleep_spo2,
        "skin_temp_deviation_c": skin,
        "respiration_avg": _pos(resp.get("avgWakingRespirationValue")),
        "spo2_avg": _pos(spo2.get("averageSpO2")),
        "spo2_low": _pos(spo2.get("lowestSpO2")),
        "steps_goal": _int(summ.get("dailyStepGoal")),
        "floors_down": _num(summ.get("floorsDescended")),
        "moderate_minutes": _int(summ.get("moderateIntensityMinutes")),
        "vigorous_minutes": _int(summ.get("vigorousIntensityMinutes")),
        "resting_calories": _num(summ.get("bmrKilocalories")),
        "weight_kg": weight,
        "bmi": _num(w.get("bmi")),
        "body_fat_pct": _num(w.get("bodyFat")),
        "hydration_ml": _num(hydr.get("valueInML")),
        "hydration_goal_ml": _num(hydr.get("goalInML")),
    }

    def stage(name: str) -> dict[str, Any]:
        s = _dict(sleep_scores.get(f"{name}Percentage"))
        return {f"{name}_pct": _num(s.get("value")), f"{name}_quality": _qualifier(s.get("qualifierKey")),
                f"{name}_ideal_low_pct": _num(s.get("optimalStart")), f"{name}_ideal_high_pct": _num(s.get("optimalEnd"))}

    sleep_row = _row(date, {
        "score": days["sleep_score"],
        "quality": _qualifier(_get(sleep_scores, "overall", "qualifierKey")),
        "duration_quality": _qualifier(_get(sleep_scores, "totalDuration", "qualifierKey")),
        "stress_quality": _qualifier(_get(sleep_scores, "stress", "qualifierKey")),
        "awake_count_quality": _qualifier(_get(sleep_scores, "awakeCount", "qualifierKey")),
        "restlessness_quality": _qualifier(_get(sleep_scores, "restlessness", "qualifierKey")),
        **stage("deep"), **stage("light"), **stage("rem"),
        "awake_count": _int(sleep.get("awakeCount")),
        "restless_moments": _int(sleep_doc.get("restlessMomentsCount")),
        "avg_stress": _num(sleep.get("avgSleepStress")),
        "need_minutes": _int(need.get("actual")),
        "need_baseline_minutes": _int(need.get("baseline")),
        "need_feedback": _phrase(need.get("feedback")),
        "body_battery_change": _int(sleep_doc.get("bodyBatteryChange")),
        "body_battery_at_wake": _int(sleep_bb[-1]) if sleep_bb else None,
        "avg_overnight_hrv": _num(sleep_doc.get("avgOvernightHrv")),
        "hrv_status": _text(sleep_doc.get("hrvStatus"), 40),
        "resting_hr": _int(sleep_doc.get("restingHeartRate")),
        "lowest_respiration": _num(sleep.get("lowestRespirationValue")),
        "highest_respiration": _num(sleep.get("highestRespirationValue")),
        "lowest_spo2": _first(_num(sleep.get("lowestSpO2Value")), _num(_get(sleep_doc, "wellnessSpO2SleepSummaryDTO", "lowestSPO2"))),
        "unmeasurable_seconds": _int(sleep.get("unmeasurableSleepSeconds")) if has_sleep else None,
        "nap_count": len(naps) or None,
        "feedback": _phrase(sleep.get("sleepScoreFeedback")),
        "insight": _phrase(sleep.get("sleepScoreInsight")),
        "window_confirmed": (int(sleep["sleepWindowConfirmed"]) if isinstance(sleep.get("sleepWindowConfirmed"), bool)
                             and has_sleep else None),
    }) if (has_sleep or days["sleep_score"] is not None) else None

    def band(key: str, name: str) -> int | None:
        return _first(_int(summ.get(key)), computed.get(name))

    daily_row = _row(date, {
        "stress_max": stress_max,
        "stress_rest_seconds": band("restStressDuration", "rest"),
        "stress_low_seconds": band("lowStressDuration", "low"),
        "stress_medium_seconds": band("mediumStressDuration", "medium"),
        "stress_high_seconds": band("highStressDuration", "high"),
        "stress_activity_seconds": _int(summ.get("activityStressDuration")),
        "stress_unmeasured_seconds": _int(summ.get("uncategorizedStressDuration")),
        "body_battery_charged": _first(_int(summ.get("bodyBatteryChargedValue")), _int(bb.get("charged"))),
        "body_battery_drained": _first(_int(summ.get("bodyBatteryDrainedValue")), _int(bb.get("drained"))),
        "body_battery_at_wake": _first(_pos(summ.get("bodyBatteryAtWakeTime")), _int(sleep_bb[-1]) if sleep_bb else None),
        "body_battery_latest": _first(_pos(summ.get("bodyBatteryMostRecentValue")),
                                      _int(next((_pos(v) for _, v in reversed(bb_points) if _pos(v) is not None), None))),
        "body_battery_feedback": _phrase(_get(bb, "endOfDayBodyBatteryDynamicFeedbackEvent", "feedbackShortType")),
        "resting_hr_7d": _first(_int(summ.get("lastSevenDaysAvgRestingHeartRate")), _int(hr.get("lastSevenDaysAvgRestingHeartRate"))),
        "hrv_5min_high": _num(hrv.get("lastNight5MinHigh")),
        "hrv_weekly_avg": _num(hrv.get("weeklyAvg")),
        "hrv_baseline_low": _num(_get(hrv, "baseline", "balancedLow")),
        "hrv_baseline_high": _num(_get(hrv, "baseline", "balancedUpper")),
        "hrv_feedback": _phrase(hrv.get("feedbackPhrase")),
        "respiration_low": _pos(resp.get("lowestRespirationValue")),
        "respiration_high": _pos(resp.get("highestRespirationValue")),
        "spo2_7d_avg": _pos(spo2.get("lastSevenDaysAvgSpO2")),
        "intensity_week_moderate": _int(im.get("weeklyModerate")),
        "intensity_week_vigorous": _int(im.get("weeklyVigorous")),
        "intensity_week_total": _int(im.get("weeklyTotal")),
        "intensity_week_goal": _first(_int(im.get("weekGoal")), _int(summ.get("intensityMinutesGoal"))),
        "highly_active_seconds": _int(summ.get("highlyActiveSeconds")),
        "active_seconds": _int(summ.get("activeSeconds")),
        "sedentary_seconds": _int(summ.get("sedentarySeconds")),
        "floors_goal": _num(summ.get("userFloorsAscendedGoal")),
        "sweat_loss_ml": _num(hydr.get("sweatLossInML")),
    })

    def factor(prefix: str) -> dict[str, Any]:
        return {"pct": _num(ready.get(f"{prefix}FactorPercent")), "text": _phrase(ready.get(f"{prefix}FactorFeedback"))}

    f = {k: factor(p) for k, p in (("sleep", "sleepScore"), ("recovery", "recoveryTime"), ("load", "acwr"),
                                   ("stress", "stressHistory"), ("hrv", "hrv"), ("sleep_history", "sleepHistory"))}
    training_row = _row(date, {
        "readiness": ready_score,
        "readiness_level": _text(ready.get("level"), 40),
        "readiness_feedback": _phrase(ready.get("feedbackShort")),
        "readiness_latest": _int(latest.get("score")),
        "recovery_time_minutes": _int(_first(latest.get("recoveryTime"), ready.get("recoveryTime"))),
        **{f"{k}_factor_pct": v["pct"] for k, v in f.items()} | {f"{k}_factor": v["text"] for k, v in f.items()},
        "status": _phrase(ts_entry.get("trainingStatusFeedbackPhrase")),
        "status_code": _int(ts_entry.get("trainingStatus")),
        "acute_load": _num(acute.get("dailyTrainingLoadAcute")),
        "chronic_load": _num(acute.get("dailyTrainingLoadChronic")),
        "load_ratio": _num(acute.get("dailyAcuteChronicWorkloadRatio")),
        "load_ratio_status": _text(acute.get("acwrStatus"), 40),
        "chronic_load_min": _num(acute.get("minTrainingLoadChronic")),
        "chronic_load_max": _num(acute.get("maxTrainingLoadChronic")),
        "aerobic_low_load": _num(balance.get("monthlyLoadAerobicLow")),
        "aerobic_low_target_min": _num(balance.get("monthlyLoadAerobicLowTargetMin")),
        "aerobic_low_target_max": _num(balance.get("monthlyLoadAerobicLowTargetMax")),
        "aerobic_high_load": _num(balance.get("monthlyLoadAerobicHigh")),
        "aerobic_high_target_min": _num(balance.get("monthlyLoadAerobicHighTargetMin")),
        "aerobic_high_target_max": _num(balance.get("monthlyLoadAerobicHighTargetMax")),
        "anaerobic_load": _num(balance.get("monthlyLoadAnaerobic")),
        "anaerobic_target_min": _num(balance.get("monthlyLoadAnaerobicTargetMin")),
        "anaerobic_target_max": _num(balance.get("monthlyLoadAnaerobicTargetMax")),
        "load_balance_feedback": _phrase(balance.get("trainingBalanceFeedbackPhrase")),
        "vo2max_running": vo2_run,
        "vo2max_cycling": vo2_ride,
        "fitness_age": _first(_num(fit_age.get("fitnessAge")), _num(_get(maxm, "generic", "fitnessAge"))),
        "chronological_age": _int(fit_age.get("chronologicalAge")),
        "achievable_fitness_age": _num(fit_age.get("achievableFitnessAge")),
        "endurance_score": _num(endurance.get("overallScore")),
        "endurance_class": _endurance_class(endurance),
        "hill_score": _num(hill.get("overallScore")),
        "hill_strength": _num(hill.get("strengthScore")),
        "hill_endurance": _num(hill.get("enduranceScore")),
        "race_5k_seconds": _pos(race.get("time5K")),
        "race_10k_seconds": _pos(race.get("time10K")),
        "race_half_seconds": _pos(race.get("timeHalfMarathon")),
        "race_marathon_seconds": _pos(race.get("timeMarathon")),
        "heat_acclimation_pct": _num(accl.get("heatAcclimationPercentage")),
        "altitude_acclimation_m": _num(accl.get("altitudeAcclimation")),
    })

    # ---- Body Battery events (sleep, activities, naps, stress) and what each did
    events: dict[tuple, dict] = {}
    raw_events = [x.get("event") for x in _dicts(g("body_battery_events"))] + list(_dicts(bb.get("bodyBatteryActivityEvent")))
    for e in _dicts(raw_events):
        start = _parse_gmt(e.get("eventStartTimeGmt"))
        tz = _num(e.get("timezoneOffset"))
        if start is None:
            continue
        # Garmin's offset is in milliseconds (3600000); seconds are accepted too. Anything past 15
        # hours is not an offset: the day's own one is used instead.
        secs = None if tz is None else tz / 1000 if abs(tz) > 15 * 3600 else tz
        off = timedelta(seconds=secs) if secs is not None and abs(secs) <= 15 * 3600 else (day_offset or timedelta())
        kind = _text(e.get("eventType"), 40) or "event"
        dur = _num(e.get("durationInMilliseconds"))
        row = {"date": date, "start": (start + off).strftime("%Y-%m-%d %H:%M"), "type": kind.lower(),
               "duration_seconds": int(dur // 1000) if dur is not None and dur >= 0 else None,
               "impact": _int(e.get("bodyBatteryImpact")),
               "feedback": _phrase(_first(e.get("shortFeedback"), e.get("feedbackType")))}
        events.setdefault((row["start"], row["type"]), row)

    out = {"days": days, "sleep": sleep_row, "daily": daily_row, "training": training_row,
           "events": list(events.values()), "samples": [], "stages": []}
    if not intraday:
        return out

    # ---- intraday series
    samples: list[tuple] = []
    hr_idx = _indexes(hr.get("heartRateValueDescriptors"), "timestamp", "heartrate")
    samples += _series_rows(date, "heart_rate", _pairs(hr.get("heartRateValues"), hr_idx.get("timestamp", 0),
                                                       hr_idx.get("heartrate", 1)), day_offset)
    samples += _series_rows(date, "stress", stress_points, day_offset)
    samples += _series_rows(date, "body_battery", bb_points, day_offset, keep=lambda v: _pos(v) if (_pos(v) or 0) <= 100 else None)
    r_idx = _indexes(resp.get("respirationValueDescriptorsDTOList"), "timestamp", "respiration")
    samples += _series_rows(date, "respiration", _pairs(resp.get("respirationValuesArray"), r_idx.get("timestamp", 0),
                                                        r_idx.get("respiration", 1)), day_offset)
    continuous = [(x.get("epochTimestamp"), _first(x.get("spo2Reading"), x.get("value")))
                  for x in _dicts(spo2.get("continuousReadingDTOList"))]
    samples += _series_rows(date, "spo2", continuous or list(_pairs(spo2.get("spO2HourlyAverages"))), day_offset)
    samples += _series_rows(date, "steps", ((x.get("startGMT"), x.get("steps")) for x in _dicts(g("steps"))), day_offset)
    if has_sleep:
        samples += _series_rows(date, "sleep_heart_rate", _dto_points(sleep_doc.get("sleepHeartRate")), sleep_offset)
        hrv_points = list(_dto_points(sleep_doc.get("hrvData"))) or \
            list(_dto_points(hrv_doc.get("hrvReadings"), "hrvValue", "readingTimeGMT"))
        samples += _series_rows(date, "sleep_hrv", hrv_points, sleep_offset)
        samples += _series_rows(date, "sleep_stress", _dto_points(sleep_doc.get("sleepStress")), sleep_offset)
        samples += _series_rows(date, "sleep_body_battery", _dto_points(sleep_doc.get("sleepBodyBattery")), sleep_offset)
        samples += _series_rows(date, "sleep_respiration",
                                _dto_points(sleep_doc.get("wellnessEpochRespirationDataDTOList"), "respirationValue",
                                            "startTimeGMT"), sleep_offset)
    seen: dict[tuple, tuple] = {}
    for s in samples:
        seen[s[:3]] = s  # two readings in the same minute: the later one
    out["samples"] = list(seen.values())

    stages: dict[str, dict] = {}
    if has_sleep:
        for lv in _dicts(sleep_doc.get("sleepLevels")):
            a, b = _parse_gmt(lv.get("startGMT")), _parse_gmt(lv.get("endGMT"))
            level = _num(lv.get("activityLevel"))
            name = STAGES.get(int(level)) if level is not None and level == int(level) else None
            if a is None or b is None or b <= a or name is None or (b - a) > timedelta(hours=16):
                continue
            off = sleep_offset or timedelta()
            start = (a + off).strftime("%Y-%m-%d %H:%M")
            stages[start] = {"date": date, "start": start, "until": (b + off).strftime("%Y-%m-%d %H:%M"),
                             "stage": name, "seconds": int((b - a).total_seconds())}
    out["stages"] = [stages[k] for k in sorted(stages)]
    return out
