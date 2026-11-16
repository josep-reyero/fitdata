"""fitdata: a personal Garmin archive, organised per day, with a CLI for people and AI agents.

Reading (add --json for machine-readable output):
  fitdata summary [--days 7]                    recent days' key numbers + activities
  fitdata days [--from D] [--to D] [--days N] [--fields a,b]
  fitdata activities [--from D] [--to D] [--type trail] [--limit N]
  fitdata day YYYY-MM-DD [--source sleep]       a day's overview, or one source's full data
  fitdata activity ID [--source splits] [--records [--every 5] [--columns a,b]]
  fitdata sources                               what each source contains
  fitdata schema                                index tables and columns
  fitdata sql "SELECT ..." [--limit 1000]       read-only SQL on the index
  fitdata status                                archive size, last sync, login
  fitdata describe                              every command, its kind and arguments

Collecting (on the machine that holds the archive):
  fitdata login                                 sign in to Garmin (asks in the terminal)
  fitdata sync [--days 3]                       refresh recent days and activities
  fitdata activities-sync [--days 2]            just check for new activities (cheap)
  fitdata backfill [--max-days 60]              fill in history, newest first
  fitdata account | index
  fitdata migrate-storage --to bucket|files     move the archive between files and a bucket
  fitdata decode FILE.fit [--csv]               decode any FIT file

With [remote] host set in the config, every command except `decode`, `describe` and `guide` runs
on that machine over SSH (use --local to force this machine). Config: ~/.config/fitdata/config.toml
(see README). MES_ALLOW=read|write|admin limits which commands may run (see `fitdata describe`).

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4 not allowed, 5 refused, 6 needs a person
(for example `fitdata login`), 75 temporary (Garmin's rate limit, another sync running).
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from typing import Any

from . import describe as desc
from .config import load
from .errors import FitdataError, NeedsHuman, Refused
from .validate import BadInput, check_date, check_remote_command, check_remote_host

COLLECT = {"login", "sync", "activities-sync", "backfill", "account", "index", "migrate-storage"}
# Commands about the CLI itself: the same on every machine, so never forwarded.
LOCAL_ONLY = {"decode", "describe", "guide"}


class _Parser(argparse.ArgumentParser):
    """Usage errors become BadInput (exit 2), so they follow --json like every other error."""

    def error(self, message: str):
        raise BadInput(f"fitdata did not understand the command line: {message}. Run `{self.prog} --help` "
                       "to see what it accepts.")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="fitdata", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--local", action="store_true", help="use this machine's archive even if a remote is configured")
    p.add_argument("-v", "--verbose", action="store_true", help="more detail in the log")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    FROM, TO = "first day, YYYY-MM-DD", "last day, YYYY-MM-DD (default: today)"

    s = sub.add_parser("summary", help="recent days and activities")
    s.add_argument("--days", type=int, default=7, help="how many days back, ending today")
    s = sub.add_parser("days", help="one row of key numbers per day")
    s.add_argument("--from", dest="frm", help=FROM); s.add_argument("--to", help=TO)
    s.add_argument("--days", type=int, help="how many days back (default 7)")
    s.add_argument("--fields", help="comma-separated columns (see `fitdata schema`)")
    s = sub.add_parser("activities", help="list activities")
    s.add_argument("--from", dest="frm", help=FROM); s.add_argument("--to", help=TO)
    s.add_argument("--days", type=int, help="how many days back (default: about 10 years)")
    s.add_argument("--type", dest="type_", help="part of the activity type, for example trail")
    s.add_argument("--limit", type=int, help="most activities to list, newest first")
    s = sub.add_parser("day", help="one day")
    s.add_argument("date", help="the day, YYYY-MM-DD")
    s.add_argument("--source", help="one source's full data (see `fitdata sources`)")
    s = sub.add_parser("activity", help="one activity")
    s.add_argument("id", help="the activity id (numbers only)")
    s.add_argument("--source", help="one source's full data (see `fitdata sources`)")
    s.add_argument("--records", action="store_true", help="per-sample records (CSV-derived)")
    s.add_argument("--every", type=int, default=1, help="keep every Nth record")
    s.add_argument("--columns", help="comma-separated record columns to keep")
    sub.add_parser("sources", help="what each source contains")
    sub.add_parser("schema", help="index tables and columns")
    s = sub.add_parser("sql", help="read-only SQL on the index")
    s.add_argument("query", help="one SELECT on the days, activities or files table")
    s.add_argument("--limit", type=int, default=1000, help="most rows to return (default 1000)")
    sub.add_parser("status", help="archive overview")
    sub.add_parser("describe", help="every command, its kind (read, write, admin) and arguments")
    sub.add_parser("guide", help="how an AI agent should use fitdata (AGENTS.md)")

    sub.add_parser("login", help="sign in to Garmin")
    s = sub.add_parser("sync", help="refresh recent days and activities")
    s.add_argument("--days", type=int, default=3, help="how many recent days to refresh")
    s.add_argument("--force", action="store_true", help="fetch again even days and activities already complete")
    s = sub.add_parser("activities-sync", help="check for new activities")
    s.add_argument("--days", type=int, default=2, help="how many recent days to check")
    s = sub.add_parser("backfill", help="fill in history")
    s.add_argument("--max-days", type=int, default=60, help="most days to fetch in this run")
    s.add_argument("--until", help="oldest date to reach")
    sub.add_parser("account", help="refresh profile, devices, zones, gear")
    sub.add_parser("index", help="rebuild the SQLite index")
    s = sub.add_parser("migrate-storage", help="move the archive between plain files and a bucket (copies, "
                       "checks every object, then switches; never deletes the old copy)")
    s.add_argument("--to", required=True, choices=["bucket", "files"], help="where the archive should live")
    s = sub.add_parser("decode", help="decode a FIT file")
    s.add_argument("file", help="path of a FIT file on this machine")
    s.add_argument("--csv", action="store_true", help="write one CSV per message type into the current folder "
                   "(needs write access)")
    # Global flags are accepted after the command too (`fitdata days --json`).
    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--local", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


# ------------------------------------------------------------------ output

def _print(data: Any, as_json: bool) -> None:
    if as_json or not isinstance(data, list) or not data or not isinstance(data[0], dict):
        json.dump(data, sys.stdout, indent=None if as_json else 1, ensure_ascii=False, default=str)
        print()
        return
    cols = list(data[0])
    width = {c: min(28, max(len(str(c)), *(len(_fmt(r.get(c))) for r in data))) for c in cols}
    print("  ".join(str(c)[: width[c]].ljust(width[c]) for c in cols))
    for r in data:
        print("  ".join(_fmt(r.get(c))[: width[c]].ljust(width[c]) for c in cols))


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


# ------------------------------------------------------------------ remote

# ssh hands the remote side one command string, which that machine's login shell parses. Quoting
# rules differ between shells (fish treats backslashes inside '...' differently from sh), so the
# arguments travel as one base64url word, which no shell interprets, and are unpacked by fitdata.
FORWARDED = "--forwarded-argv"


def _encode_argv(argv: list[str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(argv).encode()).decode().rstrip("=")


def _decode_argv(token: str) -> list[str]:
    try:
        if not token or set(token) - set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"):
            raise ValueError
        argv = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError
        return argv
    except ValueError:
        raise BadInput("This machine received a fitdata request over SSH that it could not read. "
                       "Make sure both machines run the same version of fitdata, then try again.") from None


def _remote_command(argv: list[str], command: str) -> str:
    return f"{check_remote_command(command)} {FORWARDED} {_encode_argv(argv)}"


def _forward(argv: list[str], host: str, command: str, interactive: bool) -> int:
    remote = _remote_command(argv, command)
    # "--" ends ssh's own options, so the host can never be read as one.
    ssh = ["ssh", *(["-t"] if interactive else []), "--", check_remote_host(host), remote]
    return subprocess.call(ssh)


# ------------------------------------------------------------------ main

def _setup_logging(verbose: bool) -> None:
    from . import paths
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log = logging.getLogger("fitdata")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in (logging.FileHandler(paths.LOGS / "fitdata.log"), logging.StreamHandler(sys.stderr)):
        h.setFormatter(fmt)
        log.addHandler(h)


@contextmanager
def _lock():
    from . import paths
    from .safefs import hold_lock
    with hold_lock(paths.LOCK):
        yield




def _report(message: str, argv: list[str], kind: str, exit_code: int = 2, hint: str | None = None) -> None:
    if "--json" in argv:
        body = {"error": message, "kind": kind, "exit_code": exit_code}
        if hint:
            body["hint"] = hint
        json.dump(body, sys.stderr, ensure_ascii=False)
        print(file=sys.stderr)
    else:
        print(f"fitdata: {message}" + (f"\n  {hint}" if hint else ""), file=sys.stderr)


def _log_to_file(message: str) -> None:
    """Keep a collecting command's failure in logs/fitdata.log too (stderr gets it from _report)."""
    log = logging.getLogger("fitdata")
    record = log.makeRecord(log.name, logging.WARNING, __file__, 0, message, (), None)
    for h in log.handlers:
        if isinstance(h, logging.FileHandler):
            h.handle(record)


def main(argv: list[str] | None = None) -> int:
    # Everything fitdata creates (archive files, index, logs, state) is for its owner only.
    os.umask(0o077)
    argv = sys.argv[1:] if argv is None else argv
    forwarded = False
    try:
        if argv[:1] == [FORWARDED]:  # a request forwarded from another machine (see _forward)
            argv = ["--local", *_decode_argv(argv[1] if len(argv) == 2 else "")]
            forwarded = True
        return _main(argv, forwarded=forwarded)
    except NeedsHuman as e:
        # The person has to act: with --json, the needs_human result goes to stdout (as mes apps do).
        if "--json" in argv:
            json.dump(e.payload(), sys.stdout, ensure_ascii=False)
            print()
        _log_to_file(e.message)
        _report(e.message, argv, e.kind, e.exit_code, e.action)
        return e.exit_code
    except FitdataError as e:
        _log_to_file(e.message)
        _report(e.message, argv, e.kind, e.exit_code, e.hint)
        return e.exit_code
    except (ValueError, OverflowError) as e:
        # A value fitdata could not use (a number far too large, a file that is not a FIT file...).
        # Said in plain words instead of a Python traceback; the technical text stays short.
        _report("fitdata could not use one of the values it was given, so it stopped without "
                f"changing anything. Check the command and try again. (Detail: {type(e).__name__}: "
                f"{str(e)[:200]})", argv, "bad_input")
        return 2


def _guide() -> str:
    agents = desc.REPO / "AGENTS.md"
    if agents.is_file():
        return agents.read_text()
    return ("# Using fitdata\n\nAlways call `fitdata --json <command>`. Run `fitdata --json describe` for every "
            "command, its kind and its arguments.\n")


def _main(argv: list[str], forwarded: bool = False) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if forwarded and args.cmd == "decode":
        # The sending side never forwards decode (it always runs on the machine that has the file),
        # so a forwarded decode is not from fitdata itself. It would read any file on this machine.
        raise Refused("This machine received a forwarded 'decode' request, which fitdata never sends, "
                      "so it was refused. Run decode on the machine that has the FIT file.")
    # Checked before anything runs or is forwarded: a command above MES_ALLOW never starts.
    level = desc.allowed_level()
    desc.check_allowed(desc.COMMAND_KINDS[args.cmd], level)
    if args.cmd == "decode" and args.csv:
        desc.check_allowed("write", level)

    if args.cmd == "describe":
        if args.json:
            _print(desc.describe(parser), True)
        else:
            print(desc.describe_text(parser))
        return 0
    if args.cmd == "guide":
        text = _guide()
        if args.json:
            _print({"app": "fitdata-garmin", "source": "AGENTS.md", "guide": text}, True)
        else:
            print(text)
        return 0

    cfg = load()
    if args.cmd not in LOCAL_ONLY and cfg.remote_host and not args.local:
        return _forward(argv, cfg.remote_host, cfg.remote_command, interactive=args.cmd == "login")

    if args.cmd == "decode":
        if os.environ.get("MES_VIA_GATE"):
            raise Refused("decode reads any file on this machine, so it is not run for requests that come "
                          "through the mes gate. Ask for the activity instead: fitdata activity ID --records.")
        from .fit import decode_cli
        return decode_cli(args.file, args.csv)
    if args.cmd == "login":
        from .client import interactive_login
        interactive_login()
        return 0

    if args.cmd not in COLLECT:
        from . import query, sync
        if args.cmd == "status":
            st = sync.status()
            if args.json:
                _print(st, True)
            else:
                sync.print_status(st)
            return 0
        out = {
            "summary": lambda: query.summary(args.days),
            "days": lambda: query.days(args.frm, args.to, args.days, args.fields),
            "activities": lambda: query.activities(args.frm, args.to, args.days, args.type_, args.limit),
            "day": lambda: query.day(args.date, args.source),
            "activity": lambda: query.activity(args.id, args.source, args.records, args.every, args.columns),
            "sources": query.sources,
            "schema": query.schema,
            "sql": lambda: query.sql(args.query, args.limit),
        }[args.cmd]()
        _print(out, args.json)
        return 0

    if args.cmd == "backfill" and args.until:
        check_date(args.until, "--until date")
    _setup_logging(args.verbose)
    from . import index
    if args.cmd == "index":
        index.rebuild()
        return 0
    if args.cmd == "migrate-storage":
        from .migrate import migrate
        with _lock():
            result = migrate(args.to, progress=not args.json)
            if result.get("changed"):
                index.rebuild()
        if args.json:
            _print(result, True)
        else:
            print(result["message"])
        return 0
    from . import sync
    from .client import Session
    with _lock():
        s = Session().connect()
        if args.cmd == "sync":
            sync.sync_recent(s, args.days, force=args.force)
        elif args.cmd == "activities-sync":
            sync.sync_activities(s, date.today(), args.days)
        elif args.cmd == "backfill":
            sync.backfill(s, args.max_days, args.until)
        elif args.cmd == "account":
            sync.sync_account(s)
        index.rebuild()
    return 0


if __name__ == "__main__":
    sys.exit(main())
