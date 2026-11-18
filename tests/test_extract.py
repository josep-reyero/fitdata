"""What the index reads from each day's processed/<source>.json files (apps/fitdata_garmin/extract.py),
and how it copes with files that are missing, empty, of another shape or full of nulls. The
inputs are small made-up records in the shapes Garmin Connect returns; no real data."""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from conftest import DAY
from fitdata_garmin import extract, index, paths

GMT0 = "2026-11-13T23:00:00.0"            # local midnight of DAY, one hour ahead of GMT
LOCAL0 = "2026-11-14T00:00:00.0"
MS0 = 1794610800000                        # 2026-11-13 23:00:00 GMT, in milliseconds
STAMPS = {"startTimestampGMT": GMT0, "startTimestampLocal": LOCAL0}


def _sample() -> dict:
    """One day of every source the index reads, with a few values each."""
    minute = 60_000
    return {
        "daily_summary": {"totalSteps": 9120, "dailyStepGoal": 8000, "totalDistanceMeters": 7100,
                          "totalKilocalories": 2450.0, "activeKilocalories": 640.0, "bmrKilocalories": 1810.0,
                          "floorsAscended": 11.0, "floorsDescended": 9.0, "restingHeartRate": 52,
                          "lastSevenDaysAvgRestingHeartRate": 53, "minHeartRate": 47, "maxHeartRate": 151,
                          "averageStressLevel": 27, "maxStressLevel": 88, "restStressDuration": 30000,
                          "lowStressDuration": 12000, "mediumStressDuration": None, "highStressDuration": None,
                          "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 15, "intensityMinutesGoal": 150,
                          "wellnessStartTimeGmt": GMT0, "wellnessStartTimeLocal": LOCAL0},
        "heart_rate": {**STAMPS, "heartRateValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "heartrate"}],
                       "heartRateValues": [[MS0, 60], [MS0 + 2 * minute, None], [MS0 + 4 * minute, 58], "junk", [None, 70]]},
        "stress": {**STAMPS, "stressValueDescriptorsDTOList": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "stressLevel"}],
                   "stressValuesArray": [[MS0, 20], [MS0 + 3 * minute, -1], [MS0 + 6 * minute, 60], [MS0 + 9 * minute, 80],
                                         [MS0 + 12 * minute, -2]],
                   "bodyBatteryValueDescriptorsDTOList": [
                       {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                       {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryStatus"},
                       {"bodyBatteryValueDescriptorIndex": 2, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
                       {"bodyBatteryValueDescriptorIndex": 3, "bodyBatteryValueDescriptorKey": "bodyBatteryVersion"}],
                   "bodyBatteryValuesArray": [[MS0, "MEASURED", 40, 3.0], [MS0 + 3 * minute, "MEASURED", 85, 3.0],
                                              [MS0 + 6 * minute, "MEASURED", 22, 3.0]]},
        "body_battery": [{"charged": 55, "drained": 61, "bodyBatteryActivityEvent": [
            {"eventType": "ACTIVITY", "eventStartTimeGmt": "2026-11-14T16:00:00.0", "timezoneOffset": 3600000,
             "durationInMilliseconds": 3_600_000, "bodyBatteryImpact": -12, "shortFeedback": "EXERCISE"}],
            "endOfDayBodyBatteryDynamicFeedbackEvent": {"feedbackShortType": "DAY_STRESSFUL_1"}}],
        "body_battery_events": [{"event": {"eventType": "SLEEP", "eventStartTimeGmt": "2026-11-13T22:10:00.0",
                                           "timezoneOffset": 3600000, "durationInMilliseconds": 27_000_000,
                                           "bodyBatteryImpact": 58, "shortFeedback": "SLEEP_RESTFUL"}}],
        "steps": [{"startGMT": GMT0, "endGMT": "2026-11-13T23:15:00.0", "steps": 0},
                  {"startGMT": "2026-11-14T07:00:00.0", "endGMT": "2026-11-14T07:15:00.0", "steps": 420}],
        "respiration": {**STAMPS, "avgWakingRespirationValue": 15.2, "lowestRespirationValue": 11.0,
                        "highestRespirationValue": 21.0, "avgSleepRespirationValue": None,
                        "respirationValuesArray": [[MS0, 14], [MS0 + 2 * minute, -1]]},
        "spo2": {"averageSpO2": 95, "lowestSpO2": 89, "lastSevenDaysAvgSpO2": 95.0, "avgSleepSpO2": 94,
                 "spO2HourlyAverages": [[MS0 + 60 * minute, 96]], "continuousReadingDTOList": None},
        "hrv": {"hrvSummary": {"lastNightAvg": 61, "lastNight5MinHigh": 88, "weeklyAvg": 58, "status": "BALANCED",
                               "feedbackPhrase": "HRV_BALANCED_2",
                               "baseline": {"lowUpper": 46, "balancedLow": 50, "balancedUpper": 66}},
                "hrvReadings": [{"hrvValue": 70, "readingTimeGMT": "2026-11-14T01:00:00.0"}]},
        "intensity_minutes": {"weeklyModerate": 80, "weeklyVigorous": 30, "weeklyTotal": 140, "weekGoal": 150},
        "sleep": {
            "dailySleepDTO": {
                "sleepTimeSeconds": 26400, "napTimeSeconds": 0, "sleepWindowConfirmed": True,
                "sleepStartTimestampGMT": 1794607800000, "sleepStartTimestampLocal": 1794611400000,  # 22:10 GMT, 23:10 local
                "sleepEndTimestampGMT": 1794635400000, "sleepEndTimestampLocal": 1794639000000,
                "deepSleepSeconds": 5400, "lightSleepSeconds": 14400, "remSleepSeconds": 6600, "awakeSleepSeconds": 1200,
                "averageRespirationValue": 13.9, "lowestRespirationValue": 11.0, "highestRespirationValue": 18.0,
                "awakeCount": 2, "avgSleepStress": 14.5, "avgHeartRate": 55.1,
                "sleepScoreFeedback": "POSITIVE_DEEP_2", "sleepScoreInsight": "NONE",
                "sleepScores": {"overall": {"value": 81, "qualifierKey": "GOOD"},
                                "totalDuration": {"qualifierKey": "FAIR"},
                                "deepPercentage": {"value": 20, "qualifierKey": "EXCELLENT", "optimalStart": 16.0, "optimalEnd": 33.0},
                                "remPercentage": {"value": 25, "qualifierKey": "GOOD", "optimalStart": 21.0, "optimalEnd": 31.0}},
                "sleepNeed": {"baseline": 480, "actual": 500, "feedback": "INCREASED"},
            },
            "sleepLevels": [{"startGMT": "2026-11-13T22:10:00.0", "endGMT": "2026-11-13T22:40:00.0", "activityLevel": 1.0},
                            {"startGMT": "2026-11-13T22:40:00.0", "endGMT": "2026-11-13T23:30:00.0", "activityLevel": 0.0},
                            {"startGMT": "2026-11-13T23:30:00.0", "endGMT": "2026-11-13T23:40:00.0", "activityLevel": 7.0},
                            {"startGMT": "bad", "endGMT": "2026-11-13T23:50:00.0", "activityLevel": 2.0}],
            "sleepHeartRate": [{"value": 54, "startGMT": 1794607800000}],
            "hrvData": [{"value": 63.0, "startGMT": 1794607800000}],
            "sleepBodyBattery": [{"value": 30, "startGMT": 1794607800000}, {"value": 86, "startGMT": 1794634980000}],
            "restlessMomentsCount": 31, "avgOvernightHrv": 61.0, "hrvStatus": "BALANCED", "bodyBatteryChange": 56,
            "avgSkinTempDeviationC": -0.3, "restingHeartRate": 51,
        },
        "morning_training_readiness": {"score": 72, "level": "HIGH", "feedbackShort": "READY_HIGH_3", "recoveryTime": 120,
                                       "sleepScoreFactorPercent": 80, "sleepScoreFactorFeedback": "GOOD",
                                       "hrvFactorPercent": 95, "hrvFactorFeedback": "GOOD"},
        "training_readiness": [{"score": 55, "timestamp": "2026-11-14T17:30:00.0", "inputContext": "AFTER_POST_EXERCISE_RESET"},
                               {"score": 72, "timestamp": "2026-11-14T06:30:00.0", "inputContext": "AFTER_WAKEUP_RESET"}],
        "training_status": {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"9": {
            "trainingStatus": 7, "trainingStatusFeedbackPhrase": "PRODUCTIVE_3", "primaryTrainingDevice": True,
            "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 420, "dailyTrainingLoadChronic": 380,
                                     "dailyAcuteChronicWorkloadRatio": 1.1, "acwrStatus": "OPTIMAL"}}}},
            "mostRecentVO2Max": {"generic": {"calendarDate": DAY, "vo2MaxPreciseValue": 51.3}}},
        "training_load_balance": {"metricsTrainingLoadBalanceDTOMap": {"9": {"monthlyLoadAerobicLow": 300,
                                                                            "trainingBalanceFeedbackPhrase": "BALANCED_2"}}},
        "max_metrics": [],
        "endurance_score": {"enduranceScoreDTO": {"overallScore": 6100, "classificationLowerLimitIntermediate": 5100,
                                                  "classificationLowerLimitTrained": 5800,
                                                  "classificationLowerLimitWellTrained": 6500}},
        "hill_score": {"hillScoreDTOList": [{"calendarDate": DAY, "overallScore": 61, "strengthScore": None}]},
        "race_predictions": [{"calendarDate": DAY, "time5K": 1290, "time10K": 2700, "timeHalfMarathon": None, "timeMarathon": -1}],
        "fitness_age": {"chronologicalAge": 41, "fitnessAge": 36.4, "achievableFitnessAge": 33.0},
        "weigh_ins": {"dateWeightList": [{"weight": 72500.0, "bmi": 22.6, "bodyFat": None, "timestampGMT": 2},
                                         {"weight": 72900.0, "bmi": 22.7, "bodyFat": 18.1, "timestampGMT": 1}]},
        "hydration": {"valueInML": 1800.0, "goalInML": 2500.0, "sweatLossInML": None},
    }


def test_a_full_day():
    got = extract.day(DAY, _sample())
    d = got["days"]
    assert (d["steps"], d["steps_goal"], d["resting_hr"], d["stress_avg"]) == (9120, 8000, 52, 27)
    assert (d["body_battery_high"], d["body_battery_low"]) == (85, 22)  # from the samples: the summary has none
    assert d["intensity_minutes"] == 50 and (d["moderate_minutes"], d["vigorous_minutes"]) == (20, 15)
    assert (d["sleep_start"], d["sleep_end"]) == ("2026-11-13 23:10", "2026-11-14 06:50")
    assert (d["sleep_deep_seconds"], d["sleep_awake_seconds"], d["sleep_score"]) == (5400, 1200, 81)
    assert (d["skin_temp_deviation_c"], d["sleep_spo2_avg"], d["respiration_avg"]) == (-0.3, 94, 15.2)
    assert (d["hrv_last_night_avg"], d["hrv_status"], d["training_readiness"]) == (61, "BALANCED", 72)
    assert (d["weight_kg"], d["bmi"], d["body_fat_pct"]) == (72.5, 22.6, None)  # the day's last weigh-in, in kg
    assert d["vo2max_running"] == 51.3  # from training status, because it is that day's value
    s = got["sleep"]
    assert (s["score"], s["quality"], s["deep_pct"], s["deep_ideal_low_pct"], s["light_pct"]) == (81, "GOOD", 20, 16.0, None)
    assert (s["need_minutes"], s["body_battery_at_wake"], s["restless_moments"], s["feedback"]) == (500, 86, 31, "POSITIVE_DEEP")
    w = got["daily"]
    # Medium and high are not in the summary: counted from the samples (3 minutes each).
    assert (w["stress_rest_seconds"], w["stress_medium_seconds"], w["stress_high_seconds"]) == (30000, 180, 180)
    assert (w["body_battery_charged"], w["body_battery_latest"], w["body_battery_feedback"]) == (55, 22, "DAY_STRESSFUL")
    assert (w["hrv_baseline_low"], w["hrv_baseline_high"], w["hrv_feedback"]) == (50, 66, "HRV_BALANCED")
    assert (w["intensity_week_total"], w["intensity_week_goal"], w["resting_hr_7d"]) == (140, 150, 53)
    t = got["training"]
    assert (t["readiness"], t["readiness_latest"], t["readiness_level"], t["readiness_feedback"]) == (72, 55, "HIGH", "READY_HIGH")
    assert (t["sleep_factor_pct"], t["hrv_factor"], t["load_factor_pct"]) == (80, "GOOD", None)
    assert (t["status"], t["acute_load"], t["load_ratio"], t["load_balance_feedback"]) == ("PRODUCTIVE", 420, 1.1, "BALANCED")
    assert (t["endurance_score"], t["endurance_class"], t["hill_score"], t["hill_strength"]) == (6100, "trained", 61, None)
    assert (t["race_5k_seconds"], t["race_half_seconds"], t["race_marathon_seconds"]) == (1290, None, None)
    assert (t["fitness_age"], t["chronological_age"]) == (36.4, 41)
    events = {(e["type"], e["start"]): e for e in got["events"]}
    assert events[("sleep", "2026-11-13 23:10")]["impact"] == 58
    assert events[("activity", "2026-11-14 17:00")]["duration_seconds"] == 3600


def test_series_are_local_and_leave_out_garmins_markers():
    got = extract.day(DAY, _sample())
    by = {}
    for date, series, at, v in got["samples"]:
        assert date == DAY
        by.setdefault(series, []).append((at, v))
    assert by["heart_rate"] == [("2026-11-14 00:00", 60), ("2026-11-14 00:04", 58)]  # a null bpm and junk rows left out
    assert by["stress"] == [("2026-11-14 00:00", 20), ("2026-11-14 00:06", 60), ("2026-11-14 00:09", 80)]  # -1 and -2 out
    assert by["body_battery"][1] == ("2026-11-14 00:03", 85)
    assert by["respiration"] == [("2026-11-14 00:00", 14)]
    assert by["steps"] == [("2026-11-14 00:00", 0), ("2026-11-14 08:00", 420)]
    assert by["spo2"] == [("2026-11-14 01:00", 96)]
    assert by["sleep_heart_rate"] == [("2026-11-13 23:10", 54)] and by["sleep_hrv"] == [("2026-11-13 23:10", 63.0)]
    # Stages: a level Garmin does not define (7) and a segment without a start are left out.
    assert [(s["start"], s["until"], s["stage"], s["seconds"]) for s in got["stages"]] == [
        ("2026-11-13 23:10", "2026-11-13 23:40", "light", 1800), ("2026-11-13 23:40", "2026-11-14 00:30", "deep", 3000)]
    assert extract.day(DAY, _sample(), intraday=False)["samples"] == []


def test_nothing_at_all():
    got = extract.day(DAY, {})
    assert all(v is None for v in got["days"].values())
    assert got["sleep"] is got["daily"] is got["training"] is None
    assert got["events"] == got["samples"] == got["stages"] == []


@pytest.mark.parametrize("empty", [None, [], {}, "", 0, "text", [None], [[]], {"x": None}, True])
def test_every_source_empty_or_of_another_shape(empty):
    got = extract.day(DAY, {name: empty for name in index.DAY_SOURCES + index.INTRADAY_SOURCES})
    assert all(v is None for v in got["days"].values())
    assert got["sleep"] is got["daily"] is got["training"] is None
    assert got["samples"] == got["stages"] == got["events"] == []


def _nulls(x):
    if isinstance(x, dict):
        return {k: _nulls(v) if isinstance(v, (dict, list)) else None for k, v in x.items()}
    if isinstance(x, list):
        return [_nulls(v) for v in x]
    return None


def test_every_field_null():
    got = extract.day(DAY, _nulls(_sample()))
    assert all(v is None for v in got["days"].values())
    assert got["samples"] == got["stages"] == got["events"] == []


def _scramble(x, rng):
    """The sample with values swapped for others of the wrong type, lists cut short, keys dropped."""
    odd = [None, "", "x", -1, 10**30, 1e308, float("nan"), True, [], {}, [None], {"a": None}]
    if isinstance(x, dict):
        return {k: _scramble(v, rng) for k, v in x.items() if rng.random() > 0.1}
    if isinstance(x, list):
        return [_scramble(v, rng) for v in x[: rng.randint(0, len(x))]]
    return rng.choice(odd) if rng.random() < 0.4 else x


def test_scrambled_inputs_never_raise():
    rng = random.Random(7)
    for _ in range(300):
        got = extract.day(DAY, _scramble(_sample(), rng))
        for series in got["samples"]:
            assert isinstance(series[3], (int, float)) and series[3] >= 0


def test_the_tables_are_created_from_their_lists():
    db = sqlite3.connect(":memory:")
    for name in extract.TABLES:
        for s in extract.ddl(name):
            db.execute(s)
        cols = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
        assert tuple(cols) == extract.columns(name) and cols[0] == "date" and name.startswith("garmin_")
    assert set(extract.INTRADAY_TABLES) <= set(extract.TABLES)


# ------------------------------------------------------------------ through the index


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def _day(root, date: str, sources: dict) -> None:
    ddir = root / "garmin" / "days" / date[:4] / date
    if not (ddir / "day.json").exists():
        _write(ddir / "day.json", {"date": date, "has_data": True, "headline": {"steps": None}})
    for name, data in sources.items():
        _write(ddir / "processed" / f"{name}.json", data)


def _db_rows(sql: str):
    with sqlite3.connect(paths.DB) as db:
        return db.execute(sql).fetchall()


def test_the_index_fills_the_tables_and_keeps_only_recent_series(archive, monkeypatch):
    from fitdata_garmin import config
    sample = _sample()
    _day(archive, "2026-11-10", sample)
    _day(archive, "2026-11-12", {**sample, "sleep": "{not json", "stress": [], "hrv": {"hrvSummary": None}})
    _day(archive, DAY, sample)  # DAY already has a day.json with steps 1234 (conftest)
    monkeypatch.setenv("FITDATA_INTRADAY_DAYS", "3")
    config.load.cache_clear()
    index.rebuild()
    assert _db_rows("SELECT date, steps, sleep_score, body_battery_high FROM days ORDER BY date") == [
        ("2026-11-10", 9120, 81, 85), ("2026-11-12", 9120, None, None), (DAY, 1234, 81, 85)]  # day.json's steps first
    assert _db_rows("SELECT date FROM garmin_sleep ORDER BY date") == [("2026-11-10",), (DAY,)]
    assert _db_rows("SELECT count(*) FROM garmin_daily") == [(3,)] and _db_rows("SELECT count(*) FROM garmin_training") == [(3,)]
    # Series only for the last 3 days of the archive (2026-11-12 to DAY); 2026-11-12 has no sleep.
    assert _db_rows("SELECT DISTINCT date FROM garmin_samples ORDER BY date") == [("2026-11-12",), (DAY,)]
    assert _db_rows("SELECT DISTINCT date FROM garmin_sleep_stages") == [(DAY,)]
    before = {t: _db_rows(f'SELECT * FROM "{t}" ORDER BY 1, 2') for t in extract.TABLES}
    index.rebuild()  # idempotent: the same rows, nothing doubled
    assert {t: _db_rows(f'SELECT * FROM "{t}" ORDER BY 1, 2') for t in extract.TABLES} == before
    monkeypatch.setenv("FITDATA_INTRADAY_DAYS", "0")
    config.load.cache_clear()
    index.rebuild()
    assert _db_rows("SELECT count(*) FROM garmin_samples") == [(0,)] and _db_rows("SELECT count(*) FROM garmin_sleep") == [(2,)]


def test_numbers_keep_their_form_in_the_app_tables(archive):
    _day(archive, DAY, _sample())
    index.rebuild()
    (row,) = _db_rows("SELECT typeof(hrv_baseline_low), typeof(respiration_low), hrv_baseline_low FROM garmin_daily")
    assert row == ("integer", "real", 50)


def test_the_sql_command_reads_the_app_tables(archive, run):
    _day(archive, DAY, _sample())
    index.rebuild()
    code, out, err = run("--json", "sql", "SELECT series, count(*) AS n FROM garmin_samples GROUP BY series ORDER BY series")
    assert code == 0, err
    assert {r["series"] for r in json.loads(out)} >= {"heart_rate", "stress", "body_battery", "sleep_hrv"}
    code, out, _ = run("--json", "schema")
    tables = json.loads(out)["tables"]
    assert set(tables) == {"days", "activities", *index.APP_TABLES} and tables["garmin_samples"] == ["date", "series", "at", "value"]


@pytest.mark.parametrize("value", ["-1", "x", "3661"])
def test_a_bad_intraday_setting_is_said_in_plain_words(tmp_path, monkeypatch, value):
    from fitdata_garmin import config
    from fitdata_garmin.errors import ConfigProblem
    monkeypatch.setenv("FITDATA_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("FITDATA_INTRADAY_DAYS", value)
    config.load.cache_clear()
    with pytest.raises(ConfigProblem, match="intraday_days"):
        config.load()
    config.load.cache_clear()
