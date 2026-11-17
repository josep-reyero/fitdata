# Fitness dashboards

Web dashboards for the **fitdata** index, shown by
**my-easy-ui** (the `easy-ui` command). Each dashboard is one YAML file. It lists
named read-only SQL queries and a layout of fixed components. There is no JavaScript, HTML or
CSS here, so you or your assistant can rewrite a dashboard without risk.

| Page | File | What it shows |
|---|---|---|
| Today | `dashboards/overview.yaml` (the project's), with the sections of `../apps/fitdata_garmin/dashboards/overview-garmin.yaml` | Latest steps, resting heart rate, sleep score, overnight HRV, Body Battery high and Training Readiness, each next to the change against your average over the days before; trends for sleep score, resting heart rate, HRV and steps |
| Sleep and recovery | `../apps/fitdata_garmin/dashboards/sleep.yaml` | Time asleep and sleep score, HRV, resting heart rate, the Body Battery range, and a table of the last nights |
| Activities | `../apps/fitdata_garmin/dashboards/activities.yaml` | Count, distance, time and climbing for the period; distance and time per week by sport; recent activities with pace, heart rate and training effect |
| Trends | `../apps/fitdata_garmin/dashboards/trends.yaml` | Weekly averages over the long run: recovery, sleep, steps, training time and load, VO2 max |

**Who owns which page.** Today belongs to the project: it holds the Days control and the latest
day, and two anchors (`{anchor: readings}`, `{anchor: trends}`) where each app adds its own
sections. fitdata-garmin adds the Garmin readings and charts there (its `[ui.contributes.overview]`
in `app.toml`; the queries of a section are named after the app, `garmin_...`). Sleep, Activities
and Trends come with fitdata-garmin (`[ui] dashboards` in its `app.toml`). `easy-ui compose`
shows the pages as they are put together, and `easy-ui validate`, `preview` and `publish` put them
together the same way. To change a page without editing the app, give the project its own copy:
`dashboards/<slug>.yaml` here replaces the app's page of that name, and
`sections/fitdata-garmin.overview.yaml` (with `spec: easy-ui/sections-v1`) replaces what the app
adds to Today (`sections: []` removes it).

Every page has a **Days** control (7, 30, 90 or 365 days). The period ends on the most recent
day the watch synced, not on the calendar date, so a late sync never shows an empty page.
Days without a reading are left out of averages and show as gaps or dashes, never as zero.

The dashboards read the project's shared views `days` and `activities` (one row per day and per
activity, from every app of the project), so a second app's days and activities show up on these
pages without changing them. Only those two views of the index are copied for the dashboards: the
tables behind them (`days__rows`, `activities__rows`) and fitdata-garmin's `garmin_files` stay out.

## Try it with the sample data

`fixtures/fitdata-sample.db` is a small made-up index: 120 days and 34 activities with the same
tables, views and columns as a real `fitdata.sqlite`. It includes the gaps a real archive has: days
without the watch, a missing day, nights without sleep or HRV data and a sick week. None of it
comes from a real watch. (It ends in `.db` because the repository's `.gitignore` keeps every
`*.sqlite` file out of git; `easy-ui preview` reads `.sqlite`, `.sqlite3` and `.db` files in
`ui/fixtures/`.)

From the repository folder:

```sh
easy-ui preview --fixture   # look at the pages with the sample data; nothing is published
```

It prints a one-time link to open on this computer, and shows each saved change within a couple
of seconds. To try the whole journey (data build, publish, sign-in) with the sample data, use
throwaway folders:

```sh
export MES_DATA_HOME="$(mktemp -d)" EASYUI_STATE_HOME="$(mktemp -d)" MES_CONFIG="$(mktemp -d)/none.toml"
mkdir -p "$MES_DATA_HOME/fitdata"
cp ui/fixtures/fitdata-sample.db "$MES_DATA_HOME/fitdata/fitdata.sqlite"
easy-ui data build      # copy the allowed tables into the dashboard data
easy-ui validate        # check every dashboard against that data
easy-ui publish         # make them the version the server shows
easy-ui set-password    # you type a password (12 characters or more)
easy-ui serve           # http://127.0.0.1:8765, until you press Ctrl-C
```

## Use it with your own data

**fitdata** keeps its archive where its own config says: `data_dir` in
`~/.config/fitdata/config.toml` (by default `~/.local/share/fitdata`). That stays the one place
the location is decided. Tell mes and easy-ui about the same folder with one table in
`~/.config/mes/config.toml` (or `$MES_CONFIG`):

```toml
[projects.fitdata]
data_dir = "~/fitness/data"     # exactly fitdata's data_dir
```

`fitdata garmin status` prints these lines with this machine's folder filled in. easy-ui then reads the
index at `<data_dir>/fitdata.sqlite` (every project's shared database is `<project>.sqlite` in its
data folder). No symbolic link is needed, and nothing is moved. Without the table, easy-ui looks in the mes default,
`~/.local/share/mes/fitdata/`, which is not where **fitdata** writes.

The index is the same whether **fitdata** stores its files as plain files or in a bucket
(`storage` in **fitdata**'s config), so the dashboards do not change with it.

easy-ui only reads the index; it never changes your archive. Every `fitdata garmin sync` (and
`fitdata garmin index`) rebuilds the index, so run `easy-ui data build` after it to show the new data.
If nothing changed, the build keeps the current data and open pages do not reload. Run as a mes
project, this happens by itself: `[ui] refresh_after` lists the three jobs
(`fitdata-garmin.activities`, `.daily`, `.backfill`), so mes rebuilds the dashboard data after each
successful run.

## Change a dashboard

1. Edit the YAML (see `easy-ui components` for every component and format, and `easy-ui tables`
   for the columns you can use). A page of the app is in `../apps/fitdata_garmin/dashboards/`; to
   keep the app as it is, copy the page into `dashboards/` here first and edit the copy.
2. Run `easy-ui validate`. It names the file, the line and what to change.
3. Look at it with `easy-ui preview`, then `easy-ui publish`. If a published change was a
   mistake, `easy-ui rollback` shows the previous version again (add `--restore-files` to also
   put its files back in `dashboards/`).

Tips that keep the numbers honest:

- Filter on `has_data = 1` when averaging: on a day without the watch, Garmin still returns zeros
  for steps and calories.
- Use `nullif(steps, 0)` and `nullif(sleep_seconds, 0)` so a missing reading is not averaged as 0.
- Sleep, HRV and Training Readiness for a night are stored on the morning's date.
- Sleep score, HRV status, Body Battery, Training Readiness, training effect, training load and
  VO2 max are Garmin's own calculations. Say so on the page.
- Give a stat with a change `better: higher` or `better: lower` (resting heart rate: lower), so
  a change for the worse does not look like good news.
- Times in seconds use `format: duration`, which reads "7 h 07 min", never like a clock time.
- The menu order is `[ui] dashboards` in `../project.toml`; the first one opens at the start.
- Read `days` and `activities`, never `days__rows` or `activities__rows`: the views are what every
  app shares (`source` says which app a row came from).

## Sample data and tests

The sample is made by `fixtures/make_fixture.py`. It writes a fake archive into a temporary folder
and runs fitdata-garmin's own indexer on it (through `apps/fitdata_garmin/schema.sql`), so its
tables, views and columns always match the real index:

```sh
uv run python ui/fixtures/make_fixture.py
```

The file ends in `.db` because `.gitignore` refuses every `*.sqlite`, so a real index is never
committed by mistake. Never put a real index in `fixtures/`.

The tests check the sample against **fitdata**, put the pages together the way easy-ui does and run
every query on the sample (`uv run python -m pytest tests/test_dashboards.py`). With **my-easy-ui**
added, they also run `easy-ui compose`, `data build` and `validate`:

```sh
uv run --with-editable <path to my-easy-ui> python -m pytest tests/test_dashboards.py
```

Without **my-easy-ui** installed (or with one that does not compose pages), those three are skipped.
