"""How much disk the bucket backend saves, measured on a synthetic archive.

Everything is made up by a seeded random generator: no real watch, account or person. The JSON
files have the shapes of Garmin Connect's answers (intraday arrays of [timestamp, value], the
sleep payload with its per-minute series, activity details with one metrics row per second and a
GPS polyline), written the way fitdata writes them (indent=1). The FIT files are real FIT
(decodable by Garmin's SDK) with synthetic samples, and each activity's derived/records.csv is
made by fitdata's own decoder from that FIT file.

The archive is written through fitdata's storage interface as plain files, then moved into a
bucket with the real `migrate-storage` code (copy, verify every object, switch). Disk use is
counted from st_blocks (what the disk really holds), after the bucket is closed.

    uv run --extra bucket python bench/storage_bench.py [--days 300] [--keep DIR]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import struct
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FIT_EPOCH = 631065600
SEED = 20261116
_CRC = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


# ------------------------------------------------------------------ FIT writing (synthetic)

def _crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        for nib in (byte & 0xF, byte >> 4):
            tmp = _CRC[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC[nib]
    return crc


def _fit(messages: list[tuple[int, list[tuple[int, str, int]], list[list[int]]]], file_type: int, t0: int) -> bytes:
    """messages: (global number, [(field number, struct format, base type)], rows)."""
    body = bytearray()
    defs = [(0, [(0, "B", 0x00), (4, "I", 0x86)], [[file_type, t0]]), *messages]
    for local, (glob, fields, rows) in enumerate(defs):
        body += bytes([0x40 | local, 0, 0]) + struct.pack("<H", glob) + bytes([len(fields)])
        for num, fmt, base in fields:
            body += bytes([num, struct.calcsize("<" + fmt), base])
        packer = struct.Struct("<" + "".join(f for _, f, _ in fields))
        for row in rows:
            body += bytes([local]) + packer.pack(*row)
    header = bytearray([14, 0x20]) + struct.pack("<HI", 2132, len(body)) + b".FIT"
    header += struct.pack("<H", _crc(bytes(header)))
    out = bytes(header) + bytes(body)
    return out + struct.pack("<H", _crc(out))


def wellness_fits(rng: random.Random, d: date) -> dict[str, bytes]:
    t0 = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) - FIT_EPOCH
    hr, steps, rows = 62, 0, []
    for i in range(0, 86400, 15):  # monitoring: heart rate every 15 s, steps and intensity
        hr = max(42, min(160, hr + rng.choice((-2, -1, 0, 0, 1, 2))))
        steps += rng.choice((0, 0, 0, 3, 8, 15)) if 25200 < i < 79200 else 0
        rows.append([t0 + i, hr, steps, rng.choice((0, 0, 1, 2)), 6])
    mon = _fit([(55, [(253, "I", 0x86), (27, "B", 0x02), (3, "I", 0x86), (5, "B", 0x00), (28, "B", 0x02)], rows)], 32, t0)
    rr, beats, t = [], [], 0.0
    for _ in range(26000):  # overnight beat-to-beat intervals (HRV)
        v = int(1000 + 120 * math.sin(t / 30) + rng.gauss(0, 35))
        beats.append(v)
        t += v / 1000
        if len(beats) == 5:
            rr.append(beats)
            beats = []
    hrv = _fit([(78, [(0, "HHHHH", 0x84)], rr)], 44, t0)
    stress = _fit([(227, [(0, "h", 0x83), (1, "I", 0x86)], [[max(1, min(99, int(30 + 20 * math.sin(i / 40) + rng.gauss(0, 8)))), t0 + i * 180] for i in range(480)])], 49, t0)
    stamp = d.strftime("%Y%m%d")
    return {f"{stamp}_WELLNESS.fit": mon, f"{stamp}_HRV.fit": hrv, f"{stamp}_METRICS.fit": stress}


def activity_fit(rng: random.Random, start: datetime, seconds: int) -> tuple[bytes, list[dict]]:
    t0 = int(start.timestamp()) - FIT_EPOCH
    lat, lon, alt, dist, hr = 41.40 + rng.random() / 10, 2.15 + rng.random() / 10, 120.0, 0.0, 95
    rows, poly = [], []
    for i in range(seconds):
        speed = max(0.5, 2.9 + 0.4 * math.sin(i / 300) + rng.gauss(0, 0.15))
        dist += speed
        lat += speed * math.cos(i / 900) / 111_000
        lon += speed * math.sin(i / 900) / 85_000
        alt += math.sin(i / 200) * 0.3 + rng.gauss(0, 0.05)
        hr = max(90, min(185, hr + rng.choice((-1, 0, 0, 1, 1))))
        rows.append([t0 + i, int(lat / 180 * 2**31), int(lon / 180 * 2**31), int((alt + 500) * 5), hr,
                     rng.choice((84, 85, 86, 87)), int(dist * 100), int(speed * 1000), int(200 + rng.gauss(0, 25))])
        poly.append({"lat": round(lat, 7), "lon": round(lon, 7), "altitude": round(alt, 1),
                     "time": (int(start.timestamp()) + i) * 1000, "speed": round(speed, 3), "distanceFromPreviousPoint": round(speed, 2),
                     "distanceInMeters": round(dist, 1), "valid": True, "timerStart": i == 0, "timerStop": False,
                     "cumulativeAscent": None, "cumulativeDescent": None, "extendedCoordinate": False})
    fields = [(253, "I", 0x86), (0, "i", 0x85), (1, "i", 0x85), (2, "H", 0x84), (3, "B", 0x02), (4, "B", 0x02),
              (5, "I", 0x86), (6, "H", 0x84), (7, "H", 0x84)]
    lap = [(19, [(253, "I", 0x86), (2, "I", 0x86), (7, "I", 0x86), (9, "I", 0x86)],
            [[t0 + k * 1000 + 999, t0 + k * 1000, 1000 * 1000, int(dist / max(1, seconds // 1000))] for k in range(seconds // 1000)])]
    return _fit([(20, fields, rows), *lap], 4, t0), poly


# ------------------------------------------------------------------ Garmin-shaped JSON (synthetic)

def _series(rng, t0_ms, step_s, n, base, spread, lo, hi):
    v, out = base, []
    for i in range(n):
        v = max(lo, min(hi, v + rng.gauss(0, spread)))
        out.append([t0_ms + i * step_s * 1000, round(v)])
    return out


def daily_sources(rng: random.Random, d: date) -> dict[str, object]:
    ds = d.isoformat()
    t0 = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) * 1000
    steps = rng.randint(3000, 16000)
    rhr = rng.randint(46, 56)
    gmt = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0")  # noqa: E731
    summary = {"userProfileId": 10000001, "totalKilocalories": 2100 + rng.randint(0, 900), "activeKilocalories": rng.randint(200, 1100),
               "bmrKilocalories": 1720.0, "totalSteps": steps, "totalDistanceMeters": steps * 0.78, "dailyStepGoal": 9000,
               "calendarDate": ds, "restingHeartRate": rhr, "minHeartRate": rhr - 4, "maxHeartRate": rng.randint(120, 182),
               "averageStressLevel": rng.randint(18, 45), "maxStressLevel": rng.randint(70, 99), "stressDuration": 30000,
               "bodyBatteryHighestValue": rng.randint(60, 100), "bodyBatteryLowestValue": rng.randint(5, 30),
               "moderateIntensityMinutes": rng.randint(0, 60), "vigorousIntensityMinutes": rng.randint(0, 50),
               "floorsAscended": rng.randint(0, 25) + 0.0, "floorsDescended": rng.randint(0, 25) + 0.0,
               **{f"metric{k}InSeconds": rng.randint(0, 40000) for k in ("highlyActive", "active", "sedentary", "sleeping")},
               **{f"field{k:02d}": None for k in range(40)}}
    hr = {"userProfilePK": 10000001, "calendarDate": ds, "startTimestampGMT": gmt(t0), "endTimestampGMT": gmt(t0 + 86399000),
          "maxHeartRate": summary["maxHeartRate"], "minHeartRate": rhr - 4, "restingHeartRate": rhr,
          "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
          "heartRateValues": _series(rng, t0, 120, 720, 70, 4, 40, 175)}
    stress_vals = _series(rng, t0, 180, 480, 30, 6, -2, 99)
    bb = [[t, "MEASURED", max(5, min(100, 80 - i // 6 + rng.randint(-2, 2))), 2.0] for i, (t, _) in enumerate(stress_vals)]
    stress = {"userProfilePK": 10000001, "calendarDate": ds, "maxStressLevel": 90, "avgStressLevel": 32,
              "stressValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}, {"key": "stressLevel", "index": 1}],
              "stressValuesArray": stress_vals,
              "bodyBatteryValueDescriptorsDTOList": [{"bodyBatteryValueDescriptorIndex": i, "bodyBatteryValueDescriptorKey": k}
                                                    for i, k in enumerate(("timestamp", "bodyBatteryStatus", "bodyBatteryLevel", "bodyBatteryVersion"))],
              "bodyBatteryValuesArray": bb}
    body_battery = [{"date": ds, "charged": rng.randint(30, 80), "drained": rng.randint(30, 80), "startTimestampGMT": gmt(t0),
                     "bodyBatteryValuesArray": [[t, lvl] for t, _, lvl, _ in bb], "bodyBatteryDynamicFeedbackEvent": {"feedbackLongType": "SLEEP_PREPARATION"}}]
    step_list = [{"startGMT": gmt(t0 + i * 900000), "endGMT": gmt(t0 + (i + 1) * 900000), "steps": rng.choice((0, 0, 40, 120, 600, 1300)),
                  "pushes": 0, "primaryActivityLevel": rng.choice(("sedentary", "active", "sleeping", "highlyActive")),
                  "activityLevelConstant": rng.random() < 0.6} for i in range(96)]
    respiration = {"userProfilePK": 10000001, "calendarDate": ds, "lowestRespirationValue": 9.0, "highestRespirationValue": 22.0,
                   "avgWakingRespirationValue": 14.0, "avgSleepRespirationValue": 12.0,
                   "respirationValuesArray": [[t, v / 5] for t, v in _series(rng, t0, 120, 720, 70, 3, 40, 110)]}
    sleep_start = t0 - 3600_000 * rng.randint(1, 3)
    secs = rng.randint(5 * 3600, 9 * 3600)
    levels = []
    t = sleep_start
    while t < sleep_start + secs * 1000:
        n = rng.randint(5, 50) * 60000
        levels.append({"startGMT": gmt(t), "endGMT": gmt(t + n), "activityLevel": rng.choice((0.0, 1.0, 2.0, 3.0))})
        t += n
    sleep = {"dailySleepDTO": {"id": t0, "userProfilePK": 10000001, "calendarDate": ds, "sleepTimeSeconds": secs,
                               "napTimeSeconds": 0, "sleepWindowConfirmed": True, "sleepStartTimestampGMT": sleep_start,
                               "sleepEndTimestampGMT": sleep_start + secs * 1000, "deepSleepSeconds": secs // 5, "lightSleepSeconds": secs // 2,
                               "remSleepSeconds": secs // 5, "awakeSleepSeconds": 900, "averageSpO2Value": 95.0, "averageRespirationValue": 12.0,
                               "avgSleepStress": 14.0, "sleepScores": {k: {"value": rng.randint(50, 98), "qualifierKey": rng.choice(("GOOD", "FAIR", "EXCELLENT"))}
                                                                       for k in ("overall", "totalDuration", "stress", "awakeCount", "remPercentage",
                                                                                 "restlessness", "lightPercentage", "deepPercentage")}},
             "sleepMovement": [{"startGMT": gmt(sleep_start + i * 60000), "endGMT": gmt(sleep_start + (i + 1) * 60000),
                                "activityLevel": round(abs(rng.gauss(0.4, 0.5)), 6)} for i in range(secs // 60)],
             "remSleepData": True, "sleepLevels": levels,
             "sleepRestlessMoments": [{"value": 1, "startGMT": sleep_start + rng.randint(0, secs) * 1000} for _ in range(rng.randint(10, 40))],
             "wellnessEpochRespirationDataDTOList": [{"startTimeGMT": sleep_start + i * 120000, "respirationValue": round(rng.uniform(9, 16), 1)}
                                                     for i in range(secs // 120)],
             "sleepHeartRate": [{"value": rng.randint(44, 62), "startGMT": sleep_start + i * 120000} for i in range(secs // 120)],
             "sleepStress": [{"value": rng.randint(5, 30), "startGMT": sleep_start + i * 180000} for i in range(secs // 180)],
             "sleepBodyBattery": [{"value": min(100, 20 + i // 4), "startGMT": sleep_start + i * 180000} for i in range(secs // 180)],
             "hrvData": [{"value": round(rng.uniform(25, 80), 1), "startGMT": sleep_start + i * 300000} for i in range(secs // 300)],
             "avgOvernightHrv": round(rng.uniform(35, 60), 1), "hrvStatus": "BALANCED", "bodyBatteryChange": rng.randint(20, 70),
             "restingHeartRate": rhr}
    hrv = {"userProfilePk": 10000001, "hrvSummary": {"calendarDate": ds, "weeklyAvg": 47, "lastNightAvg": rng.randint(35, 60),
                                                    "lastNight5MinHigh": 80, "baseline": {"lowUpper": 38, "balancedLow": 40, "balancedUpper": 58,
                                                                                          "markerValue": 0.4}, "status": "BALANCED",
                                                    "feedbackPhrase": "HRV_BALANCED_2", "createTimeStamp": gmt(t0)},
           "hrvReadings": [{"hrvValue": rng.randint(25, 90), "readingTimeGMT": gmt(sleep_start + i * 300000),
                            "readingTimeLocal": gmt(sleep_start + i * 300000 + 3600000)} for i in range(secs // 300)],
           "startTimestampGMT": gmt(sleep_start), "endTimestampGMT": gmt(sleep_start + secs * 1000)}
    small = lambda n: {f"value{k}": rng.choice((None, rng.randint(0, 999), round(rng.random() * 100, 2), "OK")) for k in range(n)}  # noqa: E731
    return {
        "daily_summary": summary, "heart_rate": hr, "stress": stress, "body_battery": body_battery,
        "body_battery_events": [{"event": {"eventType": "SLEEP", "eventStartTimeGmt": gmt(sleep_start), "durationInMilliseconds": secs * 1000,
                                           "bodyBatteryImpact": 55, "feedbackType": "NONE"}}],
        "steps": step_list,
        "floors": {"startTimestampGMT": gmt(t0), "floorsValueDescriptorDTOList": [{"key": k, "index": i} for i, k in enumerate(("startTimeGMT", "endTimeGMT", "floorsAscended", "floorsDescended"))],
                   "floorValuesArray": [[gmt(t0 + i * 900000), gmt(t0 + (i + 1) * 900000), rng.choice((0, 0, 1, 3)), rng.choice((0, 0, 1, 2))] for i in range(96)]},
        "respiration": respiration,
        "spo2": {"calendarDate": ds, "averageSpO2": 95.0, "lowestSpO2": 89, "latestSpO2": 96,
                 "spO2HourlyAverages": [[t0 + h * 3600000, rng.randint(90, 99)] for h in range(24)],
                 "continuousReadingDTOList": [{"epochTimestamp": gmt(sleep_start + i * 60000), "spo2Reading": rng.randint(89, 99),
                                               "readingConfidence": rng.randint(1, 5)} for i in range(secs // 60)]},
        "intensity_minutes": {"calendarDate": ds, "weeklyModerate": 120, "weeklyVigorous": 60,
                              "imValuesArray": [[t0 + i * 900000, rng.choice((0, 0, 0, 5, 15))] for i in range(96)]},
        "all_day_events": [], "sleep": sleep, "naps": [], "hrv": hrv,
        "resting_hr": {"userProfileId": 10000001, "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": float(rhr), "calendarDate": ds}]}}},
        "training_readiness": [{"calendarDate": ds, "score": rng.randint(20, 95), "level": "MODERATE", **small(25)}],
        "morning_training_readiness": {"calendarDate": ds, **small(25)},
        "training_status": {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": round(rng.uniform(48, 53), 1), "calendarDate": ds}},
                            "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"1000000001": small(18)}},
                            "mostRecentTrainingStatus": {"latestTrainingStatusData": {"1000000001": small(22)}}},
        "training_load_balance": small(20), "max_metrics": [{"generic": {"vo2MaxPreciseValue": 51.3, "calendarDate": ds}, "cycling": None}],
        "endurance_score": {"avg": 6400, **small(15)}, "hill_score": small(12), "race_predictions": [small(8)],
        "running_tolerance": small(10), "stamina_curve": small(10), "fitness_age": small(10),
        "heat_altitude_acclimation": small(10), "weigh_ins": {"dateWeightList": [], "totalAverage": small(8)},
        "blood_pressure": {"measurementSummaries": []}, "hydration": {"calendarDate": ds, "goalInML": 2500.0, "valueInML": None},
        "health_snapshots": [], "lifestyle_logging": [],
    }


def activity_sources(rng: random.Random, aid: int, start: datetime, seconds: int, poly: list[dict]) -> dict[str, object]:
    descriptors = ["directTimestamp", "sumDistance", "directSpeed", "directHeartRate", "directElevation", "directRunCadence",
                   "directPower", "directLatitude", "directLongitude", "directVerticalOscillation", "directGroundContactTime",
                   "directStrideLength", "directVerticalRatio", "directBodyBattery", "directPerformanceCondition"]
    metrics = [{"metrics": [p["time"], p["distanceInMeters"], p["speed"], rng.randint(120, 175), p["altitude"], rng.randint(160, 180),
                            rng.randint(180, 320), p["lat"], p["lon"], round(rng.uniform(7, 10), 2), rng.randint(220, 280),
                            round(rng.uniform(0.9, 1.3), 2), round(rng.uniform(6, 9), 2), None, rng.choice((-1, 0, 1, None))]}
               for p in poly]
    dto = {"startTimeLocal": start.strftime("%Y-%m-%dT%H:%M:%S.0"), "startTimeGMT": start.strftime("%Y-%m-%dT%H:%M:%S.0"),
           "distance": poly[-1]["distanceInMeters"], "duration": float(seconds), "movingDuration": float(seconds - 60),
           "elapsedDuration": float(seconds + 30), "elevationGain": 210.0, "elevationLoss": 205.0, "averageHR": 148.0,
           "maxHR": 176.0, "averageSpeed": 2.9, "calories": 640.0, "trainingEffect": 3.4, "anaerobicTrainingEffect": 1.2,
           "trainingEffectLabel": "TEMPO", "activityTrainingLoad": 120.0, "averageRunCadence": 172.0, "averagePower": 250.0}
    split = lambda k: {"distance": 1000.0, "duration": 340.0 + k, "averageHR": 150.0, "averageSpeed": 2.94, "elevationGain": 12.0,  # noqa: E731
                       "startTimeGMT": start.strftime("%Y-%m-%dT%H:%M:%S.0"), "lapIndex": k, "intensityType": "ACTIVE"}
    return {
        "summary": {"activityId": aid, "activityName": "Made-up run", "activityTypeDTO": {"typeKey": "running", "parentTypeId": 17},
                    "eventTypeDTO": {"typeKey": "uncategorized"}, "summaryDTO": dto, "locationName": "Nowhere",
                    "metadataDTO": {"deviceMetaDataDTO": {"deviceId": 1000000001}}},
        "details": {"activityId": aid, "measurementCount": len(descriptors), "metricsCount": len(metrics),
                    "metricDescriptors": [{"metricsIndex": i, "key": k, "unit": {"id": i, "key": "x", "factor": 1.0}} for i, k in enumerate(descriptors)],
                    "activityDetailMetrics": metrics, "geoPolylineDTO": {"startPoint": poly[0], "endPoint": poly[-1], "polyline": poly}},
        "splits": {"activityId": aid, "lapDTOs": [split(k) for k in range(max(1, seconds // 340))]},
        "split_summaries": {"splitSummaries": [split(0), split(1)]},
        "typed_splits": {"splits": [split(k) for k in range(4)]},
        "hr_zones": [{"zoneNumber": z, "secsInZone": rng.uniform(0, 900), "zoneLowBoundary": 100 + z * 15} for z in range(1, 6)],
        "power_zones": [{"zoneNumber": z, "secsInZone": rng.uniform(0, 900), "zoneLowBoundary": 150 + z * 40} for z in range(1, 6)],
        "power_curve": {"entries": [{"duration": s, "power": 400 - s // 30} for s in (1, 5, 10, 30, 60, 300, 600, 1200, 1800)]},
        "weather": {"temp": 14, "apparentTemp": 13, "relativeHumidity": 70, "windSpeed": 9, "weatherTypeDTO": {"desc": "Cloudy"}},
        "gear": [{"gearPk": 1, "displayName": "Shoes A", "gearTypeName": "Shoes"}],
        "exercise_sets": {"activityId": aid, "exerciseSets": None},
        "personal_records": [],
    }


# ------------------------------------------------------------------ the measurement

def disk(path: Path) -> tuple[int, int, int]:
    """(bytes on disk from st_blocks, apparent bytes, files) under path."""
    blocks = apparent = n = 0
    for root, _, files in os.walk(path):
        for f in files:
            st = os.lstat(os.path.join(root, f))
            blocks += st.st_blocks * 512
            apparent += st.st_size
            n += 1
    return blocks, apparent, n


def mib(n: float) -> str:
    return f"{n / 2**20:,.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--keep", type=Path, help="build the archive here and keep it (default: a temp folder, removed)")
    args = ap.parse_args(argv)

    work = args.keep or Path(tempfile.mkdtemp(prefix="fitdata-bench-"))
    root = work / "data"
    os.environ.update(FITDATA_DATA_DIR=str(root), FITDATA_REMOTE="", FITDATA_CONFIG=str(work / "config.toml"))
    os.environ.pop("FITDATA_STORAGE", None)
    (work / "config.toml").write_text("")
    from fitdata import fit, index, migrate, paths, query, storage

    rng = random.Random(SEED)
    a = storage.archive(write=True)
    first = date(2025, 1, 6)
    t = time.monotonic()
    n_act = 0
    for i in range(args.days):
        d = first + timedelta(days=i)
        dkey = paths.day_key(d)
        for name, data in daily_sources(rng, d).items():
            a.write_json(f"{dkey}/processed/{name}.json", data)
        for name, data in wellness_fits(rng, d).items():
            a.write(f"{dkey}/raw/wellness/{name}", data)
        acts = []
        if rng.random() < 0.6:  # about four activities a week
            n_act += 1
            aid = 20_000_000_000 + n_act
            start = datetime(d.year, d.month, d.day, 7, rng.randint(0, 59), tzinfo=timezone.utc)
            seconds = rng.randint(30, 75) * 60
            blob, poly = activity_fit(rng, start, seconds)
            akey = paths.activity_key(d.isoformat(), start.isoformat(), "running", aid)
            for name, data in activity_sources(rng, aid, start, seconds, poly).items():
                a.write_json(f"{akey}/processed/{name}.json", data)
            a.write(f"{akey}/raw/{aid}_ACTIVITY.fit", blob)
            derived = fit.derive_activity(blob, a, f"{akey}/derived")
            a.write_json(f"{akey}/activity.json", {"id": aid, "date": d.isoformat(), "type": "running", "complete": True,
                                                   "derived": derived, "layout_version": 1})
            acts.append(akey.rsplit("/", 1)[1])
        a.write_json(f"{dkey}/day.json", {"date": d.isoformat(), "complete": True, "has_data": True, "activities": acts,
                                          "headline": {"steps": rng.randint(3000, 16000)}, "layout_version": 1})
        if i % 50 == 49:
            print(f"  made {i + 1} days", file=sys.stderr)
    build_s = time.monotonic() - t
    t = time.monotonic()
    index.rebuild()
    index_files_s = time.monotonic() - t
    storage.close_all()
    files_disk, apparent, n_files = disk(root / "garmin")

    t = time.monotonic()
    result = migrate.migrate("bucket", progress=False)
    migrate_s = time.monotonic() - t
    storage.close_all()
    from fitdata import config
    config.load.cache_clear()
    bucket_disk, _, bucket_files = disk(config.load().store_dir)

    t = time.monotonic()
    index.rebuild()
    index_bucket_s = time.monotonic() - t
    aid = 20_000_000_000 + n_act // 2
    t = time.monotonic()
    for _ in range(20):
        query.activity(str(aid), records=True, every=10)
    activity_ms = (time.monotonic() - t) / 20 * 1000
    t = time.monotonic()
    query.day((first + timedelta(days=args.days // 2)).isoformat(), "sleep")
    day_ms = (time.monotonic() - t) * 1000
    storage.close_all()

    print(f"fitdata storage benchmark (synthetic archive, seed {SEED}, Python {sys.version.split()[0]})")
    print("-" * 78)
    print(f"{'Archive':38} {args.days} days, {n_act} activities, {n_files:,} files ({build_s:.0f} s to make)")
    print(f"{'Logical bytes':38} {mib(apparent)}")
    print(f"{'Plain files on disk (st_blocks)':38} {mib(files_disk)}")
    print(f"{'Bucket on disk (st_blocks)':38} {mib(bucket_disk)}  ({bucket_files} files)")
    print(f"{'Saving':38} {100 * (1 - bucket_disk / files_disk):.0f} % less disk ({files_disk / bucket_disk:.2f}x)")
    print(f"{'migrate-storage --to bucket':38} {migrate_s:.0f} s for {result['objects']:,} objects, all verified")
    print(f"{'fitdata index':38} {index_files_s:.1f} s on files, {index_bucket_s:.1f} s on the bucket")
    print(f"{'activity ID --records (bucket)':38} {activity_ms:.0f} ms; day DATE --source sleep: {day_ms:.0f} ms")
    if not args.keep:
        shutil.rmtree(work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
