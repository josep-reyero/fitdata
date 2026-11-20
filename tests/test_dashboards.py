"""The fitness dashboards and their synthetic sample data (ui/fixtures/).

The project's Today page is ui/dashboards/overview.yaml, with anchors where fitdata-garmin adds its
sections (apps/fitdata_garmin/dashboards/overview-garmin.yaml); Sleep, Health and Workouts are
fitdata-garmin's own dashboards (apps/fitdata_garmin/dashboards/). The dashboards read the project's shared views, days and
activities, and fitdata-garmin's garmin_* tables listed in its [ui] tables.

Most tests need only fitdata: they compose the dashboards as the apps contract says
(tests/compose_dashboards.py) and run every query with SQLite on both samples: the full one (120
days) and the "new watch" one (the first 5 days of an account: 4 nights, no activities, no
weigh-ins), so every page is checked with plenty of data and with almost none. The last ones need
a my-easy-ui that composes dashboards and are skipped otherwise. To run
everything:

    uv run --with-editable <path to my-easy-ui> python -m pytest tests/test_dashboards.py

(`python -m pytest`, so pytest runs in the environment that has my-easy-ui added.)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from compose_dashboards import apps, compose, order, sources
from conftest import easyui_for_project

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "ui" / "fixtures" / "fitdata-sample.db"
NEW_WATCH = REPO / "ui" / "fixtures" / "new-watch" / "fitdata-new-watch.db"
MAKE_FIXTURE = REPO / "ui" / "fixtures" / "make_fixture.py"
SLUGS = ("overview", "sleep", "health", "workouts")
SHARED = ("days", "activities")
GARMIN = ("garmin_sleep", "garmin_daily", "garmin_training", "garmin_body_battery_events", "garmin_samples",
          "garmin_sleep_stages")
LATEST = "2026-05-04"       # the latest day of both samples
NO_WATCH = "2026-02-13"     # a day the watch was not worn
OLD_DAY = "2026-04-01"      # a day with daily numbers but before the window kept through the day


def _rows(db: Path, sql: str, *args) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _columns(db: Path, table: str) -> list[str]:
    return [r[1] for r in _rows(db, f'PRAGMA table_info("{table}")')]


def _make_fixture_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_fixture", MAKE_FIXTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ sample data (fitdata only)


def test_headline_keys_match_fitdata(tmp_path, monkeypatch):
    """The generator's column lists are fitdata's own; a new headline field fails here."""
    monkeypatch.setenv("FITDATA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("FITDATA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FITDATA_REMOTE", "")
    sync = pytest.importorskip("fitdata_garmin.sync")
    mf = _make_fixture_module()
    assert tuple(sync._day_headline(tmp_path / "nothing")) == mf.DAY_HEADLINE_KEYS
    assert tuple(sync._activity_headline({})) == mf.ACTIVITY_HEADLINE_KEYS


@pytest.mark.parametrize("variant, committed", [("sample", FIXTURE), ("new-watch", NEW_WATCH)])
def test_fixture_is_what_the_generator_and_the_real_indexer_make(tmp_path, variant, committed):
    out = tmp_path / "fresh.sqlite"
    subprocess.run([sys.executable, str(MAKE_FIXTURE), "--variant", variant, "--out", str(out)], check=True,
                   capture_output=True)
    objects = "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY type, name"
    assert _rows(committed, objects) == [("table", "activities__rows"), ("table", "days__rows"),
                                         *[("table", t) for t in sorted((*GARMIN, "garmin_files"))],
                                         ("view", "activities"), ("view", "days")]
    assert _rows(committed, objects) == _rows(out, objects)
    for (_, t) in _rows(committed, objects):
        assert _columns(committed, t) == _columns(out, t), f"columns of {t} differ: run ui/fixtures/make_fixture.py"
        everything = f'SELECT * FROM "{t}" ORDER BY ' + ", ".join(str(i + 1) for i in range(len(_columns(committed, t))))
        assert _rows(committed, everything) == _rows(out, everything), (
            f"{committed.relative_to(REPO)} is out of date ({t}): run "
            f"uv run python ui/fixtures/make_fixture.py --variant {variant}"
        )
    assert _rows(committed, "SELECT DISTINCT source FROM days__rows") == [("fitdata-garmin",)]


def test_fixture_has_realistic_gaps():
    one = lambda sql: _rows(FIXTURE, sql)[0][0]  # noqa: E731
    assert one("SELECT count(*) FROM days") == 119            # 120 days, one missing from the archive
    assert one("SELECT min(date) || ' ' || max(date) FROM days") == f"2026-01-05 {LATEST}"
    assert 25 <= one("SELECT count(*) FROM activities") <= 40
    assert one("SELECT count(*) FROM days WHERE has_data = 0") == 3
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND sleep_score IS NULL") >= 3
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND hrv_last_night_avg IS NULL") >= 3
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND body_battery_high IS NULL") >= 1
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND sleep_spo2_avg IS NULL") >= 10
    assert one("SELECT count(*) FROM activities WHERE avg_hr IS NULL") >= 1
    assert one("SELECT count(*) FROM activities WHERE distance_m = 0 OR distance_m IS NULL") >= 1
    assert one("SELECT count(*) FROM days WHERE complete = 0") == 2  # the last two days still settle
    # Only the most recent days keep their readings through the day; weigh-ins and water are sparse.
    assert one("SELECT count(DISTINCT date) FROM garmin_samples") == 7
    assert one("SELECT count(DISTINCT date) FROM garmin_sleep_stages") <= 7
    assert 20 <= one("SELECT count(*) FROM days WHERE weight_kg IS NOT NULL") < 60
    assert 20 <= one("SELECT count(*) FROM days WHERE hydration_ml > 0") < 60
    assert one("SELECT count(*) FROM garmin_sleep") < one("SELECT count(*) FROM days")


def test_the_new_watch_is_an_account_that_has_just_started():
    one = lambda sql: _rows(NEW_WATCH, sql)[0][0]  # noqa: E731
    assert one("SELECT min(date) || ' ' || max(date) || ' ' || count(*) FROM days") == f"2026-04-30 {LATEST} 5"
    assert one("SELECT count(*) FROM days WHERE has_data = 1") == 5
    assert one("SELECT count(*) FROM days WHERE sleep_seconds > 0") == 4  # put on during the first day
    assert one("SELECT min(at) FROM garmin_samples WHERE date = '2026-04-30' AND series = 'heart_rate'") >= "2026-04-30 10:40"
    assert one("SELECT count(*) FROM activities") == 0
    assert one("SELECT count(*) FROM days WHERE weight_kg IS NOT NULL OR hydration_ml > 0") == 0
    assert one("SELECT count(*) FROM garmin_training WHERE fitness_age IS NOT NULL OR heat_acclimation_pct IS NOT NULL "
               "OR race_5k_seconds IS NOT NULL OR status IS NOT NULL OR vo2max_running IS NOT NULL") == 0
    assert one("SELECT count(*) FROM garmin_daily WHERE hrv_baseline_low IS NOT NULL") == 0
    assert one("SELECT count(*) FROM days WHERE hrv_status IS NOT NULL") == 0
    assert one("SELECT count(*) FROM garmin_training WHERE readiness IS NOT NULL") == 4


def test_the_dashboards_may_read_the_shared_views_and_the_apps_ui_tables():
    """The allowlist (contract 4.6): the shared views, then [views], then each app's [ui] tables."""
    project = tomllib.loads((REPO / "project.toml").read_text())
    allow = [*project["tables"], *project.get("views", {}), *[t for a in apps(REPO) for t in a["ui"].get("tables", [])]]
    assert allow == [*SHARED, *GARMIN] and "tables" not in project["ui"]


# ------------------------------------------------------------------ composition (fitdata only)


def test_where_each_dashboard_comes_from_and_the_menu():
    found = sources(REPO)
    assert {s: f for s, (f, _) in found.items()} == {s: ("project" if s == "overview" else "fitdata-garmin") for s in SLUGS}
    assert order(REPO) == list(SLUGS)


def test_the_overview_is_todays_page_once_composed():
    dash, placed, warnings = compose(REPO, "overview")
    assert warnings == [] and placed == [
        {"app": "fitdata-garmin", "id": "garmin-readings", "file": "apps/fitdata_garmin/dashboards/overview-garmin.yaml",
         "placed": "after:readings"},
        {"app": "fitdata-garmin", "id": "garmin-charts", "file": "apps/fitdata_garmin/dashboards/overview-garmin.yaml",
         "placed": "after:charts"}]
    assert list(dash["queries"]) == ["shown", "garmin_today", "garmin_night", "garmin_day_energy", "garmin_day_heart"]
    assert not any(isinstance(r, dict) for r in dash["layout"])  # the anchors are gone
    shape = [[c["type"] for c in row] for row in dash["layout"]]
    assert shape == [["controls", "stat"], ["stat"] * 4, ["stat"] * 4, ["timeline", "line", "line"], ["text"]]
    queries = {c.get("query") for row in dash["layout"] for c in row} - {None}
    assert queries == set(dash["queries"])


def compose_dashboards_raw(slug: str) -> dict:
    import yaml
    return yaml.safe_load(sources(REPO)[slug][1].read_text())


def test_the_project_overview_alone_shows_only_shared_data():
    """Without the app's sections, the project's page still works: it reads only shared views."""
    raw = compose_dashboards_raw("overview")
    assert [r for r in raw["layout"] if isinstance(r, dict)] == [{"anchor": "readings"}, {"anchor": "charts"}]
    assert list(raw["queries"]) == ["shown"]
    words = set(re.findall(r"[a-z_]+", raw["queries"]["shown"]["sql"].lower()))
    assert not words & set(GARMIN)


def test_a_project_override_removes_or_replaces_the_apps_sections(tmp_path):
    copy = tmp_path / "proj"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "*.db", "screenshots"))
    (copy / "ui" / "sections").mkdir()
    (copy / "ui" / "sections" / "fitdata-garmin.overview.yaml").write_text("spec: easy-ui/sections-v1\nsections: []\n")
    dash, placed, _ = compose(copy, "overview")
    assert placed == [] and list(dash["queries"]) == ["shown"] and len(dash["layout"]) == 1


def test_sections_follow_the_contract():
    import yaml
    (app,) = apps(REPO)
    for slug, c in app["ui"]["contributes"].items():
        assert slug in sources(REPO)
        doc = yaml.safe_load((app["folder"] / c["sections"]).read_text())
        assert set(doc) == {"spec", "sections"} and doc["spec"] == "easy-ui/sections-v1" and len(doc["sections"]) <= 10
        for s in doc["sections"]:
            assert set(s) <= {"id", "position", "queries", "layout"} and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", s["id"])
            assert all(q.startswith("garmin_") for q in s["queries"]) and 1 <= len(s["layout"]) <= 10


# ------------------------------------------------------------------ every query on the sample (fitdata only)


def _defaults(dash: dict) -> dict:
    out = {}
    for name, p in (dash.get("params") or {}).items():
        out[name] = p["default"]
    return out


def _run(slug: str, db: Path = FIXTURE, **params) -> dict:
    """Every query of a dashboard, with its defaults (the day: later than the samples' latest day,
    as when the watch has not synced yet) overridden by params."""
    dash, _, _ = compose(REPO, slug)
    values = {**_defaults(dash), "day": "2026-11-18", **params}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {}
        for name, q in dash["queries"].items():
            used = {k: v for k, v in values.items() if re.search(rf":{k}\b", q["sql"])}
            cur = con.execute(q["sql"], used)
            out[name] = {"columns": [d[0] for d in cur.description], "rows": cur.fetchall()}
        return out
    finally:
        con.close()


def _one(r: dict) -> dict:
    return dict(zip(r["columns"], r["rows"][0]))


def _all(r: dict) -> list[dict]:
    return [dict(zip(r["columns"], row)) for row in r["rows"]]


def test_every_dashboard_reads_only_what_it_may():
    stored = {n for (n,) in _rows(FIXTURE, "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    for slug in SLUGS:
        dash, _, _ = compose(REPO, slug)
        for name, q in dash["queries"].items():
            words = set(re.findall(r"[a-z_]+", q["sql"].lower()))
            assert words & stored <= {*SHARED, *GARMIN}, (slug, name)


@pytest.mark.parametrize("slug", SLUGS)
def test_every_control_is_shown_and_every_query_used(slug):
    dash, _, _ = compose(REPO, slug)
    assert dash["layout"][0][0] == {"type": "controls"}
    used = {c.get("query") for row in dash["layout"] for c in row} - {None}
    assert used == set(dash["queries"]), slug  # no query is left unused, none is missing
    sql = " ".join(q["sql"] for q in dash["queries"].values())
    for p in dash.get("params") or {}:
        assert re.search(rf":{p}\b", sql), (slug, p)
    if "day" in (dash.get("params") or {}):
        assert dash["params"]["day"]["type"] == "date" and dash["params"]["day"]["default"] == "today"


def _choices(slug: str) -> list[dict]:
    dash, _, _ = compose(REPO, slug)
    params = dash.get("params") or {}
    out = [{}]
    if "days" in params:
        out = [{"days": o["value"]} for o in params["days"]["options"]]
    return out


# Queries that return no rows on purpose with each sample: the "no workouts" lines once there are
# workouts, and the panels (all hide_empty) that need what a new watch does not have yet.
EMPTY = {
    FIXTURE: {"workouts.no_workouts", "workouts.none_in_period"},
    NEW_WATCH: {"workouts.none_in_period", "workouts.recent", "workouts.weekly", "workouts.by_sport", "workouts.load",
                "health.body", "health.weight"},
}


@pytest.mark.parametrize("db", [FIXTURE, NEW_WATCH], ids=["sample", "new-watch"])
@pytest.mark.parametrize("slug", SLUGS)
def test_every_query_has_rows_on_the_latest_day_for_every_period(slug, db):
    for choice in _choices(slug):
        for name, r in _run(slug, db, **choice).items():
            if f"{slug}.{name}" in EMPTY[db]:
                assert r["rows"] == [], f"{slug}.{name} should have no rows for {choice}"
            else:
                assert r["rows"], f"{slug}.{name} returned no rows for {choice}"


def test_a_page_without_anything_to_show_says_what_will_appear():
    """Workouts without any workout: one friendly line, the training numbers Garmin already has, and
    every other panel of the page left out (hide_empty), so no rows of dashes. Health leaves its
    body measures out until something is logged."""
    r = _run("workouts", NEW_WATCH)
    assert len(r["no_workouts"]["rows"]) == 1 and r["none_in_period"]["rows"] == []
    assert r["no_workouts"]["rows"][0][0].startswith("Start an activity on the watch")
    assert _one(r["today"])["ready"] is not None  # readiness needs only sleep
    assert _run("workouts")["no_workouts"]["rows"] == []  # the sample has workouts
    dash, _, _ = compose(REPO, "workouts")
    for row in dash["layout"][1:]:
        for c in row:
            if c["type"] not in ("controls", "text"):
                assert c.get("hide_empty") is True, c.get("title")
    health, _, _ = compose(REPO, "health")
    body = [c for row in health["layout"] for c in row if c.get("query") in ("body", "weight")]
    assert len(body) == 3 and all(c.get("hide_empty") is True for c in body)


def test_a_period_without_workouts_says_so():
    """Workouts exist, but none in the chosen period: one line, not an empty page."""
    early = "2026-01-04"  # the day before the sample starts: its workouts are all later
    r = _run("workouts", day=early)
    assert r["recent"]["rows"] == [] and r["no_workouts"]["rows"] == []
    assert r["none_in_period"]["rows"] == [("Choose a longer Period to see earlier ones.",)]


def test_a_new_watch_has_no_arrows_until_there_is_a_usual():
    """With fewer than 3 earlier days, nothing is compared: no arrow."""
    early = _one(_run("overview", NEW_WATCH, day="2026-05-02")["garmin_today"])
    assert early["score"] is not None and early["score_change"] is None and early["rhr_change"] is None
    assert early["hrv_word"] == "No baseline yet"
    later = _one(_run("overview", NEW_WATCH)["garmin_today"])
    assert later["rhr_change"] is not None  # 4 earlier days
    night = _one(_run("sleep", NEW_WATCH, day="2026-05-02")["night"])
    assert night["bed_change"] is None and night["bed_word"] is None
    health = _one(_run("health", NEW_WATCH)["today"])
    assert health["hrv_word"] == "No baseline yet"
    daily = _all(_run("health", NEW_WATCH)["daily"])
    assert all(d["baseline_low"] is None for d in daily)  # no band before Garmin has a baseline


def test_charts_of_days_start_with_the_data():
    """A new watch's charts span its own days, not the whole period: every row is a recorded day,
    and the nights start with the first night."""
    nights = _all(_run("sleep", NEW_WATCH, days=365)["nights"])
    assert [n["day"] for n in nights] == ["2026-05-01", "2026-05-02", "2026-05-03", LATEST]
    for slug, name in (("health", "daily"), ("workouts", "steps")):
        days = [r[0] for r in _run(slug, NEW_WATCH)[name]["rows"]]
        assert days[0] >= "2026-04-30" and len(days) <= 5, (slug, name)


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("day", [NO_WATCH, OLD_DAY, "2026-03-23", "2020-01-01"])
def test_a_day_without_data_shows_empty_panels_not_zeros(slug, day):
    """A day the watch was not worn, one before the window kept through the day, one missing from
    the archive and one before it: every query runs; the day's readings are empty, never 0."""
    r = _run(slug, day=day)
    if slug == "overview":
        row = _one(r["garmin_today"])
        assert row["steps"] in (None, 0) or day == OLD_DAY
    if day != OLD_DAY:
        for name in {"overview": ("garmin_day_energy", "garmin_day_heart", "garmin_night"), "sleep": ("stages",),
                     "health": ("through_the_day",)}.get(slug, ()):
            assert r[name]["rows"] == [], (slug, name)
    else:  # daily numbers yes, but nothing through the day
        for name in {"sleep": ("stages",), "health": ("through_the_day",)}.get(slug, ()):
            assert r[name]["rows"] == [], (slug, name)


def test_a_future_day_shows_the_latest_one():
    assert _one(_run("overview", day="2030-01-01")["shown"])["line"] == "Latest day with data: Mon 4 May 2026 · updated 13:20"
    assert _one(_run("overview", day=LATEST)["shown"])["line"] == "Showing Mon 4 May · updated 13:20"
    assert _one(_run("overview", day="2026-05-03")["shown"])["line"] == "Showing Sun 3 May · updated 4 May 06:20"
    assert _one(_run("sleep", day="2030-01-01")["night"])["woke"] == LATEST


def test_today_against_the_usual():
    r = _run("overview")
    row = _one(r["garmin_today"])
    ready, = _rows(FIXTURE, "SELECT readiness FROM garmin_training WHERE date = ?", LATEST)[0]
    before = [v for (v,) in _rows(FIXTURE, "SELECT readiness FROM garmin_training WHERE readiness IS NOT NULL "
                                            "AND date < ? AND date >= date(?, '-28 days')", LATEST, LATEST)]
    assert row["ready"] == ready and row["ready_change"] == pytest.approx(ready - sum(before) / len(before))
    rhr, = _rows(FIXTURE, "SELECT resting_hr FROM days WHERE date = ?", LATEST)[0]
    assert row["rhr"] == rhr
    asleep, = _rows(FIXTURE, "SELECT sleep_seconds FROM days WHERE date = ?", LATEST)[0]
    assert row["asleep"] == asleep and row["steps_word"].startswith("Goal ")


def test_sleep_night_matches_the_index():
    r = _run("sleep")
    night = _one(r["night"])
    score, asleep, start, end = _rows(FIXTURE, "SELECT sleep_score, sleep_seconds, sleep_start, sleep_end FROM days "
                                               "WHERE date = ?", LATEST)[0]
    assert night["score"] == score and night["asleep"] == asleep
    # Bed and wake times: seconds from the midnight before the night, shown with format: clock.
    clock = lambda secs: f"{int(secs) % 86400 // 3600:02d}:{int(secs) % 3600 // 60:02d}"  # noqa: E731
    assert clock(night["bed"]) == start[11:16] and clock(night["wake"]) == end[11:16]
    assert 72000 < night["bed"] < night["wake"] < 129600  # 20:00 the evening before to 12:00
    # The timeline: spans of time, one stage each, from falling asleep to waking up.
    stages = _all(r["stages"])
    assert stages[0]["start"] == start and stages[-1]["until"] == end
    assert {s["stage"] for s in stages} <= {"Deep", "Light", "REM", "Awake"}
    span = lambda s: (dt.datetime.fromisoformat(s["until"]) - dt.datetime.fromisoformat(s["start"])).total_seconds()  # noqa: E731
    deep, = _rows(FIXTURE, "SELECT sleep_deep_seconds FROM days WHERE date = ?", LATEST)[0]
    assert sum(span(s) for s in stages if s["stage"] == "Deep") == pytest.approx(deep, abs=120)


@pytest.mark.parametrize("days", [14, 30, 90, 365])
def test_sleep_nights_skip_missing_ones(days):
    nights = _all(_run("sleep", days=days)["nights"])
    expected = _rows(FIXTURE, "SELECT count(*) FROM days WHERE date > date(?, ?) AND date <= ? "
                              "AND date >= (SELECT min(date) FROM days WHERE sleep_seconds > 0)",
                     LATEST, f"-{days} days", LATEST)[0][0]
    assert len(nights) == expected
    missing = [n for n in nights if n["Sleep score"] is None]
    assert all(n["Deep"] is None for n in missing)  # not 0


def test_health_body_battery_inside_its_range_and_hrv_baseline():
    r = _run("health", days=90)
    daily = _all(r["daily"])
    assert daily and all(d["bb_low"] <= d["Body Battery at wake"] <= d["bb_high"] for d in daily
                         if d["Body Battery at wake"] is not None and d["bb_low"] is not None)
    row = _one(r["today"])
    low, high, status = _rows(FIXTURE, "SELECT g.hrv_baseline_low, g.hrv_baseline_high, s.hrv_status "
                                       "FROM garmin_daily g LEFT JOIN garmin_sleep s USING (date) WHERE date = ?",
                              LATEST)[0]
    assert row["hrv_word"].lower() == (status or "").lower().replace("_", " ")
    assert (daily[-1]["baseline_low"], daily[-1]["baseline_high"]) == (low, high)


def test_health_body_shows_only_logged_days():
    r = _run("health", days=365)
    assert len(r["weight"]["rows"]) == _rows(FIXTURE, "SELECT count(*) FROM days WHERE weight_kg IS NOT NULL")[0][0]
    body = _one(r["body"])
    weight, fat = _rows(FIXTURE, "SELECT weight_kg, body_fat_pct FROM days WHERE weight_kg IS NOT NULL "
                                 "ORDER BY date DESC LIMIT 1")[0]
    assert body["weight"] == weight and (f"{fat:.1f}% fat" in body["weight_word"]) == (fat is not None)


def test_workouts_steps_and_intensity_minutes():
    r = _run("workouts", days=30)
    steps = _all(r["steps"])
    assert [d["in_progress"] for d in steps].count(1) <= 1 and all(d["Steps"] != 0 for d in steps)
    weeks = [w["week"] for w in _all(r["intensity"])]
    assert weeks == sorted(weeks) and all(dt.date.fromisoformat(w).weekday() == 0 for w in weeks)


def test_workouts_weeks_and_sports_add_up():
    r = _run("workouts", days=365)
    acts = _rows(FIXTURE, "SELECT duration_s FROM activities WHERE date > date(?, '-365 days') AND date <= ?",
                 LATEST, LATEST)
    assert len(r["recent"]["rows"]) == len(acts)
    weekly = _all(r["weekly"])
    seconds = sum(w["Running"] + w["Cycling"] + w["Walking and hiking"] + w["Other"] for w in weekly)
    assert seconds == pytest.approx(sum(a for (a,) in acts), abs=1)
    by_sport = _all(r["by_sport"])
    assert sum(s["time"] for s in by_sport) == pytest.approx(seconds, abs=1)
    assert sum(s["workouts"] for s in by_sport) == len(acts)
    assert [s["time"] for s in by_sport] == sorted((s["time"] for s in by_sport), reverse=True)
    assert weekly[0]["week"] >= "2026-01-05"  # no made-up weeks before the archive
    recent = _all(r["recent"])
    strength = [x for x in recent if x["sport"] == "Strength training"]
    assert strength and all(x["distance"] is None for x in strength)
    today = _one(r["today"])
    assert today["acute_word"].startswith("Optimal ") and today["status"]


def test_dashboards_cope_with_an_empty_archive(tmp_path):
    """A fresh install: the index exists but has no rows yet. Nothing may fail."""
    empty = tmp_path / "empty.sqlite"
    shutil.copyfile(FIXTURE, empty)
    con = sqlite3.connect(empty)
    con.executescript("DELETE FROM days__rows; DELETE FROM activities__rows; DELETE FROM garmin_files;"
                      + "".join(f"DELETE FROM {t};" for t in GARMIN))
    con.commit()
    con.close()
    for slug in SLUGS:
        for choice in _choices(slug):
            for name, r in _run(slug, empty, **choice).items():
                for row in r["rows"][:1]:
                    assert all(v is None or v == 0 or isinstance(v, str) for v in row[1:]), (slug, name, row)


def test_dashboards_show_a_second_apps_days_and_activities(tmp_path):
    """The shared views at work: another app's activity shows on the Workouts page without any
    change to the dashboards."""
    from schema_script import schema_sql
    two = tmp_path / "two.sqlite"
    shutil.copyfile(FIXTURE, two)
    project = tomllib.loads((REPO / "project.toml").read_text())
    con = sqlite3.connect(two)
    con.executescript(schema_sql(project, ["fitdata-garmin", "fitdata-strava"]))
    con.execute("INSERT INTO activities__rows (source, id, date, type, duration_s, distance_m, start_local) "
                "VALUES ('fitdata-strava', 1, ?, 'road_biking', 3600, 30000, ? || ' 08:00:00')", (LATEST, LATEST))
    con.commit()
    con.close()
    before = _run("workouts", days=30)["recent"]["rows"]
    after = _run("workouts", two, days=30)["recent"]["rows"]
    assert len(after) == len(before) + 1 and after[0][0] == LATEST


def test_health_changes_say_which_way_is_good():
    """A higher resting heart rate must not look like good news: every delta says which way is
    better, except the few where neither way is (bedtime, wake time, weight)."""
    neutral = {("sleep", "bed_change"), ("sleep", "wake_change"), ("health", "weight_change")}
    for slug in SLUGS:
        dash, _, _ = compose(REPO, slug)
        for row in dash["layout"]:
            for c in row:
                if "delta" in c:
                    want = (None,) if (slug, c["delta"]) in neutral else ("higher", "lower")
                    assert c.get("better") in want, (slug, c.get("title"))
    rhr = [c for row in compose(REPO, "health")[0]["layout"] for c in row if c.get("delta") == "rhr_change"]
    assert rhr and rhr[0]["better"] == "lower"
    stress = [c for row in compose(REPO, "health")[0]["layout"] for c in row if c.get("delta") == "stress_change"]
    assert stress and stress[0]["better"] == "lower"


# ------------------------------------------------------------------ with my-easy-ui


@pytest.fixture
def easyui(tmp_path, monkeypatch):
    """my-easy-ui with the sample data as the project's database, all in temporary folders. Skipped
    unless this my-easy-ui composes dashboards (`easy-ui compose`)."""
    eu = easyui_for_project(REPO)
    from easyui import cli
    data = tmp_path / "mes" / "fitdata"
    data.mkdir(parents=True)
    shutil.copyfile(FIXTURE, data / "fitdata.sqlite")
    for k, v in {"MES_DATA_HOME": tmp_path / "mes", "EASYUI_STATE_HOME": tmp_path / "state",
                 "MES_CONFIG": tmp_path / "none.toml", "MES_STATE_HOME": tmp_path / "mes-state"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("MES_DATA_DIR", raising=False)

    def run(*argv: str) -> tuple[int, str, str]:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.run([*argv, "--project-dir", str(REPO)])
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
        return code, out.getvalue(), err.getvalue()
    code, out, err = run("compose", "--json")
    if code != 0:
        pytest.skip(f"this my-easy-ui does not compose dashboards yet: {(err or out).strip()[:200]}")
    return type("EasyUI", (), {"run": staticmethod(run), "module": eu})


def test_easy_ui_composes_as_the_tests_do(easyui):
    code, out, _ = easyui.run("compose", "--json")
    result = json.loads(out)
    assert code == 0 and result["order"] == list(SLUGS) and result["warnings"] == []
    by = {d["slug"]: d for d in result["dashboards"]}
    assert {s: by[s]["from"] for s in SLUGS} == {s: ("project" if s == "overview" else "fitdata-garmin") for s in SLUGS}
    assert [(s["id"], s["placed"]) for s in by["overview"]["sections"]] == [
        ("garmin-readings", "after:readings"), ("garmin-charts", "after:charts")]


def test_easy_ui_builds_and_validates_the_sample(easyui):
    code, _, err = easyui.run("data", "build")
    assert code == 0, err
    code, out, err = easyui.run("validate", "--json")
    report = json.loads(out)
    assert code == 0 and report["ok"] and report["errors"] == 0 and report["warnings"] == 0, err


def test_easy_ui_validates_the_new_watch_sample(easyui, tmp_path):
    """The pages also check out against an account that has just started (queries, columns, look)."""
    shutil.copyfile(NEW_WATCH, tmp_path / "mes" / "fitdata" / "fitdata.sqlite")
    code, _, err = easyui.run("data", "build")
    assert code == 0, err
    code, out, err = easyui.run("validate", "--json")
    report = json.loads(out)
    assert code == 0 and report["ok"] and report["errors"] == 0 and report["warnings"] == 0, err


def test_easy_ui_composes_into_files(easyui, tmp_path):
    code, _, err = easyui.run("compose", "--out", str(tmp_path / "composed"))
    assert code == 0, err
    import yaml
    files = sorted(p.stem for p in (tmp_path / "composed").glob("*.yaml"))
    assert files == sorted(SLUGS)
    composed = yaml.safe_load((tmp_path / "composed" / "overview.yaml").read_text())
    assert list(composed["queries"]) == list(compose(REPO, "overview")[0]["queries"])
    assert "anchor" not in json.dumps(composed["layout"])

