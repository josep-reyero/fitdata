"""Make the synthetic sample index the dashboards are tested and previewed against.

Every number here is made up by a seeded random generator. Nothing comes from a real watch
or a real account.

The script writes a small fake archive into a temporary folder, as `fitdata garmin sync` would:
day.json, activity.json and, for every day, the processed/<source>.json files in the shapes
Garmin Connect returns them (daily summary, heart rate, stress and Body Battery every few
minutes, sleep with its stages, HRV, readiness, training status, weigh-ins...). Then it runs
fitdata-garmin's own indexer on it, which writes through apps/fitdata_garmin/schema.sql. So the
sample has exactly what a real `fitdata.sqlite` has: the shared tables' rows (days__rows,
activities__rows, all with source = 'fitdata-garmin'), their views (days, activities), and
fitdata-garmin's own tables (garmin_files, garmin_sleep, garmin_daily, garmin_training,
garmin_body_battery_events, garmin_samples, garmin_sleep_stages).

It covers 120 days (2026-01-05 to 2026-05-04) and about 30 activities, with the gaps a real
archive has: a few days without the watch, one day missing from the archive, nights without
sleep or HRV data, a sick week without training, an activity without heart rate, a strength
session without distance, a day without Body Battery, files that are empty, of the wrong shape
or not JSON at all, and a last day that is still in progress (its series stop at 13:20). The
series through the day are kept for the last 7 days only, so the file stays small.

Run from the repository folder:

    uv run python ui/fixtures/make_fixture.py            # writes ui/fixtures/fitdata-sample.db
    uv run python ui/fixtures/make_fixture.py --out /tmp/fitdata.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "fitdata-sample.db"

SEED = 20260105
START = date(2026, 1, 5)
DAYS = 120
END = START + timedelta(days=DAYS - 1)  # 2026-05-04, treated as "today" by the generator

# Keys of sync._day_headline and sync._activity_headline, in their order. tests/test_dashboards.py
# checks these against fitdata itself, so a new headline field shows up as a failing test.
DAY_HEADLINE_KEYS = (
    "steps", "distance_m", "calories", "active_calories", "floors_up", "resting_hr", "min_hr",
    "max_hr", "stress_avg", "body_battery_high", "body_battery_low", "intensity_minutes",
    "sleep_seconds", "sleep_score", "hrv_last_night_avg", "hrv_status", "training_readiness",
    "vo2max_running", "training_status",
)
ACTIVITY_HEADLINE_KEYS = (
    "id", "name", "type", "parent_type", "event_type", "start_local", "start_utc", "duration_s",
    "moving_s", "elapsed_s", "distance_m", "ascent_m", "descent_m", "avg_hr", "max_hr",
    "avg_speed_mps", "calories", "aerobic_te", "anaerobic_te", "te_label", "training_load",
    "vo2max", "avg_power", "avg_cadence", "device", "location",
)

NO_WATCH = {38, 39, 40}        # watch left at home / charging: zero-filled days, has_data false
MISSING = {77}                 # a day that never made it into the archive (no row at all)
SICK = set(range(58, 64))      # higher resting HR, lower HRV, no training
NAPS = {44, 71, 96, 117}       # afternoon naps
INTRADAY_DAYS = 7              # the sample keeps the series through the day for its last 7 days only
FAKE_DEVICE = 1000000001

# Garmin activity type key -> (default activity name, parent type id). The ids are made up.
KINDS = {
    "running": ("Morning Run", 1),
    "trail_running": ("Trail Run", 1),
    "road_biking": ("Road Ride", 2),
    "indoor_cycling": ("Indoor Cycling", 2),
    "hiking": ("Hike", 3),
    "walking": ("Walk", 9),
    "strength_training": ("Strength", 29),
}


def _r1(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def _te_label(aerobic: float | None, anaerobic: float | None) -> str | None:
    if aerobic is None:
        return None
    if (anaerobic or 0) >= 2.5:
        return "ANAEROBIC_CAPACITY"
    if aerobic >= 4.2:
        return "VO2MAX"
    if aerobic >= 3.6:
        return "THRESHOLD"
    if aerobic >= 3.0:
        return "TEMPO"
    if aerobic >= 2.0:
        return "AEROBIC_BASE"
    return "RECOVERY"


def _utc_offset(d: date) -> int:
    # A made-up central-European clock: one hour ahead in winter, two from the last Sunday of March.
    return 2 if d >= date(2026, 3, 29) else 1


def plan_activities(rng: random.Random) -> dict[int, list[str]]:
    """Which kinds of activity happen on which day index."""
    plan: dict[int, list[str]] = {}
    for i in range(DAYS):
        if i in NO_WATCH or i in MISSING or i in SICK or i == DAYS - 1:
            continue
        wd = (START + timedelta(days=i)).weekday()  # 0 = Monday
        kinds: list[str] = []
        if wd == 1 and rng.random() < 0.55:
            kinds.append("running")
        if wd == 3 and rng.random() < 0.4:
            kinds.append("running" if rng.random() < 0.7 else "indoor_cycling")
        if wd == 5 and rng.random() < 0.5:
            kinds.append("trail_running" if rng.random() < 0.6 else "hiking")
        if wd == 6 and rng.random() < 0.3:
            kinds.append("road_biking")
        if wd == 0 and rng.random() < 0.15:
            kinds.append("strength_training")
        if wd == 2 and rng.random() < 0.12:
            kinds.append("walking")
        if kinds:
            plan[i] = kinds
    return plan


def make_activity(rng: random.Random, n: int, d: date, kind: str, vo2: float) -> dict:
    name, parent = KINDS[kind]
    hour = {"running": 7, "trail_running": 9, "road_biking": 10, "hiking": 9}.get(kind, 18)
    start = datetime(d.year, d.month, d.day, hour, rng.randrange(0, 60), rng.randrange(0, 60))
    distance = ascent = descent = power = cadence = None
    if kind == "running":
        km = rng.uniform(5, 13)
        pace = rng.uniform(300, 350)
        moving = km * pace
        distance, ascent = km * 1000, km * rng.uniform(4, 12)
        avg_hr, cadence = rng.uniform(140, 158), rng.uniform(164, 178)
    elif kind == "trail_running":
        km = rng.uniform(9, 20)
        moving = km * rng.uniform(390, 480)
        distance, ascent = km * 1000, km * rng.uniform(35, 65)
        avg_hr, cadence = rng.uniform(136, 152), rng.uniform(156, 170)
    elif kind == "road_biking":
        km = rng.uniform(30, 70)
        moving = km / rng.uniform(24, 29) * 3600
        distance, ascent = km * 1000, km * rng.uniform(6, 14)
        avg_hr, cadence, power = rng.uniform(124, 142), rng.uniform(80, 90), rng.uniform(170, 215)
    elif kind == "indoor_cycling":
        moving = rng.uniform(40, 60) * 60
        avg_hr, cadence, power = rng.uniform(128, 145), rng.uniform(85, 95), rng.uniform(180, 225)
    elif kind == "hiking":
        km = rng.uniform(8, 16)
        moving = km * rng.uniform(720, 900)
        distance, ascent = km * 1000, km * rng.uniform(50, 80)
        avg_hr = rng.uniform(108, 124)
    elif kind == "walking":
        km = rng.uniform(3, 6)
        moving = km * rng.uniform(660, 760)
        distance, ascent = km * 1000, km * rng.uniform(2, 8)
        avg_hr = rng.uniform(92, 104)
    else:  # strength_training: Garmin reports 0 distance and no ascent
        moving = rng.uniform(35, 55) * 60
        distance = 0.0
        avg_hr = rng.uniform(102, 118)
    if ascent is not None:
        descent = ascent * rng.uniform(0.92, 1.05)
    duration = moving * rng.uniform(1.0, 1.08)
    elapsed = duration * rng.uniform(1.0, 1.04)
    hard = {"running": (2.6, 4.4), "trail_running": (2.8, 4.0), "road_biking": (2.2, 3.6),
            "indoor_cycling": (2.8, 4.3), "hiking": (1.8, 2.9), "walking": (0.8, 1.6),
            "strength_training": (1.0, 2.4)}[kind]
    aerobic = rng.uniform(*hard)
    anaerobic = rng.uniform(0, 2.8 if kind in ("running", "indoor_cycling") else 1.2)
    max_hr = avg_hr + rng.uniform(14, 30)
    # Realistic holes: one run with a heart-rate sensor that dropped out, one walk Garmin did not rate.
    if n == 11:
        avg_hr = max_hr = None
        aerobic = anaerobic = None
    if kind == "walking" and n % 2 == 0:
        aerobic = anaerobic = None
    load = None if aerobic is None else aerobic * duration / 60 * rng.uniform(0.9, 1.3)
    aid = 1000000 + n
    return {
        "id": aid,
        "name": name if not (kind == "running" and start.hour >= 17) else "Evening Run",
        "type": kind,
        "parent_type": parent,
        "event_type": "uncategorized",
        "start_local": start.strftime("%Y-%m-%d %H:%M:%S"),
        "start_utc": (start - timedelta(hours=_utc_offset(d))).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(duration, 1),
        "moving_s": round(moving, 1),
        "elapsed_s": round(elapsed, 1),
        "distance_m": None if distance is None else round(distance, 1),
        "ascent_m": None if ascent is None else round(ascent),
        "descent_m": None if descent is None else round(descent),
        "avg_hr": None if avg_hr is None else round(avg_hr),
        "max_hr": None if max_hr is None else round(max_hr),
        "avg_speed_mps": round(distance / moving, 3) if distance else (0.0 if distance == 0 else None),
        "calories": round(duration / 60 * rng.uniform(7, 12)),
        "aerobic_te": _r1(aerobic),
        "anaerobic_te": _r1(anaerobic),
        "te_label": _te_label(_r1(aerobic), _r1(anaerobic)),
        "training_load": None if load is None else round(load / 2, 1),
        "vo2max": round(vo2) if kind in ("running", "trail_running") else None,
        "avg_power": None if power is None else round(power),
        "avg_cadence": None if cadence is None else round(cadence),
        "device": FAKE_DEVICE,
        "location": None,
    }


def _gmt_text(t: datetime) -> str:
    """Garmin's GMT text form: 2026-03-14T06:42:00.0"""
    return t.strftime("%Y-%m-%dT%H:%M:%S") + ".0"


def _ms(t: datetime) -> int:
    return int((t - datetime(1970, 1, 1)).total_seconds() * 1000)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Night:
    """One night's made-up sleep: a hypnogram (stages in cycles of about 90 minutes), its totals and
    the series Garmin reports during it. Times are GMT; `off` is the local clock's offset."""

    def __init__(self, rng: random.Random, wake_day: date, target_s: int, rest: float, hrv: float, sick: bool,
                 bb_start: float, bb_end: float):
        off = timedelta(hours=_utc_offset(wake_day))
        weekend = wake_day.weekday() >= 5
        bed_local = datetime(wake_day.year, wake_day.month, wake_day.day) - timedelta(
            minutes=rng.gauss(55 if not weekend else 20, 22))  # around 23:00, later before weekend mornings
        self.off = off
        self.start = bed_local - off
        segs: list[tuple[int, int]] = []  # (stage 0 deep / 1 light / 2 REM / 3 awake, minutes)
        asleep = 0
        cycle = 0
        while asleep < target_s // 60:
            early = cycle < 2
            plan = [(1, rng.randint(12, 28)), (0, rng.randint(25, 45) if early else rng.randint(0, 14)),
                    (1, rng.randint(8, 20)), (2, rng.randint(6, 14) if early else rng.randint(18, 34))]
            for stage, m in plan:
                if m:
                    segs.append((stage, m))
                    asleep += m
            if rng.random() < (0.55 if sick else 0.35):
                segs.append((3, rng.randint(2, 7)))
            cycle += 1
        self.segments: list[tuple[datetime, datetime, int]] = []
        t = self.start
        for stage, m in segs:
            self.segments.append((t, t + timedelta(minutes=m), stage))
            t += timedelta(minutes=m)
        self.end = t
        self.totals = {k: 60 * sum(m for st, m in segs if st == k) for k in range(4)}
        self.sleep_s = self.totals[0] + self.totals[1] + self.totals[2]
        self.awake_count = sum(1 for st, _ in segs if st == 3)
        self.rest, self.hrv, self.rng = rest, hrv, rng
        self.bb_start, self.bb_end = bb_start, bb_end

    def stage_at(self, t: datetime) -> int:
        for a, b, st in self.segments:
            if a <= t < b:
                return st
        return 3

    def every(self, minutes: int):
        t = self.start
        while t < self.end:
            yield t
            t += timedelta(minutes=minutes)

    def heart(self, t: datetime) -> int:
        st = self.stage_at(t)
        return round(self.rest + {0: 1.0, 1: 3.5, 2: 6.0, 3: 11.0}[st] + self.rng.gauss(0, 1.3))

    def body_battery(self, t: datetime) -> int:
        f = (t - self.start) / (self.end - self.start)
        return round(self.bb_start + (self.bb_end - self.bb_start) * (1 - (1 - f) ** 1.6))


def make_day(rng: random.Random, i: int, d: date, acts: list[dict], vo2: float, prev_load: float,
             state: dict) -> tuple[dict, dict[str, object]]:
    """day.json and the processed/<source>.json files of one day, in Garmin's shapes."""
    fetched = datetime(d.year, d.month, d.day, 6, 20) + timedelta(days=1 if d < END else 0, hours=0 if d < END else 7)
    base = {"date": d.isoformat(), "fetched_at": fetched.isoformat(timespec="seconds") + "+01:00",
            "complete": (END - d).days >= 2}
    off = timedelta(hours=_utc_offset(d))
    midnight = datetime(d.year, d.month, d.day) - off  # local midnight, in GMT
    stamps = {"startTimestampGMT": _gmt_text(midnight), "endTimestampGMT": _gmt_text(midnight + timedelta(days=1)),
              "startTimestampLocal": _gmt_text(midnight + off), "endTimestampLocal": _gmt_text(midnight + off + timedelta(days=1))}
    if i in NO_WATCH:
        # What Garmin returns for a day the watch was not worn: zeros, a resting calorie estimate,
        # empty series and records full of nulls.
        headline = dict.fromkeys(DAY_HEADLINE_KEYS)
        headline.update(steps=0, distance_m=0, calories=1810, active_calories=0, floors_up=0, intensity_minutes=0)
        processed = {
            "daily_summary": {"calendarDate": d.isoformat(), "totalSteps": 0, "totalDistanceMeters": 0,
                              "totalKilocalories": 1810.0, "activeKilocalories": 0.0, "bmrKilocalories": 1810.0,
                              "dailyStepGoal": state["goal"], "floorsAscended": 0.0, "floorsDescended": 0.0,
                              "moderateIntensityMinutes": 0, "vigorousIntensityMinutes": 0, "restingHeartRate": None,
                              "minHeartRate": None, "maxHeartRate": None, "averageStressLevel": -1, "maxStressLevel": -1,
                              "includesWellnessData": False},
            "heart_rate": {**stamps, "heartRateValues": None, "restingHeartRate": None},
            "stress": {**stamps, "stressValuesArray": [], "bodyBatteryValuesArray": []},
            "sleep": {"dailySleepDTO": {"calendarDate": d.isoformat(), "sleepTimeSeconds": None, "sleepScores": None}},
            "hrv": {}, "body_battery": [], "steps": [], "training_readiness": [], "spo2": {"averageSpO2": None},
        }
        return {**base, "has_data": False, "headline": headline}, processed

    trend = i / DAYS
    sick = i in SICK
    rest = 54.5 - 3.0 * trend + rng.gauss(0, 1.1) + (5.5 if sick else 0) + (1.5 if prev_load > 150 else 0)
    hrv = 57 + 6 * trend + rng.gauss(0, 5) - (14 if sick else 0) - (4 if prev_load > 150 else 0)
    weekend = d.weekday() >= 5
    sleep_h = rng.gauss(7.6 if weekend else 7.0, 0.55) - (0.6 if sick else 0)
    target_s = int(max(4.5, min(9.5, sleep_h)) * 3600 // 60 * 60)
    sleep_ok = rng.random() > 0.06 and i != 25
    hrv_ok = sleep_ok and rng.random() > 0.05
    hrv_v = round(hrv)
    status = "BALANCED" if hrv_v >= 50 else ("UNBALANCED" if hrv_v >= 44 else "LOW")
    bb_wake = round(_clip(rng.gauss(78 if not sick else 52, 9), 35, 100))
    night = Night(rng, d, target_s, rest, hrv, sick, state["bb_evening"], bb_wake) if sleep_ok else None
    sleep_s = night.sleep_s if night else None
    score = (round(_clip(52 + (sleep_s / 3600 - 6) * 13 + rng.gauss(0, 6) - (12 if sick else 0), 35, 96))
             if night else None)
    readiness = (round(_clip((score or 60) * 0.55 + (hrv - 45) * 0.9 - prev_load / 8 + rng.gauss(0, 7), 5, 98))
                 if sleep_ok else None)

    act_steps = sum(int((a["distance_m"] or 0) * 1.25) for a in acts if a["type"] in ("running", "trail_running", "hiking", "walking"))
    steps = int(max(1500, rng.gauss(7800 if not sick else 3200, 2300))) + act_steps
    in_progress = d == END
    now = midnight + timedelta(hours=13, minutes=20) if in_progress else midnight + timedelta(days=1)
    if in_progress:
        steps = 4318  # the day is still in progress
    move_km = steps * 0.00078
    active = round(steps * 0.04 + sum(a["calories"] for a in acts))
    vig = sum(int(a["duration_s"] // 60) for a in acts if (a["aerobic_te"] or 0) >= 3.0)
    mod = rng.randrange(5, 40) + sum(int(a["duration_s"] // 60) for a in acts if (a["aerobic_te"] or 0) < 3.0)
    floors = rng.randrange(2, 18) + sum(int((a["ascent_m"] or 0) // 3) for a in acts)
    stress_target = _clip(rng.gauss(29 if not sick else 38, 6), 12, 60)

    # Activities as (start, end) in GMT, for the series.
    spans = []
    for a in acts:
        st = datetime.fromisoformat(a["start_local"]) - off
        spans.append((st, st + timedelta(seconds=a["duration_s"]), a))

    def in_activity(t: datetime):
        return next((a for s0, s1, a in spans if s0 <= t < s1), None)

    wake = night.end if night else midnight + timedelta(hours=7)
    bed = midnight + timedelta(hours=23, minutes=rng.randint(-40, 30))  # tonight's bedtime (next night's sleep)

    # Heart rate every 2 minutes.
    hr_values, t, cur = [], midnight, rest + 18
    while t < now:
        a = in_activity(t)
        if night and night.start <= t < night.end:
            v = night.heart(t)
        elif t < wake or t >= bed:
            v = round(rest + 4 + rng.gauss(0, 1.5))
        elif a:
            v = round((a["avg_hr"] or 135) + rng.gauss(0, 6))
        else:
            cur = _clip(cur + rng.gauss(0, 3) + (rest + 20 - cur) * 0.08, rest + 4, rest + 60)
            v = round(cur)
        hr_values.append([_ms(t), v if rng.random() > 0.01 else None])
        t += timedelta(minutes=2)
    # Stress and Body Battery every 3 minutes (stress.json carries both).
    stress_rows, bb_rows, t, level, bb = [], [], midnight, stress_target, float(state["bb_evening"])
    drain_total = charge_total = 0.0
    while t < now:
        a = in_activity(t)
        asleep = (night and night.start <= t < night.end) or t < wake or t >= bed
        if asleep:
            sv = round(_clip(rng.gauss(12, 5), 1, 25))
            if night and night.start <= t < night.end:
                nb = night.body_battery(t)
            else:
                nb = min(100.0, bb + 0.25)
        elif a:
            sv = -2  # moving: Garmin does not measure stress during activity
            nb = bb - 0.55 - (a["aerobic_te"] or 2) * 0.12
        else:
            level = _clip(level + rng.gauss(0, 6) + (stress_target - level) * 0.15, 5, 95)
            sv = round(level) if rng.random() > 0.03 else -1  # -1: off the wrist for a moment
            nb = bb - (0.08 + level / 240)
        nb = _clip(nb, 5, 100)
        if nb > bb:
            charge_total += nb - bb
        else:
            drain_total += bb - nb
        bb = nb
        stress_rows.append([_ms(t), sv])
        bb_rows.append([_ms(t), "MEASURED", round(bb), 3.0])
        t += timedelta(minutes=3)
    bb_levels = [r[2] for r in bb_rows]
    state["bb_evening"] = round(bb)
    stress_ok = [v for _, v in stress_rows if v >= 0]
    stress_avg = round(sum(stress_ok) / len(stress_ok)) if stress_ok else None
    stress_max = max(stress_ok) if stress_ok else None
    band = {k: 0 for k in ("rest", "low", "medium", "high")}
    for v in stress_ok:
        band["rest" if v <= 25 else "low" if v <= 50 else "medium" if v <= 75 else "high"] += 180
    # Steps per 15 minutes, while awake.
    buckets = []
    t = midnight
    weights = []
    while t < midnight + timedelta(days=1):
        awake = wake <= t < bed
        a = in_activity(t)
        weights.append((t, (8 if a and a["type"] in ("running", "trail_running", "hiking", "walking") else 1) * (1 if awake else 0)
                        * rng.uniform(0.2, 1.8)))
        t += timedelta(minutes=15)
    total_w = sum(w for _, w in weights) or 1
    for t, w in weights:
        if t >= now:
            break
        buckets.append({"startGMT": _gmt_text(t), "endGMT": _gmt_text(t + timedelta(minutes=15)),
                        "steps": int(steps * w / total_w), "pushes": 0,
                        "primaryActivityLevel": "active" if w > 3 else "sedentary" if w else "sleeping",
                        "activityLevelConstant": False})
    # Respiration every 2 minutes, blood oxygen hourly during the night (when the watch measured it).
    resp_rows, t = [], midnight
    while t < now:
        asleep = (night and night.start <= t < night.end) or t < wake or t >= bed
        a = in_activity(t)
        v = (rng.gauss(13.8, 0.6) if asleep else rng.gauss(22, 3) if a else rng.gauss(15.5, 1.2))
        resp_rows.append([_ms(t), round(v) if rng.random() > 0.04 else -1])
        t += timedelta(minutes=2)
    resp_vals = [v for _, v in resp_rows if v >= 0]
    pulse_ox = night is not None and rng.random() < 0.7
    spo2_hours = []
    if pulse_ox:
        t = night.start.replace(minute=0, second=0) + timedelta(hours=1)
        while t < night.end:
            spo2_hours.append([_ms(t), round(_clip(rng.gauss(95.5, 1.2), 89, 99))])
            t += timedelta(hours=1)

    # The week's intensity minutes (Monday to Sunday), with this day.
    if d.weekday() == 0:
        state["week_mod"] = state["week_vig"] = 0
    state["week_mod"] += mod
    state["week_vig"] += vig
    # Training load: acute (7-day) and chronic (28-day) exponential averages.
    load_today = sum(a["training_load"] or 0 for a in acts)
    state["acute"] = state["acute"] + (load_today * 7 - state["acute"]) / 7
    state["chronic"] = state["chronic"] + (load_today * 7 - state["chronic"]) / 28
    state["rhr"].append(round(rest))
    state["hrv"].append(hrv_v if hrv_ok else None)
    rhr7 = round(sum(state["rhr"][-7:]) / len(state["rhr"][-7:]))
    week_hrv = [x for x in state["hrv"][-7:] if x is not None]

    headline = {
        "steps": steps,
        "distance_m": round(move_km * 1000),
        "calories": 1810 + active,
        "active_calories": active,
        "floors_up": floors,
        "resting_hr": round(rest),
        "min_hr": round(rest) - rng.randrange(2, 7),
        "max_hr": max([round(rest) + rng.randrange(55, 85)] + [a["max_hr"] or 0 for a in acts]),
        "stress_avg": stress_avg,
        # Garmin's daily summary leaves Body Battery out: the index takes it from the samples.
        "body_battery_high": None,
        "body_battery_low": None,
        "intensity_minutes": mod + 2 * vig,
        "sleep_seconds": sleep_s,
        "sleep_score": score,
        "hrv_last_night_avg": hrv_v if hrv_ok else None,
        "hrv_status": status if hrv_ok else None,
        "training_readiness": readiness,
        "vo2max_running": None,
        "training_status": None,
    }
    if in_progress:
        headline.update(intensity_minutes=mod, stress_avg=None)
    ds = d.isoformat()
    runs_today = any(a["type"] in ("running", "trail_running") for a in acts)
    hrv_points = []
    if night and hrv_ok:
        for t in night.every(5):
            hrv_points.append({"value": float(round(_clip(hrv + rng.gauss(0, 9) + (6 if night.stage_at(t) == 0 else 0), 18, 140))),
                               "startGMT": _ms(t)})
    ratio = state["acute"] / state["chronic"] if state["chronic"] > 1 else None
    status_key = ("DETRAINING" if sick else "PRODUCTIVE" if i > 70 and ratio and ratio > 0.9 else
                  "RECOVERY" if ratio and ratio < 0.7 else "MAINTAINING")
    dev = str(FAKE_DEVICE)
    balance = {dev: {"calendarDate": ds, "monthlyLoadAerobicLow": round(state["chronic"] * 2.1),
                     "monthlyLoadAerobicHigh": round(state["chronic"] * 1.3), "monthlyLoadAnaerobic": round(state["chronic"] * 0.35),
                     "monthlyLoadAerobicLowTargetMin": 210, "monthlyLoadAerobicLowTargetMax": 520,
                     "monthlyLoadAerobicHighTargetMin": 260, "monthlyLoadAerobicHighTargetMax": 560,
                     "monthlyLoadAnaerobicTargetMin": 70, "monthlyLoadAnaerobicTargetMax": 240,
                     "trainingBalanceFeedbackPhrase": "AEROBIC_HIGH_SHORTAGE" if state["chronic"] < 160 else "BALANCED",
                     "primaryTrainingDevice": True}} if i >= 14 else None
    latest_status = {dev: {"calendarDate": ds, "trainingStatus": {"PRODUCTIVE": 7, "MAINTAINING": 4, "RECOVERY": 5,
                                                                  "DETRAINING": 2}[status_key],
                           "trainingStatusFeedbackPhrase": f"{status_key}_1", "primaryTrainingDevice": True,
                           "acuteTrainingLoadDTO": {"acwrPercent": round((ratio or 0) * 50), "acwrStatus":
                                                    "OPTIMAL" if ratio and 0.8 <= ratio <= 1.4 else "LOW" if ratio else None,
                                                    "dailyTrainingLoadAcute": round(state["acute"]),
                                                    "dailyTrainingLoadChronic": round(state["chronic"]),
                                                    "minTrainingLoadChronic": round(state["chronic"] * 0.8),
                                                    "maxTrainingLoadChronic": round(state["chronic"] * 1.5),
                                                    "dailyAcuteChronicWorkloadRatio": round(ratio, 1) if ratio else None}}
                     } if i >= 7 else None
    headline["training_status"] = latest_status
    if runs_today:
        headline["vo2max_running"] = round(vo2, 1)

    def factor(pct: float, good: str, ok: str, bad: str) -> tuple[int, str]:
        pct = round(_clip(pct, 2, 100))
        return pct, good if pct >= 70 else ok if pct >= 40 else bad

    ready_doc = None
    if readiness is not None:
        sl = factor((score or 60) * 1.05, "GOOD_SLEEP", "MODERATE_SLEEP", "POOR_SLEEP")
        rec = factor(100 - prev_load / 3, "GOOD_RECOVERY", "MODERATE_RECOVERY", "POOR_RECOVERY")
        acwr = factor(100 - max(0, (ratio or 1) - 1) * 120, "GOOD_LOAD", "MODERATE_LOAD", "HIGH_LOAD")
        sth = factor(100 - stress_target * 1.2, "GOOD_STRESS_HISTORY", "MODERATE_STRESS_HISTORY", "HIGH_STRESS_HISTORY")
        hv = factor(50 + (hrv - 50) * 3, "GOOD_HRV", "MODERATE_HRV", "POOR_HRV")
        slh = factor(55 + (sum(state["sleep"][-3:]) / max(1, len(state["sleep"][-3:])) / 3600 - 6.5) * 30,
                     "GOOD_SLEEP_HISTORY", "MODERATE_SLEEP_HISTORY", "POOR_SLEEP_HISTORY")
        level_name = ("PRIME" if readiness >= 95 else "HIGH" if readiness >= 75 else "MODERATE" if readiness >= 50
                      else "LOW" if readiness >= 25 else "POOR")
        ready_doc = {"userProfilePK": 1, "calendarDate": ds, "timestamp": _gmt_text(wake + timedelta(minutes=4)),
                     "timestampLocal": _gmt_text(wake + off + timedelta(minutes=4)), "deviceId": FAKE_DEVICE,
                     "level": level_name, "feedbackLong": f"{level_name}_READY_1", "feedbackShort": f"READY_{level_name}",
                     "score": readiness, "sleepScore": score, "sleepScoreFactorPercent": sl[0],
                     "sleepScoreFactorFeedback": sl[1], "recoveryTime": round(max(0, prev_load * 6 + rng.gauss(0, 60))),
                     "recoveryTimeFactorPercent": rec[0], "recoveryTimeFactorFeedback": rec[1],
                     "acwrFactorPercent": acwr[0], "acwrFactorFeedback": acwr[1], "acuteLoad": round(state["acute"]),
                     "stressHistoryFactorPercent": sth[0], "stressHistoryFactorFeedback": sth[1],
                     "hrvFactorPercent": hv[0], "hrvFactorFeedback": hv[1],
                     "hrvWeeklyAverage": round(sum(week_hrv) / len(week_hrv)) if week_hrv else None,
                     "sleepHistoryFactorPercent": slh[0], "sleepHistoryFactorFeedback": slh[1],
                     "validSleep": True, "inputContext": "AFTER_WAKEUP_RESET", "primaryActivityTracker": True,
                     "recoveryTimeChangePhrase": None}
    if night:
        state["sleep"].append(night.sleep_s)
    readiness_list = [ready_doc] if ready_doc else []
    if ready_doc and acts:
        after = dict(ready_doc, score=max(1, readiness - rng.randint(15, 35)), inputContext="AFTER_POST_EXERCISE_RESET",
                     timestamp=_gmt_text(spans[-1][1] + timedelta(minutes=8)))
        readiness_list.append(after)

    def qual(pct: float, lo: float, hi: float) -> str:
        return "EXCELLENT" if lo <= pct <= hi else "GOOD" if lo - 5 <= pct <= hi + 5 else "FAIR" if lo - 10 <= pct <= hi + 10 else "POOR"

    sleep_doc: dict = {"dailySleepDTO": {"calendarDate": ds, "sleepTimeSeconds": None, "sleepScores": None}}
    if night:
        tot = night.totals
        pct = {k: round(100 * tot[k] / night.sleep_s) for k in range(3)}
        overall = "EXCELLENT" if score >= 90 else "GOOD" if score >= 80 else "FAIR" if score >= 60 else "POOR"
        need = round(_clip(480 + rng.gauss(0, 15) + (30 if prev_load > 150 else 0) + (25 if sick else 0), 420, 600))
        nap = 1500 if i in NAPS else 0
        sleep_doc = {
            "dailySleepDTO": {
                "id": 1, "userProfilePK": 1, "calendarDate": ds, "sleepTimeSeconds": night.sleep_s,
                "napTimeSeconds": nap, "sleepWindowConfirmed": True, "sleepWindowConfirmationType": "ENHANCED_CONFIRMED_FINAL",
                "sleepStartTimestampGMT": _ms(night.start), "sleepEndTimestampGMT": _ms(night.end),
                "sleepStartTimestampLocal": _ms(night.start + night.off), "sleepEndTimestampLocal": _ms(night.end + night.off),
                "unmeasurableSleepSeconds": 0, "deepSleepSeconds": tot[0], "lightSleepSeconds": tot[1],
                "remSleepSeconds": tot[2], "awakeSleepSeconds": tot[3], "deviceRemCapable": True, "retro": False,
                "sleepFromDevice": True,
                "averageRespirationValue": round(rng.gauss(13.8, 0.4), 1), "lowestRespirationValue": 11.0,
                "highestRespirationValue": round(rng.gauss(18, 1)), "awakeCount": night.awake_count,
                "avgSleepStress": round(rng.gauss(14 if not sick else 22, 3), 1), "ageGroup": "ADULT",
                "avgHeartRate": round(rest + 3.2 + rng.gauss(0, 0.6), 1),
                "averageSpO2Value": round(sum(v for _, v in spo2_hours) / len(spo2_hours), 1) if spo2_hours else None,
                "lowestSpO2Value": min(v for _, v in spo2_hours) if spo2_hours else None,
                "sleepScoreFeedback": f"POSITIVE_{overall}" if score >= 60 else "NEGATIVE_SHORT_SLEEP",
                "sleepScoreInsight": "POSITIVE_RESTFUL_DAY" if not sick else "NEGATIVE_STRESSFUL_DAY",
                "sleepScorePersonalizedInsight": "NONE",
                "sleepScores": {
                    "totalDuration": {"qualifierKey": qual(night.sleep_s / 3600, 7, 9), "optimalStart": 28800.0, "optimalEnd": 28800.0},
                    "stress": {"qualifierKey": "EXCELLENT" if not sick else "FAIR", "optimalStart": 0.0, "optimalEnd": 15.0},
                    "awakeCount": {"qualifierKey": "GOOD" if night.awake_count <= 2 else "FAIR", "optimalStart": 0.0, "optimalEnd": 1.0},
                    "overall": {"value": score, "qualifierKey": overall},
                    "remPercentage": {"value": pct[2], "qualifierKey": qual(pct[2], 21, 31), "optimalStart": 21.0, "optimalEnd": 31.0,
                                      "idealStartInSeconds": night.sleep_s * 0.21, "idealEndInSeconds": night.sleep_s * 0.31},
                    "restlessness": {"qualifierKey": "GOOD", "optimalStart": 0.0, "optimalEnd": 5.0},
                    "lightPercentage": {"value": pct[1], "qualifierKey": qual(pct[1], 30, 64), "optimalStart": 30.0, "optimalEnd": 64.0,
                                        "idealStartInSeconds": night.sleep_s * 0.30, "idealEndInSeconds": night.sleep_s * 0.64},
                    "deepPercentage": {"value": pct[0], "qualifierKey": qual(pct[0], 16, 33), "optimalStart": 16.0, "optimalEnd": 33.0,
                                       "idealStartInSeconds": night.sleep_s * 0.16, "idealEndInSeconds": night.sleep_s * 0.33},
                },
                "sleepVersion": 2,
                "sleepNeed": {"calendarDate": ds, "deviceId": FAKE_DEVICE, "baseline": 480, "actual": need,
                              "feedback": "INCREASED" if need > 495 else "NO_CHANGE_NO_ADJUSTMENTS",
                              "trainingFeedback": "NO_CHANGE", "sleepHistoryAdjustment": "NO_CHANGE",
                              "hrvAdjustment": "NO_CHANGE", "napAdjustment": "NO_CHANGE", "displayedForTheDay": True,
                              "recommendedBedtimeStartMins": 1350, "recommendedBedtimeEndMins": 1410,
                              "preferredActivityTracker": True},
            },
            "sleepMovement": [],
            "remSleepData": True,
            "sleepLevels": [{"startGMT": _gmt_text(a), "endGMT": _gmt_text(b), "activityLevel": float(st)}
                            for a, b, st in night.segments],
            "sleepRestlessMoments": [{"value": 1, "startGMT": _ms(t)} for t in night.every(17) if rng.random() < 0.5],
            "restlessMomentsCount": 0,
            "wellnessEpochRespirationDataDTOList": [{"startTimeGMT": _ms(t), "respirationValue": round(rng.gauss(13.8, 0.7), 1)}
                                                    for t in night.every(2)],
            "sleepHeartRate": [{"value": night.heart(t), "startGMT": _ms(t)} for t in night.every(2)],
            "sleepStress": [{"value": round(_clip(rng.gauss(14, 6), 0, 40)), "startGMT": _ms(t)} for t in night.every(3)],
            "sleepBodyBattery": [{"value": night.body_battery(t), "startGMT": _ms(t)} for t in night.every(3)],
            "skinTempDataExists": i >= 20,
            "hrvData": hrv_points,
            "breathingDisruptionData": [],
            "avgOvernightHrv": float(hrv_v) if hrv_ok else None,
            "hrvStatus": status if hrv_ok else None,
            "bodyBatteryChange": bb_wake - (night.bb_start),
            "skinTempCalibrationDays": 0 if i >= 20 else 20 - i,
            "restingHeartRate": round(rest),
        }
        sleep_doc["restlessMomentsCount"] = len(sleep_doc["sleepRestlessMoments"])
        if i >= 20:
            sleep_doc["avgSkinTempDeviationC"] = round(rng.gauss(0.0, 0.25) + (0.9 if sick else 0), 1)

    events = []
    if night:
        events.append({"eventType": "SLEEP", "eventStartTimeGmt": _gmt_text(night.start), "timezoneOffset": int(off.total_seconds() * 1000),
                       "durationInMilliseconds": int((night.end - night.start).total_seconds() * 1000),
                       "bodyBatteryImpact": bb_wake - night.bb_start, "feedbackType": "NONE",
                       "shortFeedback": "SLEEP_RESTFUL" if score >= 75 else "SLEEP_RESTLESS"})
    for s0, s1, a in spans:
        events.append({"eventType": "ACTIVITY", "eventStartTimeGmt": _gmt_text(s0), "timezoneOffset": int(off.total_seconds() * 1000),
                       "durationInMilliseconds": int((s1 - s0).total_seconds() * 1000),
                       "bodyBatteryImpact": -round((a["aerobic_te"] or 2) * 4 + (s1 - s0).total_seconds() / 900),
                       "feedbackType": "EXERCISE_TRAINING", "shortFeedback": "INTENSIVE_EXERCISE" if (a["aerobic_te"] or 0) >= 3.5 else "EXERCISE"})
    if i in NAPS:
        n0 = midnight + timedelta(hours=14, minutes=30)
        events.append({"eventType": "NAP", "eventStartTimeGmt": _gmt_text(n0), "timezoneOffset": int(off.total_seconds() * 1000),
                       "durationInMilliseconds": 1500000, "bodyBatteryImpact": 6, "feedbackType": "NONE", "shortFeedback": "NAP"})

    week_total = state["week_mod"] + 2 * state["week_vig"]
    weight = None
    if d.weekday() in (0, 3, 5) and i % 9 != 4:
        weight = round(74.6 - 1.6 * trend + rng.gauss(0, 0.35), 1)
    vo2_now = round(vo2, 1)
    race5k = 1260 * (49.0 / vo2) ** 1.05
    processed: dict[str, object] = {
        "daily_summary": {
            "userProfileId": 1, "calendarDate": ds, "totalKilocalories": float(1810 + active),
            "activeKilocalories": float(active), "bmrKilocalories": 1810.0, "wellnessKilocalories": float(1810 + active),
            "totalSteps": steps, "totalDistanceMeters": round(move_km * 1000), "dailyStepGoal": state["goal"],
            "wellnessStartTimeGmt": _gmt_text(midnight), "wellnessStartTimeLocal": _gmt_text(midnight + off),
            "wellnessEndTimeGmt": _gmt_text(now), "wellnessEndTimeLocal": _gmt_text(now + off),
            "highlyActiveSeconds": int(sum((s1 - s0 for s0, s1, _ in spans), timedelta()).total_seconds()) + rng.randint(600, 2400),
            "activeSeconds": rng.randint(5000, 12000), "sedentarySeconds": rng.randint(30000, 42000),
            "sleepingSeconds": (night.sleep_s + night.totals[3]) if night else 0,
            "includesWellnessData": True, "includesActivityData": bool(acts),
            "moderateIntensityMinutes": mod, "vigorousIntensityMinutes": vig,
            "floorsAscendedInMeters": floors * 3.048, "floorsDescendedInMeters": floors * 2.9,
            "floorsAscended": float(floors), "floorsDescended": float(max(0, floors - rng.randint(0, 3))),
            "intensityMinutesGoal": 150, "userFloorsAscendedGoal": 10,
            "minHeartRate": headline["min_hr"], "maxHeartRate": headline["max_hr"], "restingHeartRate": headline["resting_hr"],
            "lastSevenDaysAvgRestingHeartRate": rhr7, "source": "GARMIN",
            "averageStressLevel": stress_avg if stress_avg is not None else -1, "maxStressLevel": stress_max if stress_max is not None else -1,
            "stressDuration": sum(band.values()), "restStressDuration": band["rest"],
            "activityStressDuration": len([1 for _, v in stress_rows if v == -2]) * 180,
            "uncategorizedStressDuration": len([1 for _, v in stress_rows if v == -1]) * 180,
            "totalStressDuration": len(stress_rows) * 180, "lowStressDuration": band["low"],
            # Garmin leaves these out on some days; the index then counts them from the samples.
            "mediumStressDuration": band["medium"] if i % 5 else None, "highStressDuration": band["high"] if i % 5 else None,
        },
        "heart_rate": {**stamps, "userProfilePK": 1, "calendarDate": ds, "maxHeartRate": headline["max_hr"],
                       "minHeartRate": headline["min_hr"], "restingHeartRate": headline["resting_hr"],
                       "lastSevenDaysAvgRestingHeartRate": rhr7,
                       "heartRateValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "heartrate"}],
                       "heartRateValues": hr_values},
        "stress": {**stamps, "userProfilePK": 1, "calendarDate": ds, "maxStressLevel": stress_max if stress_max is not None else -1,
                   "avgStressLevel": stress_avg if stress_avg is not None else -1, "stressChartValueOffset": 1,
                   "stressChartYAxisOrigin": -1,
                   "stressValueDescriptorsDTOList": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "stressLevel"}],
                   "stressValuesArray": stress_rows,
                   "bodyBatteryValueDescriptorsDTOList": [
                       {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                       {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryStatus"},
                       {"bodyBatteryValueDescriptorIndex": 2, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
                       {"bodyBatteryValueDescriptorIndex": 3, "bodyBatteryValueDescriptorKey": "bodyBatteryVersion"}],
                   "bodyBatteryValuesArray": bb_rows},
        "body_battery": [{"date": ds, "charged": round(charge_total), "drained": round(drain_total), **stamps,
                          "bodyBatteryValuesArray": [[r[0], r[2]] for r in bb_rows[::40]],
                          "bodyBatteryValueDescriptorDTOList": [
                              {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                              {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"}],
                          "bodyBatteryActivityEvent": [e for e in events if e["eventType"] == "ACTIVITY"],
                          "endOfDayBodyBatteryDynamicFeedbackEvent": {
                              "eventTimestampGmt": _gmt_text(now), "bodyBatteryLevel": "MODERATE",
                              "feedbackShortType": "DAY_RECOVERING_AND_INACTIVE" if not acts else "DAY_STRESSFUL_AND_ACTIVE",
                              "feedbackLongType": "NONE"}}],
        "body_battery_events": [{"event": e, "activityName": None, "activityType": None, "activityId": None,
                                 "averageStress": 18.0} for e in events],
        "steps": buckets,
        "respiration": {**stamps, "userProfilePK": 1, "calendarDate": ds,
                        "lowestRespirationValue": float(min(resp_vals)) if resp_vals else None,
                        "highestRespirationValue": float(max(resp_vals)) if resp_vals else None,
                        "avgWakingRespirationValue": round(rng.gauss(15.4, 0.5), 1),
                        "avgSleepRespirationValue": None, "avgTomorrowSleepRespirationValue": round(rng.gauss(13.8, 0.4), 1),
                        "respirationValueDescriptorsDTOList": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "respiration"}],
                        "respirationValuesArray": resp_rows, "respirationVersion": 200},
        "spo2": ({"userProfilePK": 1, "calendarDate": ds, "averageSpO2": round(sum(v for _, v in spo2_hours) / len(spo2_hours)),
                  "lowestSpO2": min(v for _, v in spo2_hours), "lastSevenDaysAvgSpO2": 95.0,
                  "latestSpO2": spo2_hours[-1][1], "avgSleepSpO2": round(sum(v for _, v in spo2_hours) / len(spo2_hours)),
                  "spO2HourlyAverages": spo2_hours, "continuousReadingDTOList": None}
                 if spo2_hours else {"userProfilePK": 1, "calendarDate": ds, "averageSpO2": None, "lowestSpO2": None,
                                     "spO2HourlyAverages": None, "continuousReadingDTOList": None}),
        "intensity_minutes": {"userProfilePK": 1, "calendarDate": ds, "weeklyModerate": state["week_mod"],
                              "weeklyVigorous": state["week_vig"], "weeklyTotal": week_total, "weekGoal": 150,
                              "startDayMinutes": week_total - mod - 2 * vig, "endDayMinutes": week_total,
                              "moderateMinutes": None, "vigorousMinutes": None},
        "all_day_events": [],
        "sleep": sleep_doc,
        "naps": [],
        "hrv": ({"userProfilePk": 1, "hrvSummary": {
                    "calendarDate": ds, "weeklyAvg": round(sum(week_hrv) / len(week_hrv)) if len(week_hrv) >= 3 else None,
                    "lastNightAvg": hrv_v, "lastNight5MinHigh": hrv_v + rng.randint(12, 30),
                    "baseline": {"lowUpper": 46, "balancedLow": 50, "balancedUpper": 66, "markerValue": 0.4} if i >= 19 else None,
                    "status": status, "feedbackPhrase": f"HRV_{status}_1", "createTimeStamp": _gmt_text(wake)},
                 "hrvReadings": [{"hrvValue": int(p["value"]), "readingTimeGMT": _gmt_text(datetime(1970, 1, 1) + timedelta(milliseconds=p["startGMT"])),
                                  "readingTimeLocal": _gmt_text(datetime(1970, 1, 1) + timedelta(milliseconds=p["startGMT"]) + off)}
                                 for p in hrv_points]}
                if hrv_ok else {}),
        "resting_hr": {"userProfileId": 1, "statisticsStartDate": ds, "statisticsEndDate": ds,
                       "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": float(round(rest)), "calendarDate": ds}]}}},
        "training_readiness": readiness_list,
        "morning_training_readiness": ready_doc or {},
        "training_status": {"userId": 1,
                            "mostRecentVO2Max": {"generic": {"calendarDate": ds, "vo2MaxPreciseValue": vo2_now, "vo2MaxValue": round(vo2)}}
                            if runs_today else None,
                            "mostRecentTrainingLoadBalance": {"userId": 1, "metricsTrainingLoadBalanceDTOMap": balance} if balance else None,
                            "mostRecentTrainingStatus": {"userId": 1, "latestTrainingStatusData": latest_status} if latest_status else None,
                            "heatAltitudeAcclimationDTO": None},
        "training_load_balance": {"userId": 1, "metricsTrainingLoadBalanceDTOMap": balance, "recordedDevices": None},
        "max_metrics": ([{"userId": 1, "generic": {"calendarDate": ds, "vo2MaxPreciseValue": vo2_now, "vo2MaxValue": round(vo2),
                                                   "fitnessAge": None}, "cycling": None}] if runs_today else []),
        "endurance_score": {"userProfilePK": 1, "startDate": ds, "endDate": ds, "enduranceScoreDTO": {
            "calendarDate": ds, "overallScore": round(4900 + 900 * trend + 3 * state["chronic"]) if i >= 10 else None,
            "classification": 3, "gaugeLowerLimit": 3570, "classificationLowerLimitIntermediate": 5100,
            "classificationLowerLimitTrained": 5800, "classificationLowerLimitWellTrained": 6500,
            "classificationLowerLimitExpert": 7200, "classificationLowerLimitSuperior": 7900,
            "classificationLowerLimitElite": 8600, "gaugeUpperLimit": 10500, "contributors": []}},
        "hill_score": {"userProfilePK": 1, "startDate": ds, "endDate": ds, "hillScoreDTOList": [
            {"calendarDate": ds, "strengthScore": round(48 + 10 * trend) if i >= 10 else None,
             "enduranceScore": round(55 + 8 * trend) if i >= 10 else None,
             "overallScore": round(52 + 9 * trend) if i >= 10 else None, "hillScoreClassificationId": 3,
             "primaryTrainingDevice": True}]},
        "race_predictions": ([{"userId": 1, "calendarDate": ds, "time5K": round(race5k), "time10K": round(race5k * 2.09),
                               "timeHalfMarathon": round(race5k * 4.65), "timeMarathon": round(race5k * 9.8)}]
                             if i >= 30 else []),
        "running_tolerance": [],
        "stamina_curve": {},
        "fitness_age": {"chronologicalAge": 41, "fitnessAge": round(37.8 - 2.2 * trend + rng.gauss(0, 0.15), 1),
                        "achievableFitnessAge": 33.5, "previousFitnessAge": 38.0, "lastUpdated": _gmt_text(wake)},
        "heat_altitude_acclimation": {"calendarDate": ds, "heatAcclimationPercentage": round(_clip(10 + 30 * trend + rng.gauss(0, 3), 0, 100)),
                                      "altitudeAcclimation": 0, "currentAltitude": 120},
        "weigh_ins": {"startDate": ds, "endDate": ds, "dateWeightList": ([
            {"samplePk": 1, "date": _ms(wake), "calendarDate": ds, "weight": weight * 1000, "bmi": round(weight / 1.79 ** 2, 1),
             "bodyFat": round(18.5 - 1.2 * trend + rng.gauss(0, 0.3), 1) if i % 2 else None, "bodyWater": None,
             "boneMass": None, "muscleMass": None, "sourceType": "MANUAL", "timestampGMT": _ms(wake + timedelta(minutes=20)),
             "weightDelta": None}] if weight else []), "totalAverage": {"weight": weight * 1000 if weight else None}},
        "blood_pressure": {"startDate": ds, "endDate": ds, "bloodPressureMeasurements": []},
        "hydration": {"userId": 1, "calendarDate": ds, "valueInML": float(rng.randrange(1400, 2900, 50)) if i % 3 == 0 else None,
                      "goalInML": 2500.0, "sweatLossInML": float(sum(round(a["duration_s"] / 3600 * 600) for a in acts)) or None},
        "health_snapshots": [],
        "lifestyle_logging": {"calendarDate": ds, "dailyLogsReport": []},
    }
    # Odd files a real archive has: an empty sleep record, a stress file of the wrong shape, a
    # day whose HRV summary is null, one file that is not JSON at all.
    if i == 41:
        processed["sleep"] = {}
    if i == 52:  # a day without Body Battery or stress at all
        processed["stress"] = []
        processed["body_battery"] = []
    if i == 66:
        processed["hrv"] = {"hrvSummary": None, "hrvReadings": None}
    if i == 88:
        processed["training_status"] = "not json {"
    return {**base, "has_data": True, "headline": headline}, processed


def write_archive(root: Path) -> None:
    rng = random.Random(SEED)
    plan = plan_activities(rng)
    days_dir = root / "garmin" / "days"
    n = 0
    vo2 = 48.2
    prev_load = 0.0
    state = {"bb_evening": 30, "goal": 8000, "week_mod": 0, "week_vig": 0, "acute": 140.0, "chronic": 140.0,
             "rhr": [], "hrv": [], "sleep": [], "week_steps": []}
    for i in range(DAYS):
        d = START + timedelta(days=i)
        if i in MISSING:
            continue
        ddir = days_dir / d.strftime("%Y") / d.isoformat()
        acts = []
        for kind in plan.get(i, []):
            n += 1
            if kind in ("running", "trail_running"):
                vo2 = min(53.0, vo2 + rng.uniform(-0.1, 0.25))
            acts.append(make_activity(rng, n, d, kind, vo2))
        day, processed = make_day(random.Random(SEED * 1000 + i), i, d, acts, vo2, prev_load, state)
        prev_load = sum((a["training_load"] or 0) for a in acts)
        state["week_steps"].append(day["headline"]["steps"] or 0)
        if d.weekday() == 6:  # Garmin moves the step goal with what you walked that week
            walked = [x for x in state["week_steps"] if x]
            if walked:
                state["goal"] = int(round(state["goal"] * 0.7 + sum(walked) / len(walked) * 0.3, -2))
            state["week_steps"] = []
        names = []
        for a in acts:
            hhmm = a["start_local"][11:16].replace(":", "")
            adir = ddir / "activities" / f"{hhmm}-{a['type']}-{a['id']}"
            names.append(adir.name)
            fetched = datetime.fromisoformat(a["start_local"]) + timedelta(minutes=50)
            manifest = {**a, "date": d.isoformat(), "fetched_at": fetched.isoformat(timespec="seconds") + "+01:00",
                        "complete": True, "sources": {"summary": {"status": "ok"}},
                        "derived": {"records": int(a["elapsed_s"]), "laps": 1}, "layout_version": 1}
            _write(adir / "activity.json", manifest)
            _write(adir / "raw" / f"{a['id']}_ACTIVITY.fit", "")
        status = {name: {"status": "ok" if data not in (None, [], {}) else "empty"} for name, data in processed.items()}
        day.update(activities=names, sources=status, layout_version=1)
        _write(ddir / "day.json", day)
        for name, data in processed.items():
            _write(ddir / "processed" / f"{name}.json", data if not isinstance(data, str) else data)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1))


def _index(root: Path) -> None:
    """Run fitdata's own indexer on the fake archive, in a separate process so its settings
    (read once, at import) point only at the temporary folder."""
    env = {**os.environ, "FITDATA_DATA_DIR": str(root), "FITDATA_REMOTE": "",
           "FITDATA_CONFIG": str(root / "no-config.toml"), "FITDATA_INTRADAY_DAYS": str(INTRADAY_DAYS)}
    subprocess.run([sys.executable, "-I", "-c", "from fitdata_garmin import index; index.rebuild()"], env=env, check=True)


def build(out: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="fitdata-fixture-") as tmp:
        root = Path(tmp) / "data"
        write_archive(root)
        _index(root)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out.with_name(out.name + ".tmp")
        shutil.copyfile(root / "fitdata.sqlite", tmp_out)
        tmp_out.replace(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the synthetic sample index used by the dashboards' tests.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"where to write it (default: {DEFAULT_OUT.name})")
    args = ap.parse_args(argv)
    try:
        path = build(args.out)
    except (ImportError, subprocess.CalledProcessError) as e:
        print(f"Could not make the sample data, because fitdata's indexer did not run ({e}). "
              "Run this from the fitdata repository folder with: uv run python ui/fixtures/make_fixture.py",
              file=sys.stderr)
        return 1
    print(f"Wrote synthetic sample data ({DAYS} days from {START} to {END}, made-up numbers) to {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
