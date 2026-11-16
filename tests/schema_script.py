"""The shared database's schema script, made from a project.toml exactly as `mes db schema --sql`
makes it (apps contract, section 4.3). fitdata does not depend on mes, so the app ships a copy of
this text (apps/fitdata_garmin/schema.sql); tests/test_schema.py checks the copy against this
generator and, when the my-easy-software rework is installed, against mes itself.

To regenerate the copy after changing [tables] or [views] in project.toml, from the repository:

    uv run python tests/schema_script.py > apps/fitdata_garmin/schema.sql

(or, with mes installed: mes db schema --sql > apps/fitdata_garmin/schema.sql). Only for tests and
that one command: nothing at runtime imports this file.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "boolean": "INTEGER", "json": "TEXT"}


def _q(name: str) -> str:
    return '"' + name + '"'


def _lit(text: str) -> str:
    return "'" + text + "'"


def schema_sql(project: dict, app_names: list[str]) -> str:
    """`app_names`: the installed apps' names, in [project] apps order."""
    tables: dict = project.get("tables") or {}
    views: dict = project.get("views") or {}
    apps = ", ".join(_lit(a) for a in app_names)
    out: list[str] = []
    for name, t in tables.items():
        cols: dict = t["columns"]
        key: list[str] = t["key"]
        lines = ['  "source" TEXT NOT NULL,']
        lines += [f"  {_q(k)} {SQL_TYPES[cols[k]]} NOT NULL," for k in key]
        lines += [f"  {_q(c)} {SQL_TYPES[ty]}," for c, ty in cols.items() if c not in key]
        lines.append(f"  PRIMARY KEY ({', '.join(_q(c) for c in ['source', *key])})")
        out.append(f"CREATE TABLE IF NOT EXISTS {_q(name + '__rows')} (\n" + "\n".join(lines) + "\n);")
        out.append(f"CREATE INDEX IF NOT EXISTS {_q(name + '__rows_key')} ON {_q(name + '__rows')} "
                   f"({', '.join(_q(k) for k in key)});")
    for name, t in tables.items():
        cols, key, prefer = t["columns"], t["key"], t.get("prefer")
        rows = _q(name + "__rows")
        others = [c for c in cols if c not in key]
        out.append(f"DROP VIEW IF EXISTS {_q(name)};")
        if not prefer:
            select = ", ".join(_q(c) for c in ["source", *key, *others])
            out.append(f"CREATE VIEW {_q(name)} AS\nSELECT {select}\nFROM {rows}\nWHERE \"source\" IN ({apps});")
            continue
        ranked = [*prefer, *[a for a in app_names if a not in prefer]]
        rank = "CASE r.\"source\" " + " ".join(f"WHEN {_lit(a)} THEN {i}" for i, a in enumerate(ranked)) + " END"
        match = " AND ".join(f"r.{_q(k)} = k.{_q(k)}" for k in key)

        def pick(c: str, not_null: bool) -> str:
            cond = f" AND r.{_q(c)} IS NOT NULL" if not_null else ""
            return (f"  (SELECT r.{_q(c)} FROM {rows} r WHERE {match} AND r.\"source\" IN ({apps}){cond} "
                    f"ORDER BY {rank} LIMIT 1) AS {_q(c)}")

        lines = [pick("source", False), *[f"  k.{_q(k)} AS {_q(k)}" for k in key], *[pick(c, True) for c in others]]
        out.append(f"CREATE VIEW {_q(name)} AS\nSELECT\n" + ",\n".join(lines) + "\n"
                   f"FROM (SELECT DISTINCT {', '.join(_q(k) for k in key)} FROM {rows} WHERE \"source\" IN ({apps})) k;")
    for name, v in views.items():
        out.append(f"DROP VIEW IF EXISTS {_q(name)};")
        out.append(f"CREATE VIEW {_q(name)} AS\n{v['sql'].strip()};")
    return "\n".join(out) + "\n"


def app_names(repo: Path, project: dict) -> list[str]:
    """The [app] name of each package in [project] apps, read from apps/<package>/app.toml."""
    return [tomllib.loads((repo / "apps" / p / "app.toml").read_text())["app"]["name"] for p in project["project"]["apps"]]


def for_repo(repo: Path) -> str:
    project = tomllib.loads((repo / "project.toml").read_text())
    return schema_sql(project, app_names(repo, project))


if __name__ == "__main__":
    sys.stdout.write(for_repo(Path(__file__).resolve().parents[1]))
