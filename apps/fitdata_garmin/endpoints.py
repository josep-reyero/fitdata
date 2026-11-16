"""What we fetch from Garmin Connect. Each entry becomes processed/<name>.json.

Daily endpoints take the calendar date (YYYY-MM-DD). Activity endpoints take the activity id.
Add or remove entries here; nothing else needs to change.
"""
from __future__ import annotations

from typing import Any, Callable

from garminconnect import Garmin

Daily = Callable[[Garmin, str], Any]
PerActivity = Callable[[Garmin, int], Any]

DAILY: dict[str, Daily] = {
    # Day totals: steps, calories, distance, floors, HR, stress, body battery, intensity minutes...
    "daily_summary": lambda g, d: g.get_user_summary(d),
    # Intraday series
    "heart_rate": lambda g, d: g.get_heart_rates(d),
    "stress": lambda g, d: g.get_stress_data(d),
    "body_battery": lambda g, d: g.get_body_battery(d, d),
    "body_battery_events": lambda g, d: g.get_body_battery_events(d),
    "steps": lambda g, d: g.get_steps_data(d),
    "floors": lambda g, d: g.get_floors(d),
    "respiration": lambda g, d: g.get_respiration_data(d),
    "spo2": lambda g, d: g.get_spo2_data(d),
    "intensity_minutes": lambda g, d: g.get_intensity_minutes_data(d),
    "all_day_events": lambda g, d: g.get_all_day_events(d),
    # Sleep and recovery
    "sleep": lambda g, d: g.get_sleep_data(d),
    "naps": lambda g, d: g.get_naps(d),
    "hrv": lambda g, d: g.get_hrv_data(d),
    "resting_hr": lambda g, d: g.get_rhr_day(d),
    "training_readiness": lambda g, d: g.get_training_readiness(d),
    "morning_training_readiness": lambda g, d: g.get_morning_training_readiness(d),
    # Performance and training (Garmin/Firstbeat calculations)
    "training_status": lambda g, d: g.get_training_status(d),
    "training_load_balance": lambda g, d: g.get_training_four_week_load_balance(d),
    "max_metrics": lambda g, d: g.get_max_metrics(d),  # VO2 max (running/cycling), fitness age inputs
    "endurance_score": lambda g, d: g.get_endurance_score(d, d, aggregation="daily"),
    "hill_score": lambda g, d: g.get_hill_score(d, d),
    "race_predictions": lambda g, d: g.get_race_predictions(d, d, _type="daily"),
    "running_tolerance": lambda g, d: g.get_running_tolerance(d, d, aggregation="daily"),
    "stamina_curve": lambda g, d: g.get_stamina_curve(d),
    "fitness_age": lambda g, d: g.get_fitnessage_data(d),
    "heat_altitude_acclimation": lambda g, d: g.get_heat_altitude_acclimation(d),
    # Body and logging
    "weigh_ins": lambda g, d: g.get_daily_weigh_ins(d),
    "blood_pressure": lambda g, d: g.get_blood_pressure_day(d),
    "hydration": lambda g, d: g.get_hydration_data(d),
    "health_snapshots": lambda g, d: g.get_health_snapshots(d),
    "lifestyle_logging": lambda g, d: g.get_lifestyle_logging_data(d),
}

ACTIVITY: dict[str, PerActivity] = {
    "summary": lambda g, i: g.get_activity(i),
    "details": lambda g, i: g.get_activity_details(i, maxchart=100000, maxpoly=100000),
    "splits": lambda g, i: g.get_activity_splits(i),
    "split_summaries": lambda g, i: g.get_activity_split_summaries(i),
    "typed_splits": lambda g, i: g.get_activity_typed_splits(i),
    "hr_zones": lambda g, i: g.get_activity_hr_in_timezones(i),
    "power_zones": lambda g, i: g.get_activity_power_in_timezones(i),
    "power_curve": lambda g, i: g.get_activity_power_curve(i),
    "weather": lambda g, i: g.get_activity_weather(i),
    "gear": lambda g, i: g.get_activity_gear(i),
    "exercise_sets": lambda g, i: g.get_activity_exercise_sets(i),
    "personal_records": lambda g, i: g.get_personal_records_for_activity(i),
}

ACCOUNT: dict[str, Callable[[Garmin], Any]] = {
    "profile": lambda g: g.get_user_profile(),
    "devices": lambda g: g.get_devices(),
    "heart_rate_zones": lambda g: g.get_heart_rate_zones(),
    "power_zones": lambda g: g.get_power_zones(),
    "gear": lambda g: g.get_gear_list(limit=200),
    "personal_records": lambda g: g.get_personal_record(),
    "lifetime_totals": lambda g: g.get_lifetime_totals(),
}


DESCRIPTIONS: dict[str, str] = {
    # daily
    "daily_summary": "Day totals: steps, distance, calories, floors, HR min/max/resting, stress, body battery, intensity minutes",
    "heart_rate": "Heart rate through the day (timestamped samples)",
    "stress": "Stress level through the day, plus body battery samples",
    "body_battery": "Body Battery charged/drained and samples",
    "body_battery_events": "Events that changed Body Battery (sleep, activities, naps)",
    "steps": "Steps in 15-minute buckets",
    "floors": "Floors climbed/descended in buckets",
    "respiration": "Breathing rate through the day and sleep",
    "spo2": "Blood oxygen (pulse ox) readings",
    "intensity_minutes": "Moderate/vigorous intensity minutes",
    "all_day_events": "Auto-detected moves and events",
    "sleep": "Sleep: stages, score, timing, HR/respiration/SpO2/stress during sleep",
    "naps": "Naps",
    "hrv": "Overnight HRV: nightly average, 5-minute readings, status and baseline",
    "resting_hr": "Resting heart rate",
    "training_readiness": "Training Readiness score and its factors",
    "morning_training_readiness": "The morning Training Readiness report",
    "training_status": "Training Status, acute/chronic load, load focus, VO2 max trend",
    "training_load_balance": "Four-week load balance (aerobic low/high, anaerobic)",
    "max_metrics": "VO2 max (running/cycling) and related estimates for the day",
    "endurance_score": "Endurance score",
    "hill_score": "Hill score (strength and endurance on climbs)",
    "race_predictions": "Predicted 5K/10K/half/marathon times",
    "running_tolerance": "Running tolerance (load the body can absorb)",
    "stamina_curve": "Stamina curve",
    "fitness_age": "Fitness age",
    "heat_altitude_acclimation": "Heat and altitude acclimation",
    "weigh_ins": "Weight and body composition entries",
    "blood_pressure": "Blood pressure entries",
    "hydration": "Hydration log and goal",
    "health_snapshots": "Health Snapshot sessions",
    "lifestyle_logging": "Lifestyle logging entries",
    "raw_wellness_fit": "The watch's original all-day FIT files (raw/wellness/)",
    # activity
    "summary": "Activity summary: type, times, distance, ascent, HR, pace, power, training effect, load",
    "details": "Per-sample metric series as Garmin charts them, plus the GPS polyline",
    "splits": "Laps/splits as recorded",
    "split_summaries": "Split totals by kind (run/walk/climb...)",
    "typed_splits": "Auto-detected split types",
    "hr_zones": "Time in each heart-rate zone",
    "power_zones": "Time in each power zone",
    "power_curve": "Best power over durations",
    "weather": "Weather during the activity",
    "gear": "Gear used (e.g. shoes)",
    "exercise_sets": "Strength sets and reps",
    "personal_records": "Personal records set",
    "raw_fit": "The original FIT file from the watch (raw/)",
}
