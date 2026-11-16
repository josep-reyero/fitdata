# fitdata-garmin

Keep your own complete copy of your Garmin data, organised per day, and query it from the
command line, by hand or through an AI agent acting as your fitness assistant.

- **Raw:** the watch's original FIT files: every activity, plus the all-day wellness, sleep,
  HRV and metrics files.
- **Processed:** Garmin Connect's calculations as Garmin returns them, one JSON file per source:
  sleep stages and score, HRV status, Body Battery, stress, Training Readiness and Status,
  VO2 max, race predictions and more.
- **Activities:** one folder each, holding the original FIT, Garmin's analysis (splits, zones,
  weather...) and a per-sample `records.csv` decoded with Garmin's official FIT SDK.
- **Index:** a SQLite file you can rebuild at any time, with one row per day and per activity.
- **CLI:** reads the archive locally, or on another machine over SSH.
- **Dashboards:** web pages over the index, shown by my-easy-ui (see [`ui/README.md`](ui/README.md)).

It uses Garmin Connect's web endpoints through
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect). These are not an
official API and can change. Fetching is throttled to be gentle.

fitdata is a my-easy-software (mes) **project** ([`project.toml`](project.toml)), and it also works on its own, without mes.

## Install

```sh
uv tool install --editable .            # puts `fitdata` in ~/.local/bin
uv tool install --editable '.[bucket]'  # the same, plus the optional bucket storage (see Storage)
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

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

## Collect

```sh
fitdata login             # email, password, 2FA code; saves a token (owner-only) in data_dir/.state
fitdata sync              # last 3 days + their activities
fitdata backfill          # history, newest first, 60 days per run (resumable)
```

It keeps itself current with three jobs:

| Job | Schedule | What it does |
|---|---|---|
| `activities` | every 10 min | new activities (`fitdata activities-sync --days 2`) |
| `daily` | 07:30, 13:30, 22:30 | the last 3 days' health data (`fitdata sync --days 3`); sleep, HRV and readiness settle the next morning |
| `backfill` | 02:15 | 90 more days of history until it's complete (`fitdata backfill --max-days 90`) |

**With mes**, the jobs are `[jobs.*]` in [`project.toml`](project.toml), each with the Garmin
hosts it talks to, a memory cap and a time limit; mes installs and runs them (`mes jobs ...`), and
rebuilds the dashboard data after each one (`[ui] refresh_after`).

**Without mes** (the standalone way), the same jobs are systemd user timers on Linux:

```sh
cp systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now fitdata-activities.timer fitdata-daily.timer fitdata-backfill.timer
loginctl enable-linger "$USER"     # keep timers running while logged out
```

Use one or the other, not both. A run that finds another sync running, or Garmin limiting
requests, exits 75 (temporary); the units count that as success and the next run carries on.

## Query

```sh
fitdata summary --days 7
fitdata days --days 30 --fields resting_hr,hrv_last_night_avg,sleep_score
fitdata activities --type trail --limit 10
fitdata day 2026-11-14 --source sleep
fitdata activity 123456789 --source splits
fitdata activity 123456789 --records --every 10 --columns timestamp,heart_rate,enhanced_altitude
fitdata sql "SELECT date, steps, resting_hr FROM days ORDER BY date DESC LIMIT 14"
fitdata sources     # what each source contains
fitdata schema      # index tables and columns
fitdata status      # archive overview: days, activities, last sync, login, storage
```

`fitdata sql` runs one read-only `SELECT` on the index (days, activities, files). It returns at
most 1000 rows unless you pass `--limit N`, and says on stderr when there are more; a query may
run for 30 seconds at most.

Add `--json` for machine-readable output. [`AGENTS.md`](AGENTS.md) explains how an AI agent
should use the CLI.

## Commands, kinds and exit codes

`fitdata describe` lists every command with its **kind**, and `fitdata --json describe` gives
the same as data (with every argument), in the shape every mes app uses. mes jobs and the mes
gate read it to know what each command may do.

| Kind | Commands |
|---|---|
| read | `summary`, `days`, `activities`, `day`, `activity`, `sources`, `schema`, `sql`, `status`, `decode`, `describe`, `guide` |
| write | `sync`, `activities-sync`, `backfill`, `account`, `index`, `migrate-storage` |
| admin | `login` |

`MES_ALLOW=read|write|admin` sets the highest kind this session may run. Unset means admin (you
at your own computer); set but empty or unknown allows nothing. A command above it exits 4
before it starts, and before anything is sent to a remote. `decode --csv` writes files, so it
needs write; `decode` is never run for a request that came through the mes gate.

| Exit | Meaning |
|---|---|
| 0 | ok |
| 1 | error (for example a config value fitdata cannot use) |
| 2 | usage: the command line or a value in it was not understood |
| 3 | not found: no such day, activity, source, index or bucket |
| 4 | not allowed by `MES_ALLOW` |
| 5 | refused for safety (a forwarded `decode`, a migration copy that did not verify) |
| 6 | needs a person: `fitdata login` (with `--json`, a `needs_human` result on stdout) |
| 75 | temporary: Garmin's rate limit, Garmin unreachable, or another sync running; try later |

With `--json`, errors are one JSON line on stderr: `{"error", "kind", "exit_code", "hint"}`.

## Dashboards

`ui/dashboards/` holds the web pages (Today, Sleep and recovery, Activities, Trends), shown by
my-easy-ui. They read only the `days` and `activities` tables of the index. See
[`ui/README.md`](ui/README.md) for previewing them with made-up sample data and pointing them at
your archive.

## Where the data lives

The archive lives where fitdata's own config says: `data_dir` (default `~/.local/share/fitdata`).
mes and the dashboards find the same folder through one line in `~/.config/mes/config.toml`:

```toml
[projects.fitdata]
data_dir = "~/fitness/data"     # the same folder as fitdata's data_dir
```

No symbolic links are needed. `fitdata status` prints the exact lines for this machine.

## Storage

Set `storage` in fitdata's config:

- `files` (default): plain files and folders under `data_dir/garmin/` (see Layout).
- `bucket`: the same files, at the same keys (`garmin/days/2026/2026-11-14/day.json`...), kept
  in a my-bucket-storage bucket: compressed
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
(`fitdata[bucket]` does not put the `mybucket` command on your PATH: install my-bucket-storage
as a tool of its own, or run it with fitdata's Python as `python -m mybucket`.)

To move an existing archive, use `migrate-storage` instead of editing the setting by hand:

```sh
fitdata migrate-storage --to bucket   # copy, check every object's SHA-256, then switch the config
fitdata migrate-storage --to files    # and back
```

It takes the sync lock, copies every object, reads each one back from the copy and compares its
SHA-256 with the original, and only then writes `storage = "..."` into the config. It never
deletes the old copy: it prints the command that does, for when you are sure. If any object does
not match, nothing is switched (exit 5). Running it again is safe.

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
<data_dir>/fitdata.sqlite           index (days, activities, files)
<data_dir>/.state/                  login token and sync state
<data_dir>/store/                   only with storage = "bucket": the bucket holding garmin/...
```

## Licence

MIT. Not affiliated with Garmin.
