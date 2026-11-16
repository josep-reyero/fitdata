"""The fitness dashboards (ui/dashboards/*.yaml) and their synthetic sample data (ui/fixtures/).

The first tests need only fitdata: they check that the sample has exactly the columns the real
index has. The rest need my-easy-ui (import package `easyui`) and are skipped when it is not
installed. To run everything:

    uv run --with-editable <path to my-easy-ui> python -m pytest tests/test_dashboards.py

(`python -m pytest`, so pytest runs in the environment that has my-easy-ui added.)
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "ui" / "fixtures" / "fitdata-sample.db"
MAKE_FIXTURE = REPO / "ui" / "fixtures" / "make_fixture.py"
DASHBOARDS = REPO / "ui" / "dashboards"
SLUGS = ("activities", "overview", "sleep", "trends")
DAY_CHOICES = (7, 30, 90, 365)


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
    sync = pytest.importorskip("fitdata.sync")
    mf = _make_fixture_module()
    assert tuple(sync._day_headline(tmp_path / "nothing")) == mf.DAY_HEADLINE_KEYS
    assert tuple(sync._activity_headline({})) == mf.ACTIVITY_HEADLINE_KEYS


def test_fixture_is_what_the_generator_and_the_real_indexer_make(tmp_path):
    out = tmp_path / "fresh.sqlite"
    subprocess.run([sys.executable, str(MAKE_FIXTURE), "--out", str(out)], check=True, capture_output=True)
    tables = ("days", "activities", "files")
    assert [r[0] for r in _rows(FIXTURE, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")] == sorted(tables)
    for t in tables:
        assert _columns(FIXTURE, t) == _columns(out, t), f"columns of {t} differ: run ui/fixtures/make_fixture.py"
        order = "path" if t == "files" else ("date" if t == "days" else "id")
        assert _rows(FIXTURE, f'SELECT * FROM "{t}" ORDER BY {order}') == _rows(out, f'SELECT * FROM "{t}" ORDER BY {order}'), (
            f"ui/fixtures/fitdata-sample.db is out of date ({t}): run uv run python ui/fixtures/make_fixture.py"
        )


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


def test_project_toml_allows_only_days_and_activities():
    project = tomllib.loads((REPO / "project.toml").read_text())
    assert project["project"]["name"] == "fitdata" and project["project"]["title"] == "Fitness"
    assert project["ui"]["data"] == "fitdata.sqlite"
    assert project["ui"]["tables"] == ["days", "activities"]


# ------------------------------------------------------------------ dashboards (needs my-easy-ui)


@pytest.fixture
def easyui(tmp_path, monkeypatch):
    """my-easy-ui with the sample data built as the dashboard data, all in a temporary folder."""
    pytest.importorskip("easyui", reason="my-easy-ui is not installed, so the dashboards were not checked. Run: uv run --with-editable <path to my-easy-ui> python -m pytest tests/test_dashboards.py")
    from easyui import appconfig, bundle, cli, spec

    monkeypatch.setenv("MES_DATA_HOME", str(tmp_path / "mes"))
    monkeypatch.setenv("EASYUI_STATE_HOME", str(tmp_path / "state"))
    return type("EasyUI", (), {"appconfig": appconfig, "bundle": bundle, "cli": cli, "spec": spec, "tmp": tmp_path})


def _build(eu, source: Path = FIXTURE):
    data = eu.tmp / "mes" / "fitdata"
    data.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, data / "fitdata.sqlite")
    cfg = eu.appconfig.load(REPO)
    eu.bundle.build(cfg)
    return cfg, eu.bundle.require_current(cfg)


@pytest.fixture
def built(easyui):
    cfg, current = _build(easyui)
    return easyui, cfg, current


def _run(eu, current: Path, slug: str, days: int) -> dict:
    dash = eu.spec.load_file(DASHBOARDS / f"{slug}.yaml")
    return eu.spec.run_queries(dash, list(dash.queries), {"days": days}, bundle_path=current)["results"]


def test_every_dashboard_is_valid_against_the_sample(built):
    eu, cfg, _ = built
    report = eu.spec.validate_app(cfg)
    problems = [p for d in report["dashboards"] for p in d["errors"] + d["warnings"]]
    assert problems == []
    assert report["ok"] and report["data"]["checked"]
    assert sorted(d["slug"] for d in report["dashboards"]) == list(SLUGS)


def test_cli_validate(built, capsys):
    eu, _, _ = built
    code = eu.cli.run(["validate", "--json", "--app-dir", str(REPO)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] and out["errors"] == 0 and out["warnings"] == 0


def test_only_allowlisted_tables_reach_the_dashboards(built):
    eu, _, current = built
    assert {t["name"] for t in eu.bundle.describe_tables(current, samples=0)} == {"days", "activities"}


@pytest.mark.parametrize("slug", SLUGS)
def test_compiled_dashboards_have_the_days_control_and_no_sql(built, slug):
    eu, _, _ = built
    compiled = eu.spec.compile_dashboard(eu.spec.load_file(DASHBOARDS / f"{slug}.yaml"))
    text = json.dumps(compiled).lower()
    assert "select " not in text and '"sql"' not in text
    (days,) = compiled["params"]
    assert days["name"] == "days" and days["label"] == "Days" and days["type"] == "choice"
    assert [o["value"] for o in days["options"]] == list(DAY_CHOICES)
    assert compiled["layout"][0][0] == {"id": "r0c0", "type": "controls", "title": None, "params": ["days"]}


@pytest.mark.parametrize("days", DAY_CHOICES)
@pytest.mark.parametrize("slug", SLUGS)
def test_every_query_runs_for_every_period(built, slug, days):
    eu, _, current = built
    for name, r in _run(eu, current, slug, days).items():
        assert not r["truncated"], name
        assert r["rows"], f"{slug}.{name} returned no rows for {days} days"


def _window(days: int) -> tuple[str, str]:
    end = _rows(FIXTURE, "SELECT max(date) FROM days WHERE has_data = 1")[0][0]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    return start, end  # the period is start < date <= end


@pytest.mark.parametrize("days", DAY_CHOICES)
def test_overview_latest_values(built, days):
    eu, _, current = built
    start, end = _window(days)
    row = dict(zip(*(lambda r: (r["columns"], r["rows"][0]))(_run(eu, current, "overview", days)["latest"])))
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


def test_overview_daily_leaves_unrecorded_days_empty(built):
    eu, _, current = built
    r = _run(eu, current, "overview", 365)["daily"]
    days = {row[0]: dict(zip(r["columns"], row)) for row in r["rows"]}
    no_watch = [d for (d,) in _rows(FIXTURE, "SELECT date FROM days WHERE has_data = 0")]
    for d in no_watch:
        assert days[d]["Steps"] is None and days[d]["Resting heart rate"] is None  # not 0
    assert [row[0] for row in r["rows"]] == sorted(days)


@pytest.mark.parametrize("days", DAY_CHOICES)
def test_sleep_averages_skip_missing_nights(built, days):
    eu, _, current = built
    start, end = _window(days)
    r = _run(eu, current, "sleep", days)
    row = dict(zip(r["summary"]["columns"], r["summary"]["rows"][0]))
    scores = [s for (s,) in _rows(FIXTURE, "SELECT sleep_score FROM days WHERE has_data = 1 AND sleep_score IS NOT NULL "
                                           "AND date > ? AND date <= ?", start, end)]
    assert row["score"] == pytest.approx(sum(scores) / len(scores))
    assert len(r["last_nights"]["rows"]) >= 1


@pytest.mark.parametrize("days", DAY_CHOICES)
def test_activity_totals_and_weeks_add_up(built, days):
    eu, _, current = built
    start, end = _window(days)
    r = _run(eu, current, "activities", days)
    totals = dict(zip(r["totals"]["columns"], r["totals"]["rows"][0]))
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


def test_recent_activities_show_missing_values_as_empty(built):
    eu, _, current = built
    r = _run(eu, current, "activities", 365)["recent"]
    rows = [dict(zip(r["columns"], row)) for row in r["rows"]]
    strength = [x for x in rows if x["kind"] == "Strength training"]
    assert strength and all(x["distance"] is None and x["pace"] is None for x in strength)
    assert any(x["avg_hr"] is None for x in rows)
    runs = [x for x in rows if x["kind"] == "Running"]
    assert runs and all(240 < x["pace"] < 480 for x in runs)  # seconds per km


def test_trends_weeks_start_with_the_archive(built):
    eu, _, current = built
    r = _run(eu, current, "trends", 365)
    for name in ("health", "training"):
        weeks = [row[0] for row in r[name]["rows"]]
        assert weeks[0] == "2026-01-05" and weeks[-1] == "2026-05-04"
        assert len(weeks) == len(set(weeks)) == 18


def test_dashboards_cope_with_an_empty_archive(easyui, tmp_path):
    """A fresh install: the index exists but has no rows yet. Nothing may fail."""
    empty = tmp_path / "empty.sqlite"
    shutil.copyfile(FIXTURE, empty)
    con = sqlite3.connect(empty)
    con.executescript("DELETE FROM days; DELETE FROM activities; DELETE FROM files;")
    con.commit()
    con.close()
    _, current = _build(easyui, empty)
    for slug in SLUGS:
        for days in DAY_CHOICES:
            for name, r in _run(easyui, current, slug, days).items():
                assert all(v is None or v == 0 for row in r["rows"][:1] for v in row[1:]), (slug, name)


def test_menu_order_names_every_dashboard_once():
    project = tomllib.loads((REPO / "project.toml").read_text())
    order = project["ui"]["dashboards"]
    files = sorted(p.stem for p in (REPO / "ui" / "dashboards").glob("*.yaml"))
    assert order[0] == "overview" and sorted(order) == files


def test_health_changes_say_which_way_is_good():
    """A higher resting heart rate must not look like good news: every health delta has better:."""
    for name in ("overview", "sleep"):
        text = (REPO / "ui" / "dashboards" / f"{name}.yaml").read_text()
        assert text.count("      delta: ") == text.count("      better: "), name
    assert "delta: resting_hr_change\n      better: lower" in (REPO / "ui" / "dashboards" / "overview.yaml").read_text()
