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
fitdata --json sql "SELECT ... FROM days|activities|files ..."   # read-only, 1000 rows unless --limit N
fitdata --json describe                         # every command, its kind (read/write/admin) and arguments
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
- **Rows:** `fitdata sql` returns at most 1000 rows by default and says on stderr when there are
  more; aggregate in SQL or pass `--limit N` rather than pulling everything.
- **Exit codes:** 0 ok, 1 error, 2 usage (fix the command), 3 not found, 4 not allowed (your
  `MES_ALLOW` level is too low: ask the person), 5 refused for safety, 6 needs a person (with
  `--json`, stdout has a `needs_human` result: show its `action`, for example "run `fitdata
  login`", then `rerun`), 75 temporary (Garmin rate limit or a sync running: try later).
- **Kinds:** query commands are `read`; `sync`, `backfill`, `index`, `migrate-storage` change
  data (`write`); `login` is `admin` and always needs the person at a terminal.
- **Privacy:** never copy personal data into this repo, issues, or commits.

## Working on the code

- fitdata is a mes project: [`project.toml`](project.toml) names the app (`[apps.fitdata]`),
  its three jobs (`[jobs.*]`, kept in step with `systemd/`, which is the standalone way to run
  them; `tests/test_project.py` checks they match) and what the dashboards may read (`[ui]`).
  fitdata does not import mes; it answers `describe` in the same shape.
- Layout: `src/fitdata/`.
  - `cli.py`: commands, `--json`, SSH forwarding, MES_ALLOW check, exit codes.
  - `describe.py`: command kinds, `describe`, MES_ALLOW. A new command needs a kind here.
  - `errors.py`: error classes and their exit codes.
  - `config.py`: the per-machine TOML config (`data_dir`, `storage`, `[bucket]`, `[remote]`).
  - `paths.py`: archive layout, as Paths and as storage keys.
  - `storage.py`: the one interface to the archive files (plain files or a bucket); key checks.
  - `migrate.py`: `migrate-storage` (copy, verify by SHA-256, switch, never delete).
  - `endpoints.py`: every Garmin source fetched, with descriptions.
  - `sync.py`: downloading and manifests.
  - `fit.py`: FIT decoding with Garmin's SDK.
  - `index.py`: the SQLite index.
  - `query.py`: read-only queries.
- **Archive access goes through `storage.archive()`** (read-only) or `storage.archive(write=True)`,
  by key (`garmin/days/...`), never by building a path under data_dir. Keys are checked in
  `storage.check_key`; nothing can name `.state/`, and the saved login never enters a bucket.
- **No personal data in the repo:** no names, emails, hostnames, paths of a real machine,
  tokens or sample data from a real account. Machine specifics belong in
  `~/.config/fitdata/config.toml`.
- **The archive (files or bucket) is the source of truth.** The SQLite index is derived;
  `fitdata index` rebuilds it.
  Never store anything only in the index.
- **Adding a Garmin source:** add one entry to `endpoints.py` (fetcher plus description). Sync,
  manifests and the CLI pick it up automatically.
- **Be gentle with Garmin:** keep throttling (`client.THROTTLE`) and the backoff on rate limits.
- **Test** against a scratch `FITDATA_DATA_DIR` (with `FITDATA_REMOTE=""` and `FITDATA_CONFIG`
  pointing at a temporary file) before touching a real archive. `uv run python -m pytest` runs
  everything that needs no extras; `uv run --extra bucket python -m pytest` adds the bucket
  tests; `tests/fakegarmin.py` stands in for Garmin (made-up data, real FIT files).
