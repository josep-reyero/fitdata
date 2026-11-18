# Fitness dashboards

Web dashboards for the **fitdata** index, shown by
**my-easy-ui** (the `easy-ui` command). Each dashboard is one YAML file. It lists
named read-only SQL queries and a layout of fixed components. The look is `theme.yaml` and two
small stylesheets in `styles/`; there is no JavaScript or HTML here, so you or your assistant can
rewrite a dashboard without risk.

| Page | File | The question it answers |
|---|---|---|
| Today | `dashboards/overview.yaml` (the project's), with the sections of `../apps/fitdata_garmin/dashboards/overview-garmin.yaml` | How ready am I, how did I sleep, how is the day going? Readiness, sleep score, time asleep, Body Battery, HRV and resting heart rate each with Garmin's word for it and an arrow against your usual; stress, steps and intensity minutes against their goals; last night stage by stage, Body Battery, stress and heart rate through the day; your usual measure by measure; the latest activities |
| Sleep | `../apps/fitdata_garmin/dashboards/sleep.yaml` | How long and how well did I sleep? Score, time asleep against need, bed and wake times; the night as a timeline of stages next to what made the score; heart rate, HRV, breathing, Body Battery and stress through the night; stages, sleep window, time asleep against need and score per night; night vitals; nights and naps |
| Stress and energy | `../apps/fitdata_garmin/dashboards/stress.yaml` | How stressful was the day? Stress and Body Battery through the day, time in each stress level (one colour per level), what charged and drained the Body Battery; the levels, average stress, charge and drain, and Body Battery at wake within the day's range, day by day |
| Heart and HRV | `../apps/fitdata_garmin/dashboards/heart.yaml` | Are my heart and HRV where they usually are? Heart rate through the day, HRV through the night; resting heart rate with its 7- and 28-day averages; overnight HRV inside Garmin's baseline band; the day's lowest and highest; the last two weeks |
| Activity | `../apps/fitdata_garmin/dashboards/activity.yaml` | How much did I move? Steps, floors and intensity minutes against their goals (drawn as goal lines), the period's averages over the days recorded so far, calories, active and sitting time |
| Training | `../apps/fitdata_garmin/dashboards/training.yaml` | Am I ready to train, and is my load right? Readiness and its factors, acute load inside its optimal range, load focus by kind, VO2 max, endurance and hill scores, training time per week and by sport, race predictions, the period's activities; without activities, one line saying what will appear |
| Body | `../apps/fitdata_garmin/dashboards/body.yaml` | Weight, body fat, fitness age, water against its goal and heat acclimation; each part appears once something is logged, and without anything, one line saying where it comes from |
| Trends | `../apps/fitdata_garmin/dashboards/trends.yaml` | Any of 24 measures day by day or as weekly or monthly averages (with the lowest-to-highest band) over 90 days to everything, next to a second measure |

**Who owns which page.** Today belongs to the project: it holds the Day control and the day
shown, and two anchors (`{anchor: readings}`, `{anchor: trends}`) where each app adds its own
sections. fitdata-garmin adds the Garmin readings and charts there (its `[ui.contributes.overview]`
in `app.toml`; the queries of a section are named after the app, `garmin_...`). The other pages
come with fitdata-garmin (`[ui] dashboards` in its `app.toml`). `easy-ui compose` shows the pages
as they are put together, and `easy-ui validate`, `preview` and `publish` put them together the
same way. To change a page without editing the app, give the project its own copy:
`dashboards/<slug>.yaml` here replaces the app's page of that name, and
`sections/fitdata-garmin.overview.yaml` (with `spec: easy-ui/sections-v1`) replaces what the app
adds to Today (`sections: []` removes it).

**Controls.** The day pages have a **Day** control (a date, today by default) and most have a
**Period**, whose shortest choice is the default (2 weeks; 30 days on Training, where load builds
over weeks; 90 days on Body, where weigh-ins are far apart). A day that has not synced yet shows
the most recent day with data, so a late sync never shows an empty page; Today says which day it
shows in one quiet line under its control. Trends has its own controls: the measure, the one next
to it, each day or week or month averages, and how far back from the latest day. Days without a
reading are left out of averages and show as gaps, never as zero.

**Few days of data.** The pages are made for an account that has just started as much as for
years of data. Charts span the days there are, not the whole period. Every arrow compares a
reading with **your usual**: the average of the days before it (up to 28) that have one, and only
once there are 3 of them; until then the readings stand alone. Averages say how many days they
are made of ("Over 4 days", "3 days so far"), a 7-day average line appears once 3 of its 7 days
have a reading and a 28-day one once 14 do. A panel that needs data the archive does not have yet
(weigh-ins, race predictions, naps, an HRV baseline, activities) is left out of the page
(`hide_empty: true`), and Training without activities and Body without anything logged say in
one line what will appear there and how.

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
stage lanes, `bands` the stress levels and `load-focus` the kinds of training load.
`styles/20-look.css` sets the figures and the notes, and a few classes: `duration` keeps a
duration figure ("7 h 41 min") on one line sized to its tile, `dayline` turns a stat into the
quiet line that says which day a page shows, and `empty-note` into the friendly line of a page
with nothing yet. It sets no widths: easy-ui lays the rows out for every screen, from a phone to a
wide monitor. Group related readings in one row (two or three charts side by side) and let them
wrap; each number keeps a one-line caption, and the explanations go in the notes at the end of
the page.

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
density (`easy-ui screenshot` does that by default): a laptop (1440 px) in light and dark, phones
(390 px), a wide screen (2560 px) and the new watch, shown in the project's README. Take new ones
from the samples only, never from a real index, for example
`easy-ui screenshot sleep --fixture --width 1440 --height 1100 --out ui/screenshots/sleep.png`.

The file ends in `.db` because `.gitignore` refuses every `*.sqlite`, so a real index is never
committed by mistake. Never put a real index in `fixtures/`.

The tests check both samples against **fitdata**, put the pages together the way easy-ui does and
run every query on both (`uv run python -m pytest tests/test_dashboards.py`): the panels that need
more data must be empty for the new watch, its pages must say what will appear, and no arrow may
show without a usual. With **my-easy-ui** added, they also run `easy-ui compose`, `data build` and
`validate` (with no warnings) on both samples:

```sh
uv run --with-editable <path to my-easy-ui> python -m pytest tests/test_dashboards.py
```

Without **my-easy-ui** installed (or with one that does not compose pages), those three are skipped.
