# AGENTS.md

This repo has two parts:
1. **fitdata**, a tool that archives a person's Garmin data.
2. **A CLI that lets an agent act as their fitness assistant**: answer questions about training,
   sleep, recovery and health using their own data.

## Using the data (fitness-assistant mode)

Always use the `fitdata` CLI with `--json`. Don't read archive files directly unless the CLI
can't answer. The CLI works the same whether the archive is local or on another machine over
SSH, because the user's config decides that.

Start broad, then drill down:

```sh
fitdata --json summary --days 7                 # recent days' key numbers + activities
fitdata --json days --days 30 --fields resting_hr,hrv_last_night_avg,sleep_score,training_readiness
fitdata --json activities --days 30 [--type trail_running]
fitdata --json day 2026-11-14                   # overview + which sources have data that day
fitdata --json day 2026-11-14 --source sleep    # full Garmin data for one source
fitdata --json activity <id>                    # what it was + totals + available sources/files
fitdata --json activity <id> --source splits
fitdata --json activity <id> --records --every 10 --columns timestamp,heart_rate,enhanced_speed,enhanced_altitude
fitdata --json sources                          # what each source means
fitdata --json schema                           # index tables/columns, then:
fitdata --json sql "SELECT ... FROM days|activities|files ..."   # read-only
```

Guidelines:
- **Dates:** dates are the user's local calendar days (YYYY-MM-DD). Sleep, HRV and Training
  Readiness for a night are stored on the morning's date.
- **Size:** `--records` can be large (one row per second). Use `--every` and `--columns`.
- **Missing values:** `null` means the watch or Garmin had no value, so don't treat it as zero.
  `fitdata day DATE` lists `sources_with_data`.
- **Garmin's metrics:** numbers like Training Effect, load, VO2 max, readiness and Body Battery
  are Garmin's own calculations. Say so when you use them, and prefer trends over single days.
- **Freshness:** `fitdata status` shows the last sync. Recent days refresh several times a day.
- **Privacy:** never copy personal data into this repo, issues, or commits.

## Working on the code

- Layout: `src/fitdata/`.
  - `cli.py`: commands, `--json`, SSH forwarding.
  - `config.py`: the per-machine TOML config.
  - `paths.py`: archive layout.
  - `endpoints.py`: every Garmin source fetched, with descriptions.
  - `sync.py`: downloading and manifests.
  - `fit.py`: FIT decoding with Garmin's SDK.
  - `index.py`: the SQLite index.
  - `query.py`: read-only queries.
- **No personal data in the repo:** no names, emails, hostnames, paths of a real machine,
  tokens or sample data from a real account. Machine specifics belong in
  `~/.config/fitdata/config.toml`.
- **Files are the source of truth.** The SQLite index is derived; `fitdata index` rebuilds it.
  Never store anything only in the index.
- **Adding a Garmin source:** add one entry to `endpoints.py` (fetcher plus description). Sync,
  manifests and the CLI pick it up automatically.
- **Be gentle with Garmin:** keep throttling (`client.THROTTLE`) and the backoff on rate limits.
- **Test** against a scratch `FITDATA_DATA_DIR` before touching a real archive.
