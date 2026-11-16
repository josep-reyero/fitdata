"""`fitdata`: the project command. It hands each command to the project's apps.

The fitdata project does not depend on my-easy-software (mes), so this is its own implementation
of the project command of the apps contract (section 5), with the standard library only:

    fitdata                         a short help: the built-ins, then each app's short name and title
    fitdata --json                  the same as `fitdata --json describe`
    fitdata describe [--json]       every app of the project with its commands and their kinds (read)
    fitdata guide [--json]          AGENTS.md of the project, then each app's guide (read)
    fitdata apps [--json]           the project's apps and where they are installed (read)
    fitdata <app> <command> ...     runs the app's own program: `fitdata garmin sync` runs
                                    `fitdata-garmin sync` (the short name or the full app name)
    fitdata <command> ...           while the project has exactly one app, the same as
                                    `fitdata <that app> <command> ...`, with a one-line note on
                                    stderr (none with --json): `fitdata sync --days 3` still works

An app's program always runs itself (os.execv), so its output, exit code, terminal and its own
MES_ALLOW checks are exactly the app's: nothing can be reached through this command that the app
would refuse. The built-ins are read commands and check MES_ALLOW first (unset: admin; empty or
unknown: nothing is allowed, exit 4).

The project folder is MES_PROJECT_DIR when set, else the parent of the environment this runs in
(<project>/.venv). The apps are found through apps.lock.json (written by `mes apps sync`) when it
exists, else at <project>/apps/<package>/app.toml. Programs are taken only from the environment's
own bin folder, never from PATH. Every file is read without following a link, as a regular file,
with a size limit, and every name in it is checked before it is used in a path or a command.

fitdata-garmin forwards commands to another machine over SSH as `<command> --forwarded-argv
<encoded arguments>`, where <command> is usually this one; such a request is handed to
fitdata-garmin unchanged and without a note.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


# ------------------------------------------------------------------ names and limits (contract, section 0)

PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
APP_NAME_RE = PROJECT_NAME_RE
PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}\Z")
CLI_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
CLI_OBJECT_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*\Z")
# Programs that run any code or install software: an app's program can never be one of them.
NOT_APP_PROGRAMS = re.compile(
    r"^(?:python[0-9.]*|pythonw|pip[0-9.]*|uv|uvx|pydoc[0-9.]*|ipython[0-9]*|pytest|py\.test|activate.*|"
    r"sh|bash|zsh|fish|dash|env|deactivate|mes|easy-ui|mybucket)\Z", re.IGNORECASE)
RESERVED_SHORT = ("describe", "guide", "apps", "help")
BUILTINS = ("describe", "guide", "apps")
MAX_APPS = 16
MAX_PROJECT_TOML = 256 * 1024
MAX_APP_TOML = 64 * 1024
MAX_LOCK = 64 * 1024
MAX_GUIDE = 1024 * 1024
MAX_TITLE = 80
DESCRIBE_TIMEOUT_S = 60
LOCK_FILE = "apps.lock.json"
KINDS = ("read", "write", "admin")
EXIT_CODES = {"0": "ok", "1": "error", "2": "usage", "3": "not found", "4": "not allowed",
              "5": "refused for safety", "6": "needs a person", "75": "temporary failure, retry"}
JSON_NOTE = "Add --json (before or after the command) to get only JSON data on stdout."
FORWARDED = "--forwarded-argv"   # a request fitdata-garmin forwarded over SSH (see the docstring)
FORWARDED_APP = "fitdata-garmin"

STALE_LOCK = "The list of installed apps (apps.lock.json) is out of date, so the apps cannot be found safely."
SYNC_HINT = "Run: mes apps sync"


class Problem(Exception):
    """A plain-language error with its exit code and a short machine word for --json."""

    def __init__(self, message: str, *, hint: str | None = None, kind: str = "error", code: int = 1) -> None:
        super().__init__(message)
        self.message, self.hint, self.kind, self.code = message, hint, kind, code


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "...'"


# ------------------------------------------------------------------ reading files safely

def _read(path: Path, limit: int, what: str) -> bytes:
    """A regular file, never through a symbolic link, at most `limit` bytes."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Problem(f"{what} ({path}) does not exist.", kind="not_found", code=3) from None
    except OSError as e:
        raise Problem(f"{what} ({path}) could not be read: {e.strerror}.") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise Problem(f"{what} ({path}) is a link or not a regular file, so fitdata did not read it.",
                      hint="Replace it with the file itself.")
    if st.st_size > limit:
        raise Problem(f"{what} ({path}) is larger than {limit // 1024} KB, so fitdata did not read it.",
                      hint="Check that it is the right file.")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise Problem(f"{what} ({path}) could not be read: {e.strerror}.") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise Problem(f"{what} ({path}) is not a regular file, so fitdata did not read it.")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Problem(f"{what} ({path}) is larger than {limit // 1024} KB, so fitdata did not read it.")
    return data


def _toml(data: bytes, path: Path, what: str) -> dict:
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise Problem(f"{what} ({path}) is not valid TOML ({e}), so fitdata cannot tell what it says.",
                      hint="Fix the line it names.", kind="config_invalid") from None


def _is_real_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


# ------------------------------------------------------------------ the project and its apps

@dataclass(frozen=True)
class App:
    package: str
    name: str
    short: str
    title: str
    program: str
    folder: Path
    in_project: bool


@dataclass(frozen=True)
class Project:
    root: Path
    name: str
    title: str
    version: str
    apps: tuple[App, ...]
    bin: Path

    def program_path(self, app: App) -> Path:
        path = self.bin / app.program
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise Problem(f"The program of the app {app.name} is not installed in the project's environment.",
                          hint="Run: uv sync", kind="not_found", code=3)
        return path


def find_root() -> Path:
    env = os.environ.get("MES_PROJECT_DIR")
    if env:
        root = Path(os.path.abspath(env))
        if not (root / "project.toml").is_file():
            raise Problem(f"MES_PROJECT_DIR is set to {root}, but that folder has no project.toml, so fitdata "
                          "cannot tell which project to use.",
                          hint="Point MES_PROJECT_DIR at the folder that holds project.toml, or remove it.")
        return root
    root = Path(os.path.abspath(sys.prefix)).parent
    if (root / "project.toml").is_file():
        return root
    raise Problem("fitdata could not find its project folder (the one with project.toml), so it does not know "
                  "which apps it has.",
                  hint="Run the fitdata of the project's own environment (<project>/.venv/bin/fitdata), or set "
                  "MES_PROJECT_DIR to the project folder.")


def _project_table(data: dict, path: Path) -> tuple[str, str, str, list[str]]:
    if isinstance(data.get("apps"), dict):
        raise Problem("project.toml still has the old [apps.<name>] section. Apps are now Python packages, each with "
                      "its own app.toml, and the project lists them under [project] apps. The project cannot be "
                      "used until this is changed.",
                      hint='Move the app into apps/<package>/ with an app.toml (see: mes guide apps), write apps = '
                      '["<package>"] under [project], and remove [apps.<name>].', kind="config_invalid")
    if isinstance(data.get("jobs"), dict):
        name = next(iter(data["jobs"]), "<name>")
        raise Problem(f"project.toml still has [jobs.{name}]. Jobs now belong to the app that runs them, in its "
                      "app.toml, so they are no longer read here.",
                      hint=f"Move the job to [jobs.{name}] in the app's app.toml and drop the app's name from its run "
                      "line. To change a schedule from the project, use [settings.<app>].", kind="config_invalid")
    if isinstance(data.get("ui"), dict) and "data" in data["ui"]:
        name = (data.get("project") or {}).get("name", "<project>")
        raise Problem(f"[ui] data is no longer used: every project has one shared database, {name}.sqlite, in "
                      "its data folder.", hint="Remove the data line from [ui].", kind="config_invalid")
    project = data.get("project")
    if not isinstance(project, dict):
        raise Problem(f"{path} has no [project] table, so fitdata cannot tell which project this is.",
                      hint='Add [project] with name = "<project>" and apps = ["<package>"].', kind="config_invalid")
    name = project.get("name")
    if not (isinstance(name, str) and PROJECT_NAME_RE.match(name)) or name == "mes" or NOT_APP_PROGRAMS.match(name):
        raise Problem(f"The project name in project.toml ({_show(name)}) is not a name fitdata can use.",
                      hint="Use 2 to 31 lowercase letters, digits and '-', starting with a letter.",
                      kind="config_invalid")
    title = project.get("title", name)
    if not isinstance(title, str) or len(title) > MAX_TITLE or any(ord(c) < 32 for c in title):
        raise Problem("The [project] title in project.toml must be one line of text, at most 80 characters.",
                      kind="config_invalid")
    version = project.get("version", "0.0.0")
    if not isinstance(version, str) or not re.match(r"^[A-Za-z0-9.+-]{1,40}\Z", version):
        raise Problem('The [project] version in project.toml must be a short version such as "0.1.0".',
                      kind="config_invalid")
    if "apps" not in project:
        raise Problem("project.toml does not list its apps, so nothing knows which commands, jobs and dashboards "
                      "the project has.", hint='Add apps = ["<package>"] under [project].', kind="config_invalid")
    apps = project["apps"]
    if (not isinstance(apps, list) or not 1 <= len(apps) <= MAX_APPS
            or not all(isinstance(a, str) and PACKAGE_RE.match(a) for a in apps) or len(set(apps)) != len(apps)):
        raise Problem("[project] apps in project.toml must list 1 to 16 package names (lowercase letters, digits "
                      "and '_'), each once.", hint='For example: apps = ["fitdata_garmin"]', kind="config_invalid")
    return name, title, version, apps


def _app_toml(folder: Path, package: str, project: str) -> tuple[str, str, str]:
    """(name, title, program) from <folder>/app.toml, after checking every value used."""
    path = folder / "app.toml"
    data = _toml(_read(path, MAX_APP_TOML, f"The app.toml of {package}"), path, f"The app.toml of {package}")
    app = data.get("app")
    if not isinstance(app, dict):
        raise Problem(f"{path} has no [app] table, so fitdata cannot tell which app {package} is.",
                      hint='Add [app] with name = "<app name>" and command = "<its program>".', kind="config_invalid")
    name = app.get("name")
    if (not (isinstance(name, str) and APP_NAME_RE.match(name)) or name in ("mes", project)
            or NOT_APP_PROGRAMS.match(name)):
        raise Problem(f"The app name in {path} ({_show(name)}) is not a name fitdata can use.",
                      hint="Use 2 to 31 lowercase letters, digits and '-', not the project's own name.",
                      kind="config_invalid")
    title = app.get("title", name)
    if not isinstance(title, str) or len(title) > MAX_TITLE or any(ord(c) < 32 for c in title):
        raise Problem(f"The [app] title in {path} must be one line of text, at most 80 characters.",
                      kind="config_invalid")
    cli, command = app.get("cli"), app.get("command")
    if (cli is None) == (command is None):
        raise Problem(f"{path} must say either cli or command under [app] (exactly one), so fitdata knows which "
                      "program runs the app.", kind="config_invalid")
    if cli is not None:
        if not (isinstance(cli, str) and CLI_OBJECT_RE.match(cli)):
            raise Problem(f"[app] cli in {path} ({_show(cli)}) is not of the form module:object.",
                          kind="config_invalid")
        program = name
    else:
        if not (isinstance(command, str) and CLI_NAME_RE.match(command)) or NOT_APP_PROGRAMS.match(command):
            raise Problem(f"[app] command in {path} ({_show(command)}) is not a program name fitdata can run.",
                          hint="Name the app's own console script, for example command = \"fitdata-garmin\".",
                          kind="config_invalid")
        program = command
    return name, title, program


def _short(name: str, project: str) -> str:
    return name[len(project) + 1:] if name.startswith(project + "-") else name


def _from_lock(root: Path, project: str, digest: str, packages: list[str]) -> list[tuple]:
    """(package, folder, in_project, name, program) of each app, from a lock that passes every check."""
    def stale(why: str) -> Problem:
        return Problem(f"{STALE_LOCK} ({why})", hint=SYNC_HINT, kind="apps_lock_stale")
    data = _read(root / LOCK_FILE, MAX_LOCK, "The list of installed apps")
    try:
        lock = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise stale("it is not valid JSON") from None
    if not isinstance(lock, dict) or lock.get("v") != 1:
        raise stale("it is not in a format this fitdata knows")
    if lock.get("project") != project:
        raise stale("it is for another project")
    if lock.get("project_toml_sha256") != digest:
        raise stale("project.toml changed after it was written")
    apps = lock.get("apps")
    if not isinstance(apps, list) or [a.get("package") if isinstance(a, dict) else None for a in apps] != packages:
        raise stale("its apps are not the ones [project] apps lists")
    out = []
    for a in apps:
        path, name, program, in_project = a.get("path"), a.get("name"), a.get("program"), a.get("in_project")
        if (not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise stale(f"the folder of {a['package']} is not a plain absolute path")
        folder = Path(path)
        if not _is_real_dir(folder):
            raise stale(f"the folder of {a['package']} is missing or is a link")
        if not isinstance(in_project, bool) or not isinstance(name, str) or not isinstance(program, str):
            raise stale(f"the entry of {a['package']} is incomplete")
        if in_project and os.path.realpath(folder) != os.path.realpath(root / "apps" / a["package"]):
            raise stale(f"{a['package']} is said to be in the project but its folder is elsewhere")
        out.append((a["package"], folder, in_project, name, program))
    return out


def load(root: Path | None = None) -> Project:
    root = root or find_root()
    path = root / "project.toml"
    raw = _read(path, MAX_PROJECT_TOML, "project.toml")
    name, title, version, packages = _project_table(_toml(raw, path, "project.toml"), path)
    digest = hashlib.sha256(raw).hexdigest()
    if os.path.lexists(root / LOCK_FILE):
        located = _from_lock(root, name, digest, packages)
    else:
        located = []
        for package in packages:
            folder = root / "apps" / package
            if not (_is_real_dir(folder) and os.path.lexists(folder / "app.toml")):
                raise Problem(f"The app {package} is not inside this project's apps folder and the list of installed "
                              "apps has not been written yet.", hint=SYNC_HINT, kind="not_found", code=3)
            located.append((package, folder, True, None, None))
    apps: list[App] = []
    seen: dict[str, str] = {}
    for package, folder, in_project, lock_name, lock_program in located:
        app_name, app_title, program = _app_toml(folder, package, name)
        if lock_name is not None and (lock_name != app_name or lock_program != program):
            raise Problem(f"{STALE_LOCK} (the app in {folder} is now {app_name})", hint=SYNC_HINT,
                          kind="apps_lock_stale")
        short = _short(app_name, name)
        if short in RESERVED_SHORT:
            raise Problem(f"The app {app_name} would be called {short!r} in fitdata commands, which is one of the "
                          "project command's own words.", hint="Rename the app.", kind="config_invalid")
        for word in {app_name, short}:
            if word in seen and seen[word] != app_name:
                raise Problem(f"The apps {seen[word]} and {app_name} would both be called {word!r} in fitdata "
                              "commands.", hint="Rename one of them.", kind="config_invalid")
            seen[word] = app_name
        apps.append(App(package, app_name, short, app_title, program, folder, in_project))
    return Project(root, name, title, version, tuple(apps), Path(sys.executable).parent)


# ------------------------------------------------------------------ MES_ALLOW (as mes.app.allowed_level)

def allowed_level() -> str:
    raw = os.environ.get("MES_ALLOW")
    if raw is None:
        return "admin"
    value = raw.strip().lower()
    how = 'Set MES_ALLOW to "read", "write" or "admin", or remove it.'
    if not value:
        raise Problem("MES_ALLOW is set but empty, so fitdata cannot tell what this session may do, and nothing is "
                      "allowed to run.", hint=how, kind="not_allowed", code=4)
    if value not in KINDS:
        raise Problem(f"MES_ALLOW is set to {raw[:20]!r}, which fitdata does not recognise, so nothing is allowed "
                      "to run.", hint=how, kind="not_allowed", code=4)
    return value


# ------------------------------------------------------------------ the built-ins

def _run_json(project: Project, app: App, *words: str) -> dict:
    program = project.program_path(app)
    try:
        res = subprocess.run([str(program), "--json", *words], capture_output=True, timeout=DESCRIBE_TIMEOUT_S,
                             stdin=subprocess.DEVNULL)
        data = json.loads(res.stdout) if res.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = None
    if not isinstance(data, dict):
        raise Problem(f"The app {app.name} did not answer `{app.program} --json {' '.join(words)}` as expected, so "
                      f"fitdata cannot list what it does.",
                      hint=f"Run `{app.program} --json {' '.join(words)}` to see what goes wrong; then `uv sync`.",
                      kind="app_broken")
    return data


def describe(project: Project, level: str) -> dict[str, Any]:
    apps = []
    for app in project.apps:
        d = _run_json(project, app, "describe")
        if d.get("app") != app.name:
            raise Problem(f"The app {app.name} describes itself as {_show(d.get('app'))}, so fitdata cannot trust "
                          "its list of commands.", hint="Check its app.toml and run: uv sync", kind="app_broken")
        apps.append({"name": app.name, "short": app.short, "package": app.package, "title": app.title,
                     "program": app.program, "describe": d})
    return {"project": project.name, "title": project.title, "version": project.version, "command": project.name,
            "allowed": level, "json": JSON_NOTE, "exit_codes": dict(EXIT_CODES), "apps": apps,
            # Jobs are composed from the app.toml files by mes; this command does not import mes.
            "jobs": []}


def describe_text(d: dict[str, Any]) -> str:
    may = {"read": "only read commands (they only look)",
           "write": "read and write commands (write commands change data)",
           "admin": "read, write and admin commands"}[d["allowed"]]
    lines = [f"{d['title']} ({d['project']}), version {d['version']}", f"This session may run {may}.", ""]
    for a in d["apps"]:
        lines.append(f"{a['short']}: {a['title']} ({a['name']})")
        rows = [(c.get("name", ""), c.get("kind", ""), (c.get("help") or "").splitlines()[0] if c.get("help") else "")
                for c in a["describe"].get("commands", []) if isinstance(c, dict)]
        width = max((len(str(r[0])) for r in rows), default=0)
        lines += [f"  {str(n).ljust(width)}  {str(k).ljust(5)}  {h}" for n, k, h in rows]
        lines.append("")
    first = d["apps"][0]["short"] if d["apps"] else "<app>"
    lines.append(f"Run a command as: {d['project']} {first} <command> (add --json for data). For one command's "
                 f"arguments: {d['project']} {first} <command> --help.")
    return "\n".join(lines)


def guide(project: Project) -> dict[str, Any]:
    parts, texts = [], []
    path = project.root / "AGENTS.md"
    if os.path.lexists(path):
        texts.append(_read(path, MAX_GUIDE, "The project's AGENTS.md").decode("utf-8", errors="replace").rstrip())
        parts.append({"source": "project", "file": "AGENTS.md"})
    for app in project.apps:
        texts.append(f"## {app.title} ({project.name} {app.short} ...)")
        own = app.folder / "AGENTS.md"
        if os.path.lexists(own):
            texts.append(_read(own, MAX_GUIDE, f"The AGENTS.md of {app.name}").decode("utf-8", errors="replace").rstrip())
            parts.append({"source": app.name, "file": "AGENTS.md"})
        else:
            text = _run_json(project, app, "guide").get("guide")
            texts.append(str(text or "").rstrip())
            parts.append({"source": app.name, "file": "generated"})
    return {"project": project.name, "guide": "\n\n".join(texts) + "\n", "parts": parts}


def apps_list(project: Project) -> dict[str, Any]:
    return {"project": project.name,
            "apps": [{"name": a.name, "short": a.short, "package": a.package, "title": a.title, "program": a.program,
                      "location": "in-project" if a.in_project else "installed"} for a in project.apps]}


def help_text(project: Project) -> str:
    lines = [f"{project.title} ({project.name}): the project command. It hands each command to one of the project's apps.",
             "", "Built-in commands (add --json for data):",
             f"  {project.name} describe     every app, with its commands and what each may do",
             f"  {project.name} guide        how an AI agent should use this project (AGENTS.md of the project and apps)",
             f"  {project.name} apps         the project's apps and where they are installed", "", "Apps:"]
    width = max(len(a.short) for a in project.apps)
    lines += [f"  {project.name} {a.short.ljust(width)} <command>   {a.title} ({a.program})" for a in project.apps]
    if len(project.apps) == 1:
        a = project.apps[0]
        lines += ["", f"While {a.name} is the only app, `{project.name} <command>` runs it too "
                  f"(`{project.name} {a.short} <command>` keeps working when more apps are added)."]
    lines += ["", f"For an app's own commands: {project.name} {project.apps[0].short} --help"]
    return "\n".join(lines)


# ------------------------------------------------------------------ dispatch

def _print(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False)
    print()


def _exec(project: Project, app: App, args: list[str]) -> NoReturn:
    program = project.program_path(app)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(str(program), [str(program), *args])


def _builtin(project: Project, word: str, args: list[str], as_json: bool) -> int:
    level = allowed_level()  # built-ins are read commands: refused when MES_ALLOW allows nothing
    extra = [a for a in args if a != "--json"]
    if extra:
        raise Problem(f"`{project.name} {word}` takes no arguments except --json, but it was given "
                      f"{_show(' '.join(extra))}.", hint=f"Run: {project.name} {word} --json", kind="usage", code=2)
    if word == "describe":
        d = describe(project, level)
        _print(d) if as_json else print(describe_text(d))
    elif word == "guide":
        g = guide(project)
        _print(g) if as_json else sys.stdout.write(g["guide"])
    else:
        a = apps_list(project)
        if as_json:
            _print(a)
        else:
            for x in a["apps"]:
                print(f"{x['short']}  {x['name']}  {x['title']}  ({x['location']}, program {x['program']})")
    return 0


def run(argv: list[str]) -> int:
    as_json = "--json" in argv
    project = load()
    words = [a for a in argv if a != "--json"]
    if not words:
        if as_json:
            return _builtin(project, "describe", [], True)
        print(help_text(project))
        return 0
    w = words[0]
    rest = list(argv)
    rest.remove(w)  # the arguments without that one word (its first occurrence)
    if w in ("-h", "--help", "help"):
        print(help_text(project))
        return 0
    if w in BUILTINS:
        return _builtin(project, w, rest, as_json)
    for app in project.apps:
        if w in (app.short, app.name):
            _exec(project, app, rest)
    if w == FORWARDED:
        target = next((a for a in project.apps if a.name == FORWARDED_APP), None)
        if target:
            _exec(project, target, list(argv))
    if len(project.apps) == 1:
        app = project.apps[0]
        if not as_json:
            print(f'Note: "{project.name} {w}" was passed to {app.name}, the only app of this project. '
                  f'Write "{project.name} {app.short} {w}" so it keeps working when the project has more apps.',
                  file=sys.stderr)
        _exec(project, app, list(argv))
    shorts = ", ".join(a.short for a in project.apps)
    raise Problem(f'{project.name} has several apps, so "{w}" does not say which one is meant.',
                  hint=f"Start with the app's short name: {shorts}. For example: "
                  f"{project.name} {project.apps[0].short} {w}.", kind="usage", code=2)


def _report(e: Problem, as_json: bool) -> None:
    if as_json:
        body = {"error": e.message, "kind": e.kind, "exit_code": e.code}
        if e.hint:
            body["hint"] = e.hint
        json.dump(body, sys.stderr, ensure_ascii=False)
        print(file=sys.stderr)
    else:
        print(f"fitdata: {e.message}" + (f"\n  {e.hint}" if e.hint else ""), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        return run(argv)
    except Problem as e:
        _report(e, "--json" in argv)
        return e.code
    except OSError as e:  # os.execv failed: the program is there but could not be started
        _report(Problem(f"fitdata could not start an app's program ({e.strerror}).", hint="Run: uv sync"),
                "--json" in argv)
        return 1


if __name__ == "__main__":
    sys.exit(main())
