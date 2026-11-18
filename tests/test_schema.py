"""apps/fitdata_garmin/schema.sql is the exact script `mes db schema --sql` prints for this
project.toml (apps contract, section 4.3). fitdata does not depend on mes, so the copy is checked
against tests/schema_script.py (which reproduces the contract's own fitlab example to the byte)
and, when my-easy-software is available, against mes itself."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest

from schema_script import for_repo, schema_sql

REPO = Path(__file__).resolve().parents[1]
SCHEMA = REPO / "apps" / "fitdata_garmin" / "schema.sql"
REGENERATE = "uv run python tests/schema_script.py > apps/fitdata_garmin/schema.sql"

# Section 9.1 (the fitlab example's tables and views) and 9.6 (the script mes must print for it).
FITLAB = '''
[project]
name = "fitlab"
apps = ["fitlab_walks", "fitlab_rides"]

[tables.activities]
key = ["id"]
columns = { id = "text", date = "text", type = "text", distance_m = "real", duration_s = "real", ascent_m = "real" }

[tables.days]
key = ["date"]
prefer = ["fitlab-rides"]
[tables.days.columns]
date = "text"
walk_km = "real"
ride_km = "real"
weather = "text"

[views.weekly]
sql = """
SELECT strftime('%Y-%W', date) AS week, type, count(*) AS activities, round(sum(distance_m) / 1000.0, 1) AS km
FROM activities
GROUP BY 1, 2
"""
'''
FITLAB_SQL = '''CREATE TABLE IF NOT EXISTS "activities__rows" (
  "source" TEXT NOT NULL,
  "id" TEXT NOT NULL,
  "date" TEXT,
  "type" TEXT,
  "distance_m" REAL,
  "duration_s" REAL,
  "ascent_m" REAL,
  PRIMARY KEY ("source", "id")
);
CREATE INDEX IF NOT EXISTS "activities__rows_key" ON "activities__rows" ("id");
CREATE TABLE IF NOT EXISTS "days__rows" (
  "source" TEXT NOT NULL,
  "date" TEXT NOT NULL,
  "walk_km" REAL,
  "ride_km" REAL,
  "weather" TEXT,
  PRIMARY KEY ("source", "date")
);
CREATE INDEX IF NOT EXISTS "days__rows_key" ON "days__rows" ("date");
DROP VIEW IF EXISTS "activities";
CREATE VIEW "activities" AS
SELECT "source", "id", "date", "type", "distance_m", "duration_s", "ascent_m"
FROM "activities__rows"
WHERE "source" IN ('fitlab-walks', 'fitlab-rides');
DROP VIEW IF EXISTS "days";
CREATE VIEW "days" AS
SELECT
  (SELECT r."source" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "source",
  k."date" AS "date",
  (SELECT r."walk_km" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') AND r."walk_km" IS NOT NULL ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "walk_km",
  (SELECT r."ride_km" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') AND r."ride_km" IS NOT NULL ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "ride_km",
  (SELECT r."weather" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') AND r."weather" IS NOT NULL ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "weather"
FROM (SELECT DISTINCT "date" FROM "days__rows" WHERE "source" IN ('fitlab-walks', 'fitlab-rides')) k;
DROP VIEW IF EXISTS "weekly";
CREATE VIEW "weekly" AS
SELECT strftime('%Y-%W', date) AS week, type, count(*) AS activities, round(sum(distance_m) / 1000.0, 1) AS km
FROM activities
GROUP BY 1, 2;
'''


def test_the_generator_prints_the_contracts_fitlab_script_exactly():
    assert schema_sql(tomllib.loads(FITLAB), ["fitlab-walks", "fitlab-rides"]) == FITLAB_SQL


def test_the_fitlab_script_merges_as_the_contract_says():
    """Contract 9.7 step 4: one day row, walk_km 4.2, ride_km 21.5, weather and source from rides."""
    db = sqlite3.connect(":memory:")
    db.executescript(FITLAB_SQL)
    db.execute("INSERT INTO days__rows VALUES ('fitlab-walks', '2026-01-05', 4.2, NULL, 'sunny')")
    db.execute("INSERT INTO days__rows VALUES ('fitlab-rides', '2026-01-05', NULL, 21.5, 'cloudy')")
    db.execute("INSERT INTO days__rows VALUES ('gone-app', '2026-01-06', 1, 1, 'x')")  # not installed: left out
    assert db.execute("SELECT * FROM days").fetchall() == [("fitlab-rides", "2026-01-05", 4.2, 21.5, "cloudy")]


def test_schema_sql_is_the_script_for_this_project_toml():
    assert SCHEMA.read_text() == for_repo(REPO), f"apps/fitdata_garmin/schema.sql is out of date. Run: {REGENERATE}"


def test_schema_sql_has_the_contracts_form():
    text = SCHEMA.read_text()
    assert text.endswith(";\n") and "\n\n" not in text and "--" not in text and "BEGIN" not in text
    db = sqlite3.connect(":memory:")
    db.executescript(text)
    db.executescript(text)  # runs again on its own result (every index run does this)
    kinds = dict(db.execute("SELECT name, type FROM sqlite_master"))
    assert kinds["days"] == kinds["activities"] == "view" and kinds["days__rows"] == kinds["activities__rows"] == "table"


def test_project_toml_columns_are_the_indexers():
    """The fixed column lists are what the index writes: the day and activity headlines of
    sync.py, the few columns index.py adds (contract 8.2), then what extract.py reads from the
    processed files (new columns go at the end, where `mes db sync` adds them)."""
    from fitdata_garmin import extract, sync
    project = tomllib.loads((REPO / "project.toml").read_text())
    days = list(project["tables"]["days"]["columns"])
    assert days == ["date", "has_data", "complete", "fetched_at", *sync._day_headline(Path("/nonexistent")),
                    "activity_count", "dir", *extract.DAY_COLUMNS]
    found = set(extract.day("2026-01-01", {})["days"])
    assert found == set(extract.DAY_COLUMNS) | set(extract.HEADLINE_FROM_FILES)
    assert set(extract.HEADLINE_FROM_FILES) <= set(sync._day_headline(Path("/nonexistent")))
    acts = list(project["tables"]["activities"]["columns"])
    headline = [k for k in sync._activity_headline({}) if k not in ("id", "name", "type", "start_local")]
    assert acts == ["id", "date", "type", "name", "start_local", "dir", "fit", *headline, "fetched_at", "complete",
                    "records"]


def _mes() -> list[str] | None:
    """How to run mes: MES_REWORK_DIR (a my-easy-software checkout), else `mes`."""
    rework = os.environ.get("MES_REWORK_DIR")
    if rework and shutil.which("uv"):
        return ["uv", "run", "--quiet", "--project", rework, "mes"]
    return [m] if (m := shutil.which("mes")) else None


def test_schema_sql_matches_mes_itself():
    mes = _mes()
    if mes is None:
        pytest.skip("my-easy-software is not available (set MES_REWORK_DIR to its checkout to compare with it)")
    r = subprocess.run([*mes, "db", "schema", "--sql", "--project-dir", str(REPO)], capture_output=True, text=True,
                       timeout=120, env={**os.environ, "MES_ALLOW": "read"})
    if r.returncode != 0:
        pytest.skip(f"this mes cannot print the schema yet: {(r.stderr or r.stdout).strip()[:300]}")
    assert r.stdout == SCHEMA.read_text(), f"schema.sql differs from mes db schema --sql. Run: {REGENERATE}"
