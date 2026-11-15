"""fitdata: a personal Garmin archive, organised per day, with a CLI for people and AI agents.

Reading (add --json for machine-readable output):
  fitdata summary [--days 7]                    recent days' key numbers + activities
  fitdata days [--from D] [--to D] [--days N] [--fields a,b]
  fitdata activities [--from D] [--to D] [--type trail] [--limit N]
  fitdata day YYYY-MM-DD [--source sleep]       a day's overview, or one source's full data
  fitdata activity ID [--source splits] [--records [--every 5] [--columns a,b]]
  fitdata sources                               what each source contains
  fitdata schema                                index tables and columns
  fitdata sql "SELECT ..."                      read-only SQL on the index
  fitdata status                                archive size, last sync, login

Collecting (on the machine that holds the archive):
  fitdata login                                 sign in to Garmin (asks in the terminal)
  fitdata sync [--days 3]                       refresh recent days and activities
  fitdata activities-sync [--days 2]            just check for new activities (cheap)
  fitdata backfill [--max-days 60]              fill in history, newest first
  fitdata account | index
  fitdata decode FILE.fit [--csv]               decode any FIT file

With [remote] host set in the config, every command except `decode` runs on that machine over
SSH (use --local to force this machine). Config: ~/.config/fitdata/config.toml (see README).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import shlex
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from typing import Any

from .config import load

COLLECT = {"login", "sync", "activities-sync", "backfill", "account", "index"}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fitdata", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--local", action="store_true", help="use this machine's archive even if a remote is configured")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("summary", help="recent days and activities"); s.add_argument("--days", type=int, default=7)
    s = sub.add_parser("days", help="one row of key numbers per day")
    s.add_argument("--from", dest="frm"); s.add_argument("--to"); s.add_argument("--days", type=int)
    s.add_argument("--fields", help="comma-separated columns (see `fitdata schema`)")
    s = sub.add_parser("activities", help="list activities")
    s.add_argument("--from", dest="frm"); s.add_argument("--to"); s.add_argument("--days", type=int)
    s.add_argument("--type", dest="type_"); s.add_argument("--limit", type=int)
    s = sub.add_parser("day", help="one day"); s.add_argument("date"); s.add_argument("--source")
    s = sub.add_parser("activity", help="one activity"); s.add_argument("id"); s.add_argument("--source")
    s.add_argument("--records", action="store_true", help="per-sample records (CSV-derived)")
    s.add_argument("--every", type=int, default=1, help="keep every Nth record"); s.add_argument("--columns")
    sub.add_parser("sources", help="what each source contains")
    sub.add_parser("schema", help="index tables and columns")
    s = sub.add_parser("sql", help="read-only SQL on the index"); s.add_argument("query")
    sub.add_parser("status", help="archive overview")

    sub.add_parser("login", help="sign in to Garmin")
    s = sub.add_parser("sync", help="refresh recent days and activities"); s.add_argument("--days", type=int, default=3)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("activities-sync", help="check for new activities"); s.add_argument("--days", type=int, default=2)
    s = sub.add_parser("backfill", help="fill in history"); s.add_argument("--max-days", type=int, default=60)
    s.add_argument("--until", help="oldest date to reach")
    sub.add_parser("account", help="refresh profile, devices, zones, gear")
    sub.add_parser("index", help="rebuild the SQLite index")
    s = sub.add_parser("decode", help="decode a FIT file"); s.add_argument("file"); s.add_argument("--csv", action="store_true")
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

def _forward(argv: list[str], host: str, command: str, interactive: bool) -> int:
    remote = f"{command} --local " + " ".join(shlex.quote(a) for a in argv)
    ssh = ["ssh", *(["-t"] if interactive else []), host, remote]
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
    paths.STATE.mkdir(parents=True, exist_ok=True)
    with open(paths.LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.getLogger("fitdata").info("Another sync is running; skipping.")
            sys.exit(0)
        yield


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _parser().parse_args(argv)
    cfg = load()

    if args.cmd != "decode" and cfg.remote_host and not args.local:
        return _forward(argv, cfg.remote_host, cfg.remote_command, interactive=args.cmd == "login")

    if args.cmd == "decode":
        from .fit import decode_cli
        return decode_cli(args.file, args.csv)
    if args.cmd == "login":
        from .client import interactive_login
        interactive_login()
        return 0

    if args.cmd not in COLLECT:
        from . import query, sync
        if args.cmd == "status":
            return sync.status()
        out = {
            "summary": lambda: query.summary(args.days),
            "days": lambda: query.days(args.frm, args.to, args.days, args.fields),
            "activities": lambda: query.activities(args.frm, args.to, args.days, args.type_, args.limit),
            "day": lambda: query.day(args.date, args.source),
            "activity": lambda: query.activity(args.id, args.source, args.records, args.every, args.columns),
            "sources": query.sources,
            "schema": query.schema,
            "sql": lambda: query.sql(args.query),
        }[args.cmd]()
        _print(out, args.json)
        return 0

    _setup_logging(args.verbose)
    from . import index, sync
    from .client import NotLoggedIn, Session
    if args.cmd == "index":
        index.rebuild()
        return 0
    try:
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
    except NotLoggedIn as e:
        logging.getLogger("fitdata").error(str(e))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
