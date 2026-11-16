"""`fitdata describe`: every command with its kind and arguments, and the MES_ALLOW check.

The JSON has the shape my-easy-software apps produce (`mes.App.describe()`), so `mes` jobs and
`mes gate` can ask fitdata which commands it has and what each may do, without fitdata depending
on mes:

    {"app": "fitdata", "title", "version", "mes_version": null, "allowed", "json", "exit_codes",
     "commands": [{"name", "kind", "help", "builtin", "args": [{"name", "flag", "kind", "type",
                   "required", "default", "many", "choices", "help"}]}],
     "jobs": [...from project.toml...]}

Kinds, in increasing order of power: read (only looks), write (changes data), admin (changes
how fitdata is set up, here: the Garmin login). MES_ALLOW names the highest kind this session may
run: unset means admin (a person at their own computer); set but empty or unknown allows nothing
(fails closed). A command above the allowed kind exits 4 before it starts.
"""
from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path
from typing import Any

from . import __version__
from .errors import EXIT_CODES, NotAllowed

KINDS = ("read", "write", "admin")

COMMAND_KINDS: dict[str, str] = {
    "summary": "read", "days": "read", "activities": "read", "day": "read", "activity": "read",
    "sources": "read", "schema": "read", "sql": "read", "status": "read", "decode": "read",
    "describe": "read", "guide": "read",
    "sync": "write", "activities-sync": "write", "backfill": "write", "account": "write",
    "index": "write", "migrate-storage": "write",
    "login": "admin",
}
BUILTIN = {"describe", "guide", "schema", "sources"}  # the commands every mes app answers

_KIND_WORDS = {"read": "only reads", "write": "changes data", "admin": "changes how fitdata is set up"}
_HOW = 'Set MES_ALLOW to "read", "write" or "admin", or remove it.'

REPO = Path(__file__).resolve().parents[2]
PROJECT_TOML = REPO / "project.toml"


def allowed_level() -> str:
    raw = os.environ.get("MES_ALLOW")
    if raw is None:
        return "admin"
    value = raw.strip().lower()
    if not value:
        raise NotAllowed("MES_ALLOW is set but empty, so fitdata cannot tell what this session may do, "
                         "and nothing is allowed to run.", hint=_HOW)
    if value not in KINDS:
        raise NotAllowed(f"MES_ALLOW is set to {raw[:20]!r}, which fitdata does not recognise, so nothing "
                         "is allowed to run.", hint=_HOW)
    return value


def check_allowed(kind: str, level: str | None = None) -> None:
    level = level or allowed_level()
    if KINDS.index(kind) > KINDS.index(level):
        raise NotAllowed(f'This command {_KIND_WORDS[kind]} (kind "{kind}"), but this session may only run '
                         f'"{level}" commands, so it was not started. This protects your data from changes '
                         "you did not allow.",
                         hint=f'Ask the owner to run it, or to give this session "{kind}" access (MES_ALLOW={kind}).')


def _type_name(action: argparse.Action) -> str:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "bool"
    return {int: "int", float: "float", None: "str", str: "str"}.get(action.type, getattr(action.type, "__name__", "str"))


def _arg(action: argparse.Action) -> dict[str, Any]:
    if not action.option_strings:
        return {"name": action.dest, "flag": None, "kind": "positional", "type": _type_name(action),
                "required": True, "default": None, "many": False, "choices": action.choices and list(action.choices),
                "help": action.help}
    flag = max(action.option_strings, key=len)
    is_flag = isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction))
    return {"name": flag.lstrip("-").replace("-", "_"), "flag": flag, "kind": "flag" if is_flag else "option",
            "type": _type_name(action), "required": bool(action.required), "default": action.default,
            "many": action.nargs in ("*", "+"), "choices": action.choices and list(action.choices),
            "help": action.help}


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return dict(a.choices)
    return {}


def commands(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    out = []
    helps = {}
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in a._choices_actions}
    for name, sp in _subparsers(parser).items():
        args = [_arg(a) for a in sp._actions
                if not isinstance(a, argparse._HelpAction) and a.help != argparse.SUPPRESS]
        out.append({"name": name, "kind": COMMAND_KINDS[name], "help": helps.get(name) or sp.description,
                    "builtin": name in BUILTIN, "args": args})
    return out


def jobs() -> list[dict[str, Any]]:
    """The jobs of project.toml (only present when fitdata runs from its repository)."""
    try:
        data = tomllib.loads(PROJECT_TOML.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for name, j in (data.get("jobs") or {}).items():
        trigger = next((t for t in ("schedule", "after", "manual") if t in j), None)
        out.append({"name": name, "run": j.get("run"), "trigger": trigger, "schedule": j.get("schedule"),
                    "after": j.get("after"), "memory": j.get("memory"), "timeout": j.get("timeout"),
                    "network": j.get("network") or False})
    return out


def describe(parser: argparse.ArgumentParser) -> dict[str, Any]:
    return {
        "app": "fitdata",
        "project": "fitdata",
        "title": "Fitness",
        "version": __version__,
        "mes_version": None,  # fitdata does not use the mes library; it answers in the same shape
        "allowed": allowed_level(),
        "json": "Add --json (before or after the command) to get only JSON data on stdout.",
        "exit_codes": dict(EXIT_CODES),
        "commands": commands(parser),
        "jobs": jobs(),
        "global_options": [
            {"flag": "--json", "help": "machine-readable JSON output"},
            {"flag": "--local", "help": "use this machine's archive even if a remote is configured"},
            {"flag": "--verbose", "help": "more detail in the log of collecting commands"},
        ],
    }


def describe_text(parser: argparse.ArgumentParser) -> str:
    level = allowed_level()
    may = {"read": "only read commands (they only look)",
           "write": "read and write commands (write commands change data)",
           "admin": "read, write and admin commands (admin: the Garmin login)"}[level]
    rows = [(c["name"], c["kind"], (c["help"] or "").splitlines()[0]) for c in commands(parser)]
    w0 = max(len(r[0]) for r in rows)
    lines = [f"Fitness (fitdata), version {__version__}", f"This session may run {may}.", ""]
    lines += [f"{n.ljust(w0)}  {k.ljust(5)}  {h}" for n, k, h in rows]
    lines += ["", "For the arguments of a command, run: fitdata <command> --help "
              "(or fitdata --json describe for everything as data)."]
    return "\n".join(lines)
