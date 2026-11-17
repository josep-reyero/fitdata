# fitdata

> **Consumer apps should ship APIs that let people consume their own data. People then set up their own
> dashboards and own their data, in an AI-native, fully flexible way.**

Keep your own complete copy of your Garmin data, and use it any way you like:

- **A CLI for agents.** Every command answers in plain text or, with `--json`, in data an AI agent
  can read, so a coding agent can act as your fitness assistant over your own numbers.
- **Fully flexible dashboards for humans.** The dashboards are plain description files, not a
  fixed app: you, or your agent, reshape them on the fly to show what you care about today.
- **Fully flexible data processing.** You hold the raw files and Garmin's own calculations, so any
  metric can be recomputed or invented: add a command or a scheduled job, and it runs on your data.

What it keeps, organised per day:

- **Raw:** the watch's original FIT files: every activity, plus the all-day wellness, sleep,
  HRV and metrics files.
- **Processed:** Garmin Connect's calculations as Garmin returns them, one JSON file per source:
  sleep stages and score, HRV status, Body Battery, stress, Training Readiness and Status,
  VO2 max, race predictions and more.
- **Activities:** one folder each, holding the original FIT, Garmin's analysis (splits, zones,
  weather...) and a per-sample `records.csv` decoded with Garmin's official FIT SDK.
- **Index:** a SQLite file you can rebuild at any time, with one row per day and per activity.
- **CLI:** `fitdata` reads the archive locally, or on another machine over SSH. `fitdata describe`
  lists every command for an agent.
- **Dashboards:** web pages over the index that you can change freely, shown by **[my-easy-ui](https://github.com/josep-reyero/my-easy-ui)** (see [`ui/README.md`](ui/README.md)).

It uses Garmin Connect's web endpoints through
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect). These are not an
official API and can change. Fetching is throttled to be gentle.

## Project and app

**fitdata** is a **project** ([`project.toml`](project.toml)) for
**[my-easy-software](https://github.com/josep-reyero/my-easy-software)** (`mes`), the framework for
small personal projects (how projects and apps fit together:
[the apps contract](https://github.com/josep-reyero/my-easy-software/blob/main/docs/apps-contract.md)).
It has one app, **fitdata-garmin**, which archives the Garmin data. The app's code, its jobs and
its own dashboards live in its package, [`apps/fitdata_garmin/`](apps/fitdata_garmin/), described
by its [`app.toml`](apps/fitdata_garmin/app.toml). More data sources can be added as more apps of
the same project: they write their rows into the same shared `days` and `activities` tables, and
the dashboards show them without changes.

**fitdata** also works on its own, without `mes`. The sections below describe both ways: standalone
(Install to Storage), and under `mes` on a server
([Running under my-easy-software on a server](#running-under-my-easy-software-on-a-server)).

Commands are written `fitdata garmin <command>`, for example `fitdata garmin sync`. `fitdata` is
the project command and hands each command to the app; `fitdata-garmin <command>` is the app's own
program and does the same (see [Commands, kinds and exit codes](#commands-kinds-and-exit-codes)).

## Install

In a clone of this repository:

```sh
uv sync --locked --no-dev                  # exactly the versions pinned in uv.lock, into .venv/
uv sync --locked --no-dev --extra bucket   # the same, plus the optional bucket storage (see Storage)
mkdir -p ~/.local/bin && ln -sf "$PWD/.venv/bin/fitdata-garmin" "$PWD/.venv/bin/fitdata" ~/.local/bin/
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). **fitdata** holds your Garmin login, so it
runs only the dependency versions recorded in `uv.lock`: `--locked` installs exactly those and stops
if `uv.lock` does not match `pyproject.toml`. (`uv tool install` would pick the newest versions
instead.) `~/.local/bin/fitdata` and `~/.local/bin/fitdata-garmin` are links to the programs in the
clone's `.venv/`, so the timers and the SSH setup below find them there.

**Update:** in the clone, `git pull`, then the same `uv sync --locked --no-dev` line as above (with
`--extra bucket` if you use it; leaving it out removes the bucket package). The links stay as they
are.

**fitdata** creates its files for its owner only (umask 077): the archive, the index, the logs and the
saved login.

## Configure

`~/.config/fitdata/config.toml` (or `$FITDATA_CONFIG`). Nothing machine-specific lives in the repo.

On the machine that keeps the archive:

```toml
data_dir = "~/fitness/data"
# storage = "files"     # the default; "bucket" keeps the files in a my-bucket-storage bucket
```

On any other machine, to read that archive over SSH:

```toml
[remote]
host = "my-server"                 # an ssh host or alias
command = "~/.local/bin/fitdata"   # default
```

With a remote set, every command runs on that host (including `login` and `sync`), except
`decode`, `describe` and `guide`. Add `--local` to use the current machine instead.

Environment overrides: `FITDATA_DATA_DIR`, `FITDATA_STORAGE`, `FITDATA_REMOTE` (`""` stays local).
When mes runs fitdata-garmin (`mes jobs run`, `mes db rebuild`, its systemd units or its gate), it
sets `MES_PROJECT=fitdata` and `MES_DATA_DIR`, and that folder is used instead of `data_dir`, so the
app writes the same `fitdata.sqlite` that `mes db` and the dashboards read (see [Where the data
lives](#where-the-data-lives)).

### Limiting what an SSH key may do

This is the standalone way. Under **my-easy-software**, agents use mes's gate instead (see [Running
under **my-easy-software** on a server](#running-under-my-easy-software-on-a-server)).

`MES_ALLOW` is only as strong as what sets it. A key that reaches the archive machine with a plain
SSH login can run anything, as you. For a key used by an agent or another machine, let the
archive machine set the level: install the gate next to **fitdata** there,

```sh
ln -sf "$PWD/scripts/fitdata-ssh-gate" ~/.local/bin/fitdata-ssh-gate
```

and start that key's line in `~/.ssh/authorized_keys` with a forced command (the path in full):

```
restrict,command="/home/you/.local/bin/fitdata-ssh-gate read" ssh-ed25519 AAAA... agent-laptop
```

The gate accepts only what **fitdata** itself sends (`<command> --forwarded-argv <encoded arguments>`,
one line), never runs the command the caller names, and starts the fitdata-garmin next to it with
`MES_ALLOW=read` (or `write`) and `MES_VIA_GATE=1` in a clean environment. A shell, `scp`, other
commands or odd characters are refused (exit 4). `restrict` also turns off port forwarding, agent
forwarding and terminals for that key. With the gate, `[remote] command` on the other machine must
be one word (the default `~/.local/bin/fitdata` is). `fitdata garmin login` needs a terminal and
admin, so run it through a key without the gate, or at the machine itself.

## Collect

```sh
fitdata garmin login      # email, password, 2FA code; saves a token (owner-only) in data_dir/.state
fitdata garmin sync       # last 3 days + their activities
fitdata garmin backfill   # history, newest first, 60 days per run (resumable)
```

It keeps itself current with three jobs:

| Job | Schedule | What it does |
|---|---|---|
| `activities` | every 10 min | new activities (`activities-sync --days 2`) |
| `daily` | 07:30, 13:30, 22:30 | the last 3 days' health data (`sync --days 3`); sleep, HRV and readiness settle the next morning |
| `backfill` | 02:15 | 90 more days of history until it's complete (`backfill --max-days 90`) |

**With `mes`**, the jobs are `[jobs.*]` in the app's [`app.toml`](apps/fitdata_garmin/app.toml),
each with the Garmin hosts it talks to, a memory cap and a time limit; `mes` installs and runs them
as `fitdata-garmin.activities`, `fitdata-garmin.daily` and `fitdata-garmin.backfill`
(`mes jobs ...`; a project can change a schedule or a limit with `[settings.fitdata-garmin]` in
`project.toml`), and rebuilds the dashboard data after each one (`[ui] refresh_after`). After
each `uv sync`, `mes apps sync` writes `apps.lock.json` (which apps the project has, where they are
installed on this machine; not committed). Without mes nothing needs it: the apps are found in `apps/`.

**Without `mes`** (the standalone way), the same jobs are systemd user timers on Linux, which run
`~/.local/bin/fitdata-garmin --local <command>`:

```sh
cp systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now fitdata-activities.timer fitdata-daily.timer fitdata-backfill.timer
loginctl enable-linger "$USER"     # keep timers running while logged out
```

Use one or the other, never both (see [Running under **my-easy-software** on a
server](#running-under-my-easy-software-on-a-server)). A run that finds another sync running, or
Garmin limiting requests, exits 75 (temporary). The units count that as success, and the next run
carries on. Each unit has the same limits as its `mes` job (`MemoryMax=512M`, `TimeoutStartSec`
20 minutes, 30 minutes and 3 hours), cannot gain privileges (`NoNewPrivileges=yes`) and creates
owner-only files (`UMask=0077`). After copying changed units, run `systemctl --user daemon-reload`
again.

A broken entry in Garmin's list of activities is logged and skipped; the others are still fetched,
and that day is fetched again on the next run. A damaged saved login exits 6 (run
`fitdata garmin login`).

## Query

```sh
fitdata garmin summary --days 7
fitdata garmin days --days 30 --fields resting_hr,hrv_last_night_avg,sleep_score
fitdata garmin activities --type trail --limit 10
fitdata garmin day 2026-11-14 --source sleep
fitdata garmin activity 123456789 --source splits
fitdata garmin activity 123456789 --records --every 10 --columns timestamp,heart_rate,enhanced_altitude
fitdata garmin sql "SELECT date, steps, resting_hr FROM days ORDER BY date DESC LIMIT 14"
fitdata garmin sources     # what each source contains
fitdata garmin schema      # index tables and columns
fitdata garmin status      # archive overview: days, activities, last sync, login, storage
```

`sql` runs one read-only `SELECT` on the index: the project's shared views `days` and
`activities` (their first column, `source`, names the app a row came from) and the app's own
table `garmin_files`. Numbers come back as Garmin gave them. It returns at most 1000 rows unless
you pass `--limit N`, and says on stderr when there are more; a query may run for 30 seconds at
most, in memory only, and one at a time per archive (a second one meanwhile exits 75: try again in
a moment). Read commands never write to the data folder, so they also work where it is read-only
(as mes's gate gives it to read requests). There, `sql` takes its lock file `.state/sql.lock`
read-only. `fitdata garmin index` (and so every sync) creates that file. If it does not exist yet,
the query runs without the lock and says so on stderr.

The archive is read without ever following a link: a symbolic or hard link inside
`<data_dir>/garmin/` (for example after restoring a backup) is skipped and named in a warning,
so nothing outside that folder, and never the saved login, can be read or written through it.

Add `--json` for machine-readable output. [`AGENTS.md`](AGENTS.md) explains how an AI agent
should use the CLI.

## Commands, kinds and exit codes

`fitdata` is the project command. Its own commands are read commands: `fitdata describe` (every
app with its commands and their kinds; `--json` for data), `fitdata guide` (this project's
[`AGENTS.md`](AGENTS.md), then each app's) and `fitdata apps`. Anything else goes to an app:

| You type | What runs |
|---|---|
| `fitdata garmin sync --days 3` | `fitdata-garmin sync --days 3` (`fitdata fitdata-garmin ...` too) |
| `fitdata sync --days 3`, `fitdata --json days`, `fitdata --local sync` | `fitdata-garmin` with the same words, while it is the project's only app |

The second form prints, once per terminal session and never with `--json`, one line on stderr:
`Note: "**fitdata** sync" was passed to fitdata-garmin, the only app of this project. Write "**fitdata**
garmin sync" so it keeps working when the project has more apps.` While there is one app,
`fitdata --help` also lists its commands, and `fitdata --json describe` also carries its
`commands` and `global_options` at the top level. Output, exit code and terminal are the app's
own: the project command starts the app's program in its place (from the clone's `.venv/bin`,
never from `PATH`), and the app checks `MES_ALLOW` itself, so nothing is reachable through
`fitdata` that `fitdata-garmin` would refuse.

`fitdata-garmin --json describe` lists every command of the app with its **kind**, every argument
and the app's jobs, in the shape every `mes` app uses. `mes` jobs and the `mes` gate read it to
know what each command may do.

| Kind | Commands of fitdata-garmin |
|---|---|
| read | `summary`, `days`, `activities`, `day`, `activity`, `sources`, `schema`, `sql`, `status`, `decode`, `describe`, `guide` |
| write | `sync`, `activities-sync`, `backfill`, `account`, `index`, `migrate-storage` |
| admin | `login` |

`MES_ALLOW=read|write|admin` sets the highest kind this session may run. Unset means admin (you
at your own computer); set but empty or unknown allows nothing (also for `fitdata describe`,
`guide` and `apps`). A command above it exits 4 before it starts, and before anything is sent to a
remote. `decode --csv` writes files, so it needs write; `decode` is never run for a request that
came through the `mes` gate.

| Exit | Meaning |
|---|---|
| 0 | ok |
| 1 | error (for example a config value **fitdata** cannot use) |
| 2 | usage: the command line or a value in it was not understood |
| 3 | not found: no such day, activity, source, index or bucket |
| 4 | not allowed by `MES_ALLOW` |
| 5 | refused for safety (a forwarded `decode`, a migration copy that did not verify, a link in the archive) |
| 6 | needs a person: `fitdata garmin login`, also when the saved login is damaged (with `--json`, a `needs_human` result on stdout) |
| 75 | temporary: Garmin's rate limit, Garmin unreachable, or another sync running; try later |

With `--json`, errors are one JSON line on stderr: `{"error", "kind", "exit_code", "hint"}`.

## Dashboards

Four web pages, shown by **[my-easy-ui](https://github.com/josep-reyero/my-easy-ui)**: **Today** is
the project's own page ([`ui/dashboards/overview.yaml`](ui/dashboards/overview.yaml)), where
fitdata-garmin adds its readings and charts; **Sleep and recovery**, **Activities** and **Trends**
come with the app ([`apps/fitdata_garmin/dashboards/`](apps/fitdata_garmin/dashboards/)). They
read only the shared `days` and `activities` views of the index. See
[`ui/README.md`](ui/README.md) for previewing them with made-up sample data and pointing them at
your archive.

## Where the data lives

The archive lives where **fitdata**'s own config says: `data_dir` (default `~/.local/share/fitdata`).
`mes` and the dashboards find the same folder through one line in `~/.config/mes/config.toml`:

```toml
[projects.fitdata]
data_dir = "~/fitness/data"     # the same folder as fitdata's data_dir
```

No symbolic links are needed. `fitdata garmin status` prints the exact lines for this machine.

## Running under my-easy-software on a server

On a server set up with `mes server plan` (`mes guide server`), the project runs as its own
account, `mes-fitdata`, and keeps its data in `/var/lib/mes/fitdata/data` (the data folder). The
jobs are system units (`mes-fitdata.job.fitdata-garmin.<job>`), and agents reach the project
through mes's gate. Everything below is for `storage = "files"`, the default. A bucket archive
goes back to plain files first (`fitdata garmin migrate-storage --to files`).

**Only one of the two ways at a time.** The standalone user timers (`fitdata-*.timer`) and the
mes units must never both run. Both would sync with the same saved Garmin login, and their locks
live in different folders, so neither would see the other. If the machine runs **fitdata**
standalone, turn its timers off first, and stop the mes job timers while you move the data:

```sh
systemctl --user disable --now fitdata-activities.timer fitdata-daily.timer fitdata-backfill.timer
systemctl --user list-timers 'fitdata-*'                       # lists nothing
sudo systemctl stop 'mes-fitdata.job.fitdata-garmin.*.timer'
```

**Moving a standalone archive in.** Copy the archive (`garmin/`) and the saved login (`.state/`)
as the project's account, never as root. Leave out the index (`fitdata.sqlite`), the lock files and
the logs: the mes jobs build their own index from the files. Your account reads the standalone
`data_dir`, and `mes-fitdata` writes the data folder:

```sh
tar -C <standalone data_dir> --exclude='*.lock' --exclude='*.tmp' --hard-dereference -cf - garmin .state \
  | sudo -u mes-fitdata sh -c 'umask 077 && exec tar -C /var/lib/mes/fitdata/data --no-same-owner --no-same-permissions -xf -'
```

Then check. Each of the first three commands prints nothing, and the `stat` line shows
`mes-fitdata 700` and `mes-fitdata 600`:

```sh
sudo -u mes-fitdata find /var/lib/mes/fitdata/data ! -user mes-fitdata          # all the account's
sudo -u mes-fitdata find /var/lib/mes/fitdata/data -perm /077                    # all owner-only
sudo -u mes-fitdata find /var/lib/mes/fitdata/data -type l -o -type f -links +1  # no links
sudo -u mes-fitdata stat -c '%U %a %n' /var/lib/mes/fitdata/data/.state /var/lib/mes/fitdata/data/.state/garmin-tokens.json
mes server check fitdata --project-dir <clone>
```

A link that `find` names was in the standalone folder. Remove it (**fitdata** would skip it anyway).
Then start the timers again: `sudo systemctl start 'mes-fitdata.job.fitdata-garmin.*.timer'`. The
first job builds the index. Once the mes jobs are working, delete the saved login from the
standalone folder (`rm <standalone data_dir>/.state/garmin-tokens.json`), so only one copy of it
exists.

**`fitdata garmin login` on the server.** Signing in needs a person at a terminal (email,
password, 2FA code), so neither a job nor the gate ever runs it (it is an `admin` command). A job
that finds no saved login, or a damaged one, exits 6. The owner runs the login once, with sudo, as
the project's account, in a short-lived sandboxed unit. Run it again only when a job reports exit 6:

```sh
sudo systemd-run --pty --wait --collect --uid=mes-fitdata --gid=mes-fitdata \
    --working-directory=/var/lib/mes/fitdata/code/current/project \
    -p UMask=0077 -p NoNewPrivileges=yes -p CapabilityBoundingSet= -p ProtectSystem=strict \
    -p ProtectHome=yes -p PrivateTmp=yes -p ReadWritePaths=/var/lib/mes/fitdata/data \
    -E MES_PROJECT=fitdata -E MES_DATA_DIR=/var/lib/mes/fitdata/data -E HOME=/var/lib/mes/fitdata/state \
    -E FITDATA_CONFIG=/nonexistent -E FITDATA_REMOTE= -E LANG=C.UTF-8 -E PYTHONDONTWRITEBYTECODE=1 \
    /var/lib/mes/fitdata/code/current/project/.venv/bin/fitdata-garmin --local login
```

The unit can write only the data folder, and the login is saved in its `.state/`, owner-only. It
can reach the network (Garmin's sign-in pages), but nothing else of the system.

**Agents use mes's gate, not fitdata's remote mode.** `[remote]` in **fitdata**'s config and
`scripts/fitdata-ssh-gate` (above, "Limiting what an SSH key may do") are the standalone way.
They are not used with mes. Under mes, an agent gets its own read or write key (`--key
read=agent-read.pub` in `mes server plan`) and sends requests through the gate
(`mes guide gate`), for example:

```sh
ssh mes-fitdata@my-server.example.ts.net '["fitdata", "garmin", "days", "--days", "7", "--json"]'
```

If the machine runs **fitdata** standalone, remove that setup too: delete the `fitdata-ssh-gate`
lines from your own account's `~/.ssh/authorized_keys` on the server, and the
`~/.local/bin/fitdata-ssh-gate` link, and delete the `[remote]` section from
`~/.config/fitdata/config.toml` on every other machine. Otherwise those machines keep reading the
standalone folder through your account.

**Exit 75 from a job** means temporary: another sync was running, or Garmin was limiting
requests or could not be reached. Nothing is wrong, and the next run carries on. mes's job units
may list such a run as failed.

**Backups.** `mes backup` (`sudo -u mes-fitdata mes backup fitdata --server-root /var/lib/mes`)
copies the whole data folder, and that includes the saved Garmin login
(`.state/garmin-tokens.json`). mes has no setting to leave a file out. Treat every backup as
holding your Garmin sign-in: keep it private (the archive is made mode 0600), and only on disks
you trust (encrypted if it leaves home). If a backup may have been seen by someone else, change
your Garmin password and run the login again (above). After a restore, run the login again if
the jobs exit 6.

## Storage

Set `storage` in **fitdata**'s config:

- `files` (default): plain files and folders under `data_dir/garmin/` (see Layout).
- `bucket`: the same files, at the same keys (`garmin/days/2026/2026-11-14/day.json`...), kept
  in a **[my-bucket-storage](https://github.com/josep-reyero/my-bucket-storage)** bucket: compressed
  with zstd, identical content stored once, small files packed into one SQLite file. Needs
  `fitdata[bucket]`.

```toml
storage = "bucket"
[bucket]
store = "~/fitness/data/store"   # default: <data_dir>/store
name = "archive"                 # default
```

The index, the logs and `.state/` (the saved Garmin login) stay plain files in either mode; the
login never goes into a bucket. Read commands open the bucket read-only. Recent days are
rewritten several times a day, and a bucket keeps each overwritten version in its trash for 7
days; run `mybucket --store <store> gc` now and then (for example weekly) to give that space back.
(`fitdata[bucket]` does not put the `mybucket` command on your PATH: install **[my-bucket-storage](https://github.com/josep-reyero/my-bucket-storage)**
as a tool of its own, or run it with **fitdata**'s Python as `python -m mybucket`.)

To move an archive from one to the other, use `migrate-storage` instead of editing the setting by
hand:

```sh
fitdata garmin migrate-storage --to bucket   # copy, check every object's SHA-256, then switch the config
fitdata garmin migrate-storage --to files    # and back
```

It takes the sync lock, copies every object, reads each one back from the copy and compares its
SHA-256 with the original, and only then writes `storage = "..."` into the config (and reads the
config back to make sure the switch took effect). It never deletes the copy it moved away from: it
prints the command that does, for when you are sure, and only when every file there was copied
and checked. Before copying from plain files, it looks at every entry on disk: a symbolic link, a
hard link, or a name the archive does not use (such as a leftover `.tmp` file) stops it with exit 5
and a list of them, because the copy would not match. If any object does not match, nothing is
switched (exit 5). Running it again is safe.

Measured on a synthetic archive (`uv run --extra bucket python bench/storage_bench.py`; made-up
data shaped like Garmin's sources, no real data; Apple M1 Pro laptop, APFS, backports.zstd):

```
fitdata storage benchmark (synthetic archive, seed 20261116, Python 3.13.2)
------------------------------------------------------------------------------
Archive                                300 days, 188 activities, 13,808 files (60 s to make)
Logical bytes                          510.1 MiB
Plain files on disk (st_blocks)        550.9 MiB
Bucket on disk (st_blocks)             74.9 MiB  (103 files)
Saving                                 86 % less disk (7.35x)
migrate-storage --to bucket            268 s for 13,808 objects, all verified
fitdata index                          0.9 s on files, 0.1 s on the bucket
activity ID --records (bucket)         9 ms; day DATE --source sleep: 1 ms
```

Synthetic data is more regular than a real archive, so read this as an upper bound: real FIT
files and GPS tracks compress less. Most of the saving comes from the large, repetitive JSON
(activity details, the sleep and intraday series). The copy is slow because every object is
compressed at zstd level 19 and written in its own transaction; it runs once.

## Layout

```
<data_dir>/garmin/days/YYYY/YYYY-MM-DD/
    day.json                        manifest + key numbers for the day
    raw/wellness/*.fit              watch's all-day FIT files
    processed/<source>.json         Garmin's processed data
    activities/<HHMM>-<type>-<id>/
        activity.json               what the activity is: type, name, times, totals, training effect
        raw/*.fit                   original activity file
        processed/<source>.json     summary, details, splits, zones, weather, gear, ...
        derived/records.csv         per-sample records; laps.csv; session.json
<data_dir>/garmin/account/          profile, devices, zones, gear, personal records
<data_dir>/fitdata.sqlite           the project's shared database: the index (views days and
                                    activities over days__rows and activities__rows; garmin_files)
<data_dir>/.state/                  login token and sync state
<data_dir>/store/                   only with storage = "bucket": the bucket holding garmin/...
```


## Related projects

This is one piece of a small family of projects that work together or on their own:

- **[my-easy-software](https://github.com/josep-reyero/my-easy-software)**: the framework: create, run, deploy and share small personal projects with the `mes` command.
- **[my-easy-ui](https://github.com/josep-reyero/my-easy-ui)**: dashboards: plain description files a person or their AI agent can reshape, served privately.
- **[my-bucket-storage](https://github.com/josep-reyero/my-bucket-storage)**: storage: a small object store with no server process that takes little disk space.

## Licence

MIT. Not affiliated with Garmin.
