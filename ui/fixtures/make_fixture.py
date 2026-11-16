"""Make the synthetic sample index the dashboards are tested and previewed against.

Every number here is made up by a seeded random generator. Nothing comes from a real watch
or a real account.

The script writes a small fake archive (day.json and activity.json files, as `fitdata sync`
would) into a temporary folder and runs fitdata's own indexer on it. So the sample has exactly
the tables and columns a real `fitdata.sqlite` has (days, activities, files), with the same
untyped columns and the same JSON text in columns such as training_status.

It covers 120 days (2026-01-05 to 2026-05-04) and about 30 activities, with the gaps a real
archive has: a few days without the watch, one day missing from the archive, nights without
sleep or HRV data, a sick week without training, an activity without heart rate, a strength
session without distance, and a last day that is still in progress.

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


def make_day(rng: random.Random, i: int, d: date, acts: list[dict], vo2: float, prev_load: float) -> dict:
    fetched = datetime(d.year, d.month, d.day, 6, 20) + timedelta(days=1 if d < END else 0, hours=0 if d < END else 7)
    base = {"date": d.isoformat(), "fetched_at": fetched.isoformat(timespec="seconds") + "+01:00",
            "complete": (END - d).days >= 2}
    if i in NO_WATCH:
        # What Garmin returns for a day the watch was not worn: zeros, a resting calorie estimate, no readings.
        headline = dict.fromkeys(DAY_HEADLINE_KEYS)
        headline.update(steps=0, distance_m=0, calories=1810, active_calories=0, floors_up=0, intensity_minutes=0)
        return {**base, "has_data": False, "headline": headline}

    trend = i / DAYS
    sick = i in SICK
    rest = 54.5 - 3.0 * trend + rng.gauss(0, 1.1) + (5.5 if sick else 0) + (1.5 if prev_load > 150 else 0)
    hrv = 57 + 6 * trend + rng.gauss(0, 5) - (14 if sick else 0) - (4 if prev_load > 150 else 0)
    weekend = d.weekday() >= 5
    sleep_h = rng.gauss(7.6 if weekend else 7.0, 0.55) - (0.6 if sick else 0)
    sleep_s = int(max(4.5, min(9.5, sleep_h)) * 3600 // 60 * 60)
    score = round(max(35, min(96, 52 + (sleep_s / 3600 - 6) * 13 + rng.gauss(0, 6) - (12 if sick else 0))))
    bb_high = round(max(30, min(100, 30 + score * 0.72 + rng.gauss(0, 5))))
    bb_low = round(max(5, min(bb_high - 20, rng.uniform(5, 30))))
    readiness = round(max(5, min(98, score * 0.55 + (hrv - 45) * 0.9 - prev_load / 8 + rng.gauss(0, 7))))
    hrv_v = round(hrv)
    status = "BALANCED" if hrv_v >= 50 else ("UNBALANCED" if hrv_v >= 44 else "LOW")

    sleep_ok = rng.random() > 0.06 and i != 25
    hrv_ok = sleep_ok and rng.random() > 0.05
    act_steps = sum(int((a["distance_m"] or 0) * 1.25) for a in acts if a["type"] in ("running", "trail_running", "hiking", "walking"))
    steps = int(max(1500, rng.gauss(7800 if not sick else 3200, 2300))) + act_steps
    if d == END:
        steps = 4318  # the day is still in progress
    move_km = steps * 0.00078
    active = round(steps * 0.04 + sum(a["calories"] for a in acts))
    vig = sum(int(a["duration_s"] // 60) for a in acts if (a["aerobic_te"] or 0) >= 3.0)
    mod = rng.randrange(5, 40) + sum(int(a["duration_s"] // 60) for a in acts if (a["aerobic_te"] or 0) < 3.0)

    runs_today = any(a["type"] in ("running", "trail_running") for a in acts)
    headline = {
        "steps": steps,
        "distance_m": round(move_km * 1000),
        "calories": 1810 + active,
        "active_calories": active,
        "floors_up": rng.randrange(2, 18) + sum(int((a["ascent_m"] or 0) // 3) for a in acts),
        "resting_hr": round(rest),
        "min_hr": round(rest) - rng.randrange(2, 7),
        "max_hr": max([round(rest) + rng.randrange(55, 85)] + [a["max_hr"] or 0 for a in acts]),
        "stress_avg": round(max(12, min(60, rng.gauss(29 if not sick else 38, 6)))),
        "body_battery_high": bb_high if rng.random() > 0.03 else None,
        "body_battery_low": bb_low,
        "intensity_minutes": mod + 2 * vig,
        "sleep_seconds": sleep_s if sleep_ok else None,
        "sleep_score": score if sleep_ok else None,
        "hrv_last_night_avg": hrv_v if hrv_ok else None,
        "hrv_status": status if hrv_ok else None,
        "training_readiness": readiness if sleep_ok else None,
        "vo2max_running": round(vo2, 1) if runs_today else None,
        "training_status": ({str(FAKE_DEVICE): {"trainingStatus": 7 if i > 70 else 4,
                                                "trainingStatusFeedbackPhrase": "PRODUCTIVE_1" if i > 70 else "MAINTAINING_1",
                                                "sinceDate": d.isoformat()}}
                            if runs_today else None),
    }
    if headline["body_battery_high"] is None:
        headline["body_battery_low"] = None
    if d == END:
        headline.update(body_battery_low=None, intensity_minutes=mod, stress_avg=None)
    return {**base, "has_data": True, "headline": headline}


def write_archive(root: Path) -> None:
    rng = random.Random(SEED)
    plan = plan_activities(rng)
    days_dir = root / "garmin" / "days"
    n = 0
    vo2 = 48.2
    prev_load = 0.0
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
        day = make_day(rng, i, d, acts, vo2, prev_load)
        prev_load = sum((a["training_load"] or 0) for a in acts)
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
        day.update(activities=names, sources={"sleep": {"status": "ok" if day["headline"]["sleep_seconds"] else "empty"}},
                   layout_version=1)
        _write(ddir / "day.json", day)
        if day["headline"]["sleep_seconds"]:
            _write(ddir / "processed" / "sleep.json", {"synthetic": True})


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1))


def _index(root: Path) -> None:
    """Run fitdata's own indexer on the fake archive, in a separate process so its settings
    (read once, at import) point only at the temporary folder."""
    env = {**os.environ, "FITDATA_DATA_DIR": str(root), "FITDATA_REMOTE": "",
           "FITDATA_CONFIG": str(root / "no-config.toml")}
    subprocess.run([sys.executable, "-I", "-c", "from fitdata import index; index.rebuild()"], env=env, check=True)


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
              "Run this from the fitdata-garmin folder with: uv run python ui/fixtures/make_fixture.py",
              file=sys.stderr)
        return 1
    print(f"Wrote synthetic sample data ({DAYS} days from {START} to {END}, made-up numbers) to {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
