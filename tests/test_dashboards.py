"""The fitness dashboards and their synthetic sample data (ui/fixtures/).

The project's Today page is ui/dashboards/overview.yaml, with anchors where fitdata-garmin adds its
sections (apps/fitdata_garmin/dashboards/overview-garmin.yaml); Sleep, Activities and Trends are
fitdata-garmin's own dashboards (apps/fitdata_garmin/dashboards/). Every dashboard reads only the
project's shared views, days and activities.

Most tests need only fitdata: they compose the dashboards as the apps contract says
(tests/compose_dashboards.py) and run every query on the sample with SQLite. The last ones need
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
MAKE_FIXTURE = REPO / "ui" / "fixtures" / "make_fixture.py"
SLUGS = ("overview", "sleep", "activities", "trends")
DAY_CHOICES = (7, 30, 90, 365)
SHARED = ("days", "activities")


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


def test_fixture_is_what_the_generator_and_the_real_indexer_make(tmp_path):
    out = tmp_path / "fresh.sqlite"
    subprocess.run([sys.executable, str(MAKE_FIXTURE), "--out", str(out)], check=True, capture_output=True)
    objects = "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY type, name"
    assert _rows(FIXTURE, objects) == [("table", "activities__rows"), ("table", "days__rows"), ("table", "garmin_files"),
                                       ("view", "activities"), ("view", "days")]
    assert _rows(FIXTURE, objects) == _rows(out, objects)
    for t, order_by in (("days__rows", "date"), ("activities__rows", "id"), ("garmin_files", "path"),
                        ("days", "date"), ("activities", "id")):
        assert _columns(FIXTURE, t) == _columns(out, t), f"columns of {t} differ: run ui/fixtures/make_fixture.py"
        assert _rows(FIXTURE, f'SELECT * FROM "{t}" ORDER BY {order_by}') == _rows(out, f'SELECT * FROM "{t}" ORDER BY {order_by}'), (
            f"ui/fixtures/fitdata-sample.db is out of date ({t}): run uv run python ui/fixtures/make_fixture.py"
        )
    assert _rows(FIXTURE, "SELECT DISTINCT source FROM days__rows") == [("fitdata-garmin",)]


def test_fixture_has_realistic_gaps():
    one = lambda sql: _rows(FIXTURE, sql)[0][0]  # noqa: E731
    assert one("SELECT count(*) FROM days") == 119            # 120 days, one missing from the archive
    assert one("SELECT min(date) || ' ' || max(date) FROM days") == "2026-01-05 2026-05-04"
    assert 25 <= one("SELECT count(*) FROM activities") <= 40
    assert one("SELECT count(*) FROM days WHERE has_data = 0") == 3
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND sleep_score IS NULL") >= 3
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND hrv_last_night_avg IS NULL") >= 3
    assert one("SELECT count(*) FROM days WHERE has_data = 1 AND body_battery_high IS NULL") >= 1
    assert one("SELECT count(*) FROM activities WHERE avg_hr IS NULL") >= 1
    assert one("SELECT count(*) FROM activities WHERE distance_m = 0 OR distance_m IS NULL") >= 1
    assert one("SELECT count(*) FROM days WHERE complete = 0") == 2  # the last two days still settle


def test_the_dashboards_may_read_only_the_shared_views():
    """The allowlist (contract 4.6): the shared views, then [views], then each app's [ui] tables."""
    project = tomllib.loads((REPO / "project.toml").read_text())
    allow = [*project["tables"], *project.get("views", {}), *[t for a in apps(REPO) for t in a["ui"].get("tables", [])]]
    assert allow == list(SHARED) and "tables" not in project["ui"]


# ------------------------------------------------------------------ composition (fitdata only)


def test_where_each_dashboard_comes_from_and_the_menu():
    found = sources(REPO)
    assert {s: f for s, (f, _) in found.items()} == {
        "sleep": "fitdata-garmin", "activities": "fitdata-garmin", "trends": "fitdata-garmin", "overview": "project"}
    assert order(REPO) == list(SLUGS)


def test_the_overview_is_todays_page_once_composed():
    dash, placed, warnings = compose(REPO, "overview")
    assert warnings == [] and placed == [
        {"app": "fitdata-garmin", "id": "garmin-readings", "file": "apps/fitdata_garmin/dashboards/overview-garmin.yaml",
         "placed": "after:readings"},
        {"app": "fitdata-garmin", "id": "garmin-trends", "file": "apps/fitdata_garmin/dashboards/overview-garmin.yaml",
         "placed": "after:trends"}]
    assert list(dash["queries"]) == ["last_day", "garmin_latest", "garmin_daily"]
    assert not any(isinstance(r, dict) for r in dash["layout"])  # the anchors are gone
    shape = [[c["type"] + ":" + str(c.get("title")) for c in row] for row in dash["layout"]]
    assert shape == [
        ["controls:None", "stat:Data up to"],
        ["stat:Steps", "stat:Resting heart rate", "stat:Sleep score", "stat:Overnight HRV"],
        ["stat:Body Battery high", "stat:Training readiness"],
        ["line:Sleep score", "line:Resting heart rate"],
        ["line:Overnight HRV", "bar:Steps per day"],
        ["text:None"]]
    queries = {c.get("query") for row in dash["layout"] for c in row} - {None}
    assert queries == set(dash["queries"])


def test_the_project_overview_alone_shows_only_shared_data():
    """Without the app's sections, the project's page still works: it reads only shared views."""
    raw = compose_dashboards_raw("overview")
    assert [r for r in raw["layout"] if isinstance(r, dict)] == [{"anchor": "readings"}, {"anchor": "trends"}]
    assert list(raw["queries"]) == ["last_day"]


def compose_dashboards_raw(slug: str) -> dict:
    import yaml
    return yaml.safe_load(sources(REPO)[slug][1].read_text())


def test_a_project_override_removes_or_replaces_the_apps_sections(tmp_path):
    copy = tmp_path / "proj"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "*.db"))
    (copy / "ui" / "sections").mkdir()
    (copy / "ui" / "sections" / "fitdata-garmin.overview.yaml").write_text("spec: easy-ui/sections-v1\nsections: []\n")
    dash, placed, _ = compose(copy, "overview")
    assert placed == [] and list(dash["queries"]) == ["last_day"] and len(dash["layout"]) == 1


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


def _run(slug: str, days: int, db: Path = FIXTURE) -> dict:
    dash, _, _ = compose(REPO, slug)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {}
        for name, q in dash["queries"].items():
            cur = con.execute(q["sql"], {"days": days} if ":days" in q["sql"] else {})
            out[name] = {"columns": [d[0] for d in cur.description], "rows": cur.fetchall()}
        return out
    finally:
        con.close()


def test_every_dashboard_reads_only_the_shared_views():
    stored = {n for (n,) in _rows(FIXTURE, "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    for slug in SLUGS:
        dash, _, _ = compose(REPO, slug)
        for name, q in dash["queries"].items():
            words = set(re.findall(r"[a-z_]+", q["sql"].lower()))
            assert words & stored <= set(SHARED), (slug, name)


@pytest.mark.parametrize("days", DAY_CHOICES)
@pytest.mark.parametrize("slug", SLUGS)
def test_every_query_runs_for_every_period(slug, days):
    for name, r in _run(slug, days).items():
        assert r["rows"], f"{slug}.{name} returned no rows for {days} days"


@pytest.mark.parametrize("slug", SLUGS)
def test_every_dashboard_has_the_days_control(slug):
    dash, _, _ = compose(REPO, slug)
    assert list(dash["params"]) == ["days"]
    days = dash["params"]["days"]
    assert days["label"] == "Days" and days["type"] == "choice" and [o["value"] for o in days["options"]] == list(DAY_CHOICES)
    assert dash["layout"][0][0] == {"type": "controls"}
    used = {c.get("query") for row in dash["layout"] for c in row} - {None}
    assert used == set(dash["queries"]), slug  # no query is left unused, none is missing


def _window(days: int) -> tuple[str, str]:
    end = _rows(FIXTURE, "SELECT max(date) FROM days WHERE has_data = 1")[0][0]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    return start, end  # the period is start < date <= end


def _one(r: dict) -> dict:
    return dict(zip(r["columns"], r["rows"][0]))


@pytest.mark.parametrize("days", DAY_CHOICES)
def test_overview_latest_values(days):
    start, end = _window(days)
    row = _one(_run("overview", days)["garmin_latest"])
    # Steps come from the last full day, the other readings from the latest day that has one.
    steps_day, steps = _rows(FIXTURE, "SELECT date, steps FROM days WHERE has_data = 1 AND steps > 0 AND date < ? "
                                      "ORDER BY date DESC LIMIT 1", end)[0]
    before = [r[0] for r in _rows(FIXTURE, "SELECT steps FROM days WHERE has_data = 1 AND steps > 0 AND date < ? "
                                           "AND date >= date(?, ?)", steps_day, steps_day, f"-{days} days")]
    assert row["steps"] == steps
    assert row["steps_change"] == pytest.approx(steps - sum(before) / len(before))
    last_hrv = _rows(FIXTURE, "SELECT hrv_last_night_avg FROM days WHERE hrv_last_night_avg IS NOT NULL "
                              "AND date > ? AND date <= ? ORDER BY date DESC LIMIT 1", start, end)[0][0]
    assert row["hrv"] == last_hrv
    assert all(row[k] is not None for k in row)
    assert _run("overview", days)["last_day"]["rows"] == [(end,)]


def test_overview_daily_leaves_unrecorded_days_empty():
    r = _run("overview", 365)["garmin_daily"]
    days = {row[0]: dict(zip(r["columns"], row)) for row in r["rows"]}
    no_watch = [d for (d,) in _rows(FIXTURE, "SELECT date FROM days WHERE has_data = 0")]
    for d in no_watch:
        assert days[d]["Steps"] is None and days[d]["Resting heart rate"] is None  # not 0
    assert [row[0] for row in r["rows"]] == sorted(days)


@pytest.mark.parametrize("days", DAY_CHOICES)
def test_sleep_averages_skip_missing_nights(days):
    start, end = _window(days)
    r = _run("sleep", days)
    row = _one(r["summary"])
    scores = [s for (s,) in _rows(FIXTURE, "SELECT sleep_score FROM days WHERE has_data = 1 AND sleep_score IS NOT NULL "
                                           "AND date > ? AND date <= ?", start, end)]
    assert row["score"] == pytest.approx(sum(scores) / len(scores))
    assert len(r["last_nights"]["rows"]) >= 1


@pytest.mark.parametrize("days", DAY_CHOICES)
def test_activity_totals_and_weeks_add_up(days):
    start, end = _window(days)
    r = _run("activities", days)
    totals = _one(r["totals"])
    acts = _rows(FIXTURE, "SELECT type, coalesce(distance_m, 0), duration_s FROM activities WHERE date > ? AND date <= ?",
                 start, end)
    assert totals["count"] == len(acts) == len(r["recent"]["rows"])
    assert totals["distance"] == pytest.approx(sum(a[1] for a in acts))
    assert totals["time"] == pytest.approx(sum(a[2] for a in acts))
    weekly = r["weekly"]
    hours = sum(sum(v for v in row[4:]) for row in weekly["rows"])
    assert hours == pytest.approx(sum(a[2] for a in acts) / 3600, abs=0.05 * len(weekly["rows"]) + 0.01)
    weeks = [row[0] for row in weekly["rows"]]
    assert weeks == sorted(weeks) and all(dt.date.fromisoformat(w).weekday() == 0 for w in weeks)
    # No made-up empty weeks before the archive starts, and no change against days never recorded.
    assert weeks[0] >= "2026-01-05"
    if days >= 90:
        assert totals["count_change"] is None and totals["distance_change"] is None


def test_recent_activities_show_missing_values_as_empty():
    r = _run("activities", 365)["recent"]
    rows = [dict(zip(r["columns"], row)) for row in r["rows"]]
    strength = [x for x in rows if x["kind"] == "Strength training"]
    assert strength and all(x["distance"] is None and x["pace"] is None for x in strength)
    assert any(x["avg_hr"] is None for x in rows)
    runs = [x for x in rows if x["kind"] == "Running"]
    assert runs and all(240 < x["pace"] < 480 for x in runs)  # seconds per km


def test_trends_weeks_start_with_the_archive():
    r = _run("trends", 365)
    for name in ("health", "training"):
        weeks = [row[0] for row in r[name]["rows"]]
        assert weeks[0] == "2026-01-05" and weeks[-1] == "2026-05-04"
        assert len(weeks) == len(set(weeks)) == 18


def test_dashboards_cope_with_an_empty_archive(tmp_path):
    """A fresh install: the index exists but has no rows yet. Nothing may fail."""
    empty = tmp_path / "empty.sqlite"
    shutil.copyfile(FIXTURE, empty)
    con = sqlite3.connect(empty)
    con.executescript("DELETE FROM days__rows; DELETE FROM activities__rows; DELETE FROM garmin_files;")
    con.commit()
    con.close()
    for slug in SLUGS:
        for days in DAY_CHOICES:
            for name, r in _run(slug, days, empty).items():
                assert all(v is None or v == 0 for row in r["rows"][:1] for v in row[1:]), (slug, name)


def test_dashboards_show_a_second_apps_days_and_activities(tmp_path):
    """The shared views at work: another app's activity shows on the Activities page, and its
    values fill a day where fitdata-garmin had none, without any change to the dashboards."""
    from schema_script import schema_sql
    two = tmp_path / "two.sqlite"
    shutil.copyfile(FIXTURE, two)
    project = tomllib.loads((REPO / "project.toml").read_text())
    con = sqlite3.connect(two)
    con.executescript(schema_sql(project, ["fitdata-garmin", "fitdata-strava"]))
    end = con.execute("SELECT max(date) FROM days WHERE has_data = 1").fetchone()[0]
    con.execute("INSERT INTO activities__rows (source, id, date, type, duration_s, distance_m, start_local) "
                "VALUES ('fitdata-strava', 1, ?, 'road_biking', 3600, 30000, ? || ' 08:00:00')", (end, end))
    con.commit()
    con.close()
    before = _one(_run("activities", 7)["totals"])
    after = _one(_run("activities", 7, two)["totals"])
    assert after["count"] == before["count"] + 1 and after["distance"] == pytest.approx(before["distance"] + 30000)


def test_health_changes_say_which_way_is_good():
    """A higher resting heart rate must not look like good news: every health delta has better:."""
    for slug in ("overview", "sleep"):
        dash, _, _ = compose(REPO, slug)
        for row in dash["layout"]:
            for c in row:
                if "delta" in c:
                    assert c.get("better") in ("higher", "lower"), (slug, c.get("title"))
    (rhr,) = [c for row in compose(REPO, "overview")[0]["layout"] for c in row if c.get("delta") == "resting_hr_change"]
    assert rhr["better"] == "lower"


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
    assert {s: by[s]["from"] for s in SLUGS} == {"overview": "project", "sleep": "fitdata-garmin",
                                                 "activities": "fitdata-garmin", "trends": "fitdata-garmin"}
    assert [(s["id"], s["placed"]) for s in by["overview"]["sections"]] == [
        ("garmin-readings", "after:readings"), ("garmin-trends", "after:trends")]


def test_easy_ui_builds_and_validates_the_sample(easyui):
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

