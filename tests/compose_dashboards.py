"""The project's dashboards as easy-ui composes them (apps contract, section 6), for the tests.

my-easy-ui does the real composition (`easy-ui compose`); this is a small, strict copy of the
rules this project uses, so the dashboards can be checked against the sample data without it:

1. Dashboards: each app's [ui] dashboards (apps/<package>/dashboards/<slug>.yaml), then the
   project's ui/dashboards/*.yaml; a project file replaces an app's dashboard of the same slug.
2. Each app's [ui.contributes.<slug>] sections (or the project's ui/sections/<app>.<slug>.yaml)
   are added to that dashboard: their queries (names must start with the app's table prefix)
   and their rows at start, at end, or right after an {anchor: name} item.
3. The anchors are removed; the result is a plain easy-ui/v1 dashboard.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import yaml


class ComposeError(Exception):
    pass


def _yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ComposeError(f"{path} is not a mapping")
    return data


def apps(repo: Path) -> list[dict]:
    """Each app: package, name, prefix, folder and its [ui] table, in [project] apps order."""
    project = tomllib.loads((repo / "project.toml").read_text())
    out = []
    for package in project["project"]["apps"]:
        folder = repo / "apps" / package
        app = tomllib.loads((folder / "app.toml").read_text())
        name = app["app"]["name"]
        short = name[len(project["project"]["name"]) + 1:] if name.startswith(project["project"]["name"] + "-") else name
        out.append({"package": package, "name": name, "prefix": short.replace("-", "_") + "_", "folder": folder,
                    "ui": app.get("ui") or {}})
    return out


def sources(repo: Path) -> dict[str, tuple[str, Path]]:
    """slug -> (where it comes from: "project" or the app's name, file)."""
    found: dict[str, tuple[str, Path]] = {}
    for app in apps(repo):
        for slug in app["ui"].get("dashboards", []):
            if slug in found:
                raise ComposeError(f"The apps {found[slug][0]} and {app['name']} both have a dashboard called {slug}.")
            found[slug] = (app["name"], app["folder"] / "dashboards" / f"{slug}.yaml")
    for path in sorted((repo / "ui" / "dashboards").glob("*.yaml")):
        found[path.stem] = ("project", path)
    return found


def order(repo: Path) -> list[str]:
    menu = tomllib.loads((repo / "project.toml").read_text()).get("ui", {}).get("dashboards", [])
    return [*menu, *[s for s in sources(repo) if s not in menu]]


def compose(repo: Path, slug: str) -> tuple[dict, list[dict], list[str]]:
    """(the composed dashboard, where each section was placed, warnings)."""
    _, path = sources(repo)[slug]
    dash = _yaml(path)
    queries = dict(dash.get("queries") or {})
    layout = list(dash.get("layout") or [])
    placed, warnings = [], []
    starts, ends, after = [], [], {}
    for app in apps(repo):
        c = (app["ui"].get("contributes") or {}).get(slug)
        override = repo / "ui" / "sections" / f"{app['name']}.{slug}.yaml"
        if c is None and not override.exists():
            continue
        file = override if override.exists() else app["folder"] / c["sections"]
        doc = _yaml(file)
        if doc.get("spec") != "easy-ui/sections-v1" or set(doc) != {"spec", "sections"}:
            raise ComposeError(f"{file}: not an easy-ui/sections-v1 file")
        for i, s in enumerate(doc["sections"] or []):
            if set(s) - {"id", "position", "queries", "layout"} or "params" in s:
                raise ComposeError(f"{file}, sections[{i}]: unknown keys")
            for q, body in (s.get("queries") or {}).items():
                if not q.startswith(app["prefix"]):
                    raise ComposeError(f"{file}, sections[{i}]: query {q} does not start with {app['prefix']}")
                if q in queries:
                    raise ComposeError(f"{file}, sections[{i}]: query {q} is already on the dashboard")
                queries[q] = body
            pos, rows = s["position"], list(s["layout"])
            if pos == "start":
                starts += rows
            elif pos == "end":
                ends += rows
            elif pos.startswith("after:"):
                anchor = pos[len("after:"):]
                if not any(isinstance(r, dict) and r.get("anchor") == anchor for r in layout):
                    warnings.append(f"{file}: no anchor {anchor} on {slug}; the section goes to the end")
                    ends += rows
                    pos = "end"
                else:
                    after.setdefault(anchor, []).extend(rows)
            else:
                raise ComposeError(f"{file}, sections[{i}]: position {pos!r}")
            placed.append({"app": app["name"], "id": s["id"], "file": str(file.relative_to(repo)), "placed": pos})
    out = list(starts)
    for row in layout:
        if isinstance(row, dict) and "anchor" in row:
            out += after.get(row["anchor"], [])
        else:
            out.append(row)
    out += ends
    ids = [p["id"] for p in placed]
    if len(ids) != len(set(ids)):
        raise ComposeError(f"{slug}: two sections share an id")
    return {**dash, "queries": queries, "layout": out}, placed, warnings
