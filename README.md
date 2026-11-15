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

It uses Garmin Connect's web endpoints through
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect). These are not an
official API and can change. Fetching is throttled to be gentle.

## Install

```sh
uv tool install --editable .          # puts `fitdata` in ~/.local/bin
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Configure

`~/.config/fitdata/config.toml` (or `$FITDATA_CONFIG`). Nothing machine-specific lives in the repo.

On the machine that keeps the archive:

```toml
data_dir = "~/fitness/data"
```

On any other machine, to read that archive over SSH:

```toml
[remote]
host = "my-server"                 # an ssh host or alias
command = "~/.local/bin/fitdata"   # default
```

With a remote set, every command runs on that host (including `login` and `sync`), except
`decode`. Add `--local` to use the current machine instead.

## Collect

```sh
fitdata login             # email, password, 2FA code; saves a token (owner-only) in data_dir/.state
fitdata sync              # last 3 days + their activities
fitdata backfill          # history, newest first, 60 days per run (resumable)
```

To keep it current automatically on Linux (systemd user timers):

```sh
cp systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now fitdata-activities.timer fitdata-daily.timer fitdata-backfill.timer
loginctl enable-linger "$USER"     # keep timers running while logged out
```

The timers do this:

| Timer | Schedule | Job |
|---|---|---|
| `fitdata-activities` | every 10 min | new activities |
| `fitdata-daily` | 3× a day | the last 3 days' health data (sleep, HRV and readiness settle the next morning) |
| `fitdata-backfill` | nightly | 90 more days of history until it's complete |

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
```

Add `--json` for machine-readable output. [`AGENTS.md`](AGENTS.md) explains how an AI agent
should use the CLI.

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
```

## Licence

MIT. Not affiliated with Garmin.
