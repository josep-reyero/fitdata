# Fitness dashboards

Web dashboards for the **fitdata** index, shown by
**my-easy-ui** (the `easy-ui` command). Each dashboard is one YAML file. It lists
named read-only SQL queries and a layout of fixed components. The look is `theme.yaml` and two
small stylesheets in `styles/`; there is no JavaScript or HTML here, so you or your assistant can
rewrite a dashboard without risk.

Four pages, each answering one question, the most important thing first:

| Page | File | The question it answers |
|---|---|---|
| Today | `dashboards/overview.yaml` (the project's), with the sections of `../apps/fitdata_garmin/dashboards/overview-garmin.yaml` | How is my day? Sleep score, time asleep, overnight HRV and resting heart rate; training readiness, Body Battery, stress and steps, each with an arrow against your usual; last night stage by stage, Body Battery and stress, and heart rate through the day |
| Sleep | `../apps/fitdata_garmin/dashboards/sleep.yaml` | How did I sleep? Score, time asleep against need, bed and wake times against your usual; last night as a timeline of stages next to what made the score; stages per night against your sleep need, the score with its 7-night average, and when you slept |
| Health | `../apps/fitdata_garmin/dashboards/health.yaml` | Is my body where it usually is? Stress, Body Battery at wake, resting heart rate and HRV (and weight and fitness age once there are some); Body Battery and stress through the day and by day; resting heart rate with its 7-day average, HRV inside Garmin's baseline band, and weight |
| Workouts | `../apps/fitdata_garmin/dashboards/workouts.yaml` | How is training going? Readiness, training status, the last 7 days' load against its optimal range and VO2 max; the recent workouts (date, sport, time, distance, average heart rate, Garmin's effect and load); time per week, by sport, and load against its range; steps per day and intensity minutes per week against their goals |

**Who owns which page.** Today belongs to the project: it holds the Day control and the day
shown, and two anchors (`{anchor: readings}`, `{anchor: charts}`) where each app adds its own
sections. fitdata-garmin adds its headline readings and the charts of the night and the day there
(its `[ui.contributes.overview]` in `app.toml`; the queries of a section are named after the app,
`garmin_...`). The other pages come with fitdata-garmin (`[ui] dashboards` in its `app.toml`).
`easy-ui compose` shows the pages as they are put together, and `easy-ui validate`, `preview` and
`publish` put them together the same way. To change a page without editing the app, give the
project its own copy: `dashboards/<slug>.yaml` here replaces the app's page of that name, and
`sections/fitdata-garmin.overview.yaml` (with `spec: easy-ui/sections-v1`) replaces what the app
adds to Today (`sections: []` removes it).

**Controls.** Every page has a **Day** control (a date, today by default). Sleep, Health and
Workouts also have a **Period**, how far back their charts go, from 2 weeks to a year; its
shortest choice is the default (30 days on Workouts, where load builds over weeks). A day that has
not synced yet shows the most recent day with data, so a late sync never shows an empty page;
Today says which day it shows in one quiet line under its control. Days without a reading are left
out of averages and show as gaps, never as zero.

**Few days of data.** The pages are made for an account that has just started as much as for
years of data. Charts span the days there are, not the whole period. Every arrow compares a
reading with **your usual**: the average of the days before it (up to 28) that have one, and only
once there are 3 of them; until then the readings stand alone. A 7-day average line appears once 3
of its 7 days have a reading. A panel that needs data the archive does not have yet (weigh-ins,
fitness age, an HRV baseline, workouts, training status) is left out of the page
(`hide_empty: true`), and Workouts without any workout says in one line what will appear there and
how, while the training numbers Garmin already has (readiness after the first nights) still show.

**Data.** The dashboards read the project's shared views `days` and `activities` (one row per
day and per activity, from every app of the project), so a second app's days and activities show
up on these pages without changing them, and fitdata-garmin's tables listed in its `[ui] tables`:
`garmin_sleep`, `garmin_daily`, `garmin_training`, `garmin_body_battery_events`, `garmin_samples`
(readings through the day and the night as `date`, `series`, `at` (local `HH:MM`), `value`) and
`garmin_sleep_stages`. The tables behind the views (`days__rows`, `activities__rows`) and
`garmin_files` stay out of the dashboard data.

**Look.** `theme.yaml` sets warm paper and ink in light mode and a soft charcoal in dark mode,
with the system's own fonts (nothing is downloaded). `styles/10-families.css` gives each family
of measures one colour, the same on every page: sleep violet, stress amber, energy (Body Battery)
teal, heart red, activity green, training blue, body brown. A component joins a family with its
`class:` (`class: sleep`, `class: [energy, pair-energy-stress]`), which colours the dot before a
reading's name, the short rule over a panel and its chart lines; `hypnogram` colours a night's
stage lanes, `stages` the stages of a stacked bar and `sports` the groups of sports.
`styles/20-look.css` sets the figures and the notes, and a few classes: `duration` keeps a
duration figure ("7 h 41 min") on one line sized to its tile, `dayline` turns a stat into the
quiet line that says which day a page shows, `empty-note` into the friendly line of a page with
nothing yet, and `wide-band` keeps a band that covers much of its chart light. It sets no widths:
easy-ui lays the rows out for every screen, from a phone to a wide monitor. Keep each page short:
a row of up to six readings, each with one short line under it (Garmin's word, a goal, a unit),
at most about five charts, one per question, two or three side by side, and one line of notes at
the end.

## Try it with the sample data

`fixtures/fitdata-sample.db` is a small made-up index (about 2 MB): 120 days and 34 activities
with the same tables, views and columns as a real `fitdata.sqlite`, made from a fake archive with
the files Garmin gives (sleep, stress, Body Battery, HRV, readiness, training status, weigh-ins
and the rest), and readings through the day and night for the last 7 days. It includes the gaps a
real archive has: days without the watch, a missing day, nights without sleep or HRV data, nights
without SpO2, a sick week, empty and broken files. `fixtures/new-watch/fitdata-new-watch.db` is
the same for an account that has just started: the watch put on at 10:40 on the first of 5
days, 4 nights of sleep, no activities, no weigh-ins, no HRV baseline or training status yet.
None of it comes from a real watch. (It ends in `.db` because the repository's `.gitignore` keeps every
`*.sqlite` file out of git; `easy-ui preview` reads `.sqlite`, `.sqlite3` and `.db` files in
`ui/fixtures/`.)

From the repository folder:

```sh
easy-ui preview --fixture   # look at the pages with the sample data; nothing is published
```

It prints a one-time link to open on this computer, and shows each saved change within a couple
of seconds. It uses `fixtures/fitdata-sample.db`; to look at the new watch instead, give the
preview that file as the project's data in throwaway folders (the first three lines below, with
`fixtures/new-watch/fitdata-new-watch.db`, then `easy-ui preview` without `--fixture`). To try the
whole journey (data build, publish, sign-in) with the sample data, use throwaway folders:

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
- Readings through the day (`garmin_samples`, `garmin_sleep_stages`) exist only for the most
  recent `intraday_days` days (60 by default); older days have their daily numbers only. Give
  their panels `hide_empty: true`, so an older day leaves them out.
- The chosen day: `WITH sel AS (SELECT min(:day, (SELECT max(date) FROM days WHERE has_data = 1)) AS d)`
  shows the latest day with data when the chosen one has not synced yet.
- Compare with your usual only once it exists: count the days it is made of and return the
  change only when there are 3 (`CASE WHEN n >= 3 THEN value - usual END`), and say how many
  days an average is over (a stat's `note:`).
- Leave the latest day (usually still in progress) out of averages of steps, calories and floors.
- Sleep score, HRV status, Body Battery, Training Readiness, training effect, training load and
  VO2 max are Garmin's own calculations. Say so on the page.
- Give a stat with a change `better: higher` or `better: lower` (resting heart rate: lower), so
  a change for the worse does not look like good news.
- Garmin's word for a reading (readiness level, HRV status, sleep quality) goes under it as
  `note:`; a goal as `reference:` on its chart; Garmin's ranges (HRV baseline, optimal load) as
  `band:`; averages as `dashed:` lines.
- Times in seconds use `format: duration` ("7 h 07 min", never like a clock time); times of
  day use `format: clock`, with bedtimes counted from the midnight before the night (23:10 is
  83400, 00:30 is 88200) so they average and chart across midnight.
- The menu order is `[ui] dashboards` in `../project.toml`; the first one opens at the start.
- Read `days` and `activities`, never `days__rows` or `activities__rows`: the views are what every
  app shares (`source` says which app a row came from).
- Give every component the `class:` of its family so its colour matches the other pages.

## Sample data and tests

Both samples are made by `fixtures/make_fixture.py`. It writes a fake archive into a temporary
folder and runs fitdata-garmin's own indexer on it (`fitdata garmin index`, keeping 7 days of
readings through the day, with the tables of `apps/fitdata_garmin/schema.sql`), so their tables,
views and columns always match the real index:

```sh
uv run python ui/fixtures/make_fixture.py                       # fixtures/fitdata-sample.db
uv run python ui/fixtures/make_fixture.py --variant new-watch   # fixtures/new-watch/fitdata-new-watch.db
```

`screenshots/` holds pictures of every page made from these samples at twice the screen's
density (`easy-ui screenshot` does that by default): a laptop (1440 px) in light, one in dark,
phones (390 px), a wide screen (2200 px) and the new watch, shown in the project's README. Take
new ones from the samples only, never from a real index, for example
`easy-ui screenshot sleep --fixture --width 1440 --height 1100 --out ui/screenshots/sleep.png`.

The file ends in `.db` because `.gitignore` refuses every `*.sqlite`, so a real index is never
committed by mistake. Never put a real index in `fixtures/`.

The tests check both samples against **fitdata**, put the pages together the way easy-ui does and
run every query on both (`uv run python -m pytest tests/test_dashboards.py`): the panels that need
more data must be empty for the new watch, Workouts must say what will appear, and no arrow may
show without a usual. With **my-easy-ui** added, they also run `easy-ui compose`, `data build` and
`validate` (with no warnings) on both samples:

```sh
uv run --with-editable <path to my-easy-ui> python -m pytest tests/test_dashboards.py
```

Without **my-easy-ui** installed (or with one that does not compose pages), those three are skipped.
