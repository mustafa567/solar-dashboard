# AGENTS.md

Working notes for AI agents (and humans) on this repo. Read this before making
changes; update it when you change something it describes.

## What this is

A personal solar monitoring dashboard for a single household. It reads a
SunPower **PVS6** gateway over its **local** API (no cloud, no auth, no
multi-user) using the [`pypvs`](https://github.com/SunStrong-Management/pypvs)
library, stores every reading in SQLite, and serves a React dashboard that
mimics the SunStrong Connect app's UX.

It runs 24/7 on an always-on **Windows** PC.

## The one constraint that shapes everything

**The PVS6 has no historical data endpoint.** It only reports *current*
readings. All history in this app exists because the poller wrote it down. That
means:

- The poller must never die. A crash is a permanent, unrecoverable hole in the
  data. Every failure path in `poller.py` is caught and retried with backoff.
- kWh figures are **integrated from power samples**, never read from the
  gateway. See `rollup.py`.
- Deleting `data/solar.db` destroys history that cannot be re-fetched. Never do
  it. `hourly_rollup` is a derived cache and *is* safe to drop and rebuild.

## Layout

```
backend/
  app/
    config.py        Settings from env + .env (hand-rolled parser, no dotenv dep)
    models.py        Reading, EnergyTotals, Bucket, HourlyRollup
    store.py         ReadingStore ABC + create_store() factory  <-- the seam
    sqlite_store.py  THE ONLY module with SQL or `import sqlite3`
    pvs_client.py    pypvs wrapper -> one normalised Reading
    calibration.py   GRID_SCALE math, shared by live reads and history rewrite
    discovery.py     finds the gateway when its DHCP lease moves it
    poller.py        the sampling loop + backoff + PollerStatus
    rollup.py        power -> energy integration and bucket windows (pure)
    rollup_job.py    keeps the hourly_rollup table in step with readings
    scheduler.py     PeriodicTask: run a coroutine on an interval
    backup.py        daily SQLite snapshot + rotation
    live.py          flow arrows + status sentence for the home view
    main.py          FastAPI app, routes, static frontend mount
  scripts/           run-backend.bat, NSSM/Task Scheduler installers,
                     seed_demo_data.py, import_*.py, recalibrate.py
  tests/             pytest; fake gateway, temp DB, no network
frontend/
  src/
    App.jsx          view switch + polling wiring; AnalyzeView is lazy-loaded
    lib/api.js       every fetch; same-origin only
    lib/chartTheme.js  the VALIDATED chart palette (see rule 8)
    hooks/           usePolledResource, useResizeKey, useTheme,
                     useRoute (hash routes: #/now, #/analyze/<range>/<date>, #/system)
    components/      TopBar, NowView, FlowDiagram, AnalyzeView, SystemView,
                     DayChart, PeriodBars, ChartParts, States, ErrorBoundary
data/                solar.db, demo.db and data/backups/ (gitignored)
logs/                NSSM service logs (gitignored)
```

## Rules that matter

### 1. Storage abstraction is load-bearing

SQLite is going to be swapped for **Azure SQL** later for backup/remote access.
The whole point of `ReadingStore` is that the migration is one new file plus a
line in `create_store()`.

- **Never** import `sqlite3` or write SQL outside `sqlite_store.py`.
- **Never** let a SQLite-specific type leak through the `ReadingStore` API; it
  speaks in `Reading` / `HourlyRollup` / `StoreStats` only.
- The interface is `async` so a natively-async driver can implement it directly.
  The SQLite implementation wraps blocking calls in `asyncio.to_thread`.

### 2. The PVS serial is a credential

The local API password is **the last 5 characters of the PVS serial number**.
So the full serial must never appear in an API response or a log line. Use
`config.mask_serial()`. There is a test asserting this (`test_poller.py`).

### 3. Sign conventions, fixed once

- `grid_kw` is signed like the gateway's `/sys/livedata/net_p`:
  **positive = importing** from the grid, **negative = exporting**.
- `grid_direction` is the derived label (`import` / `export` / `idle`) stored
  alongside it so nothing downstream re-derives the sign.
- Site energy balance for this PV-only system: `home = solar + grid`.

### 4. Timestamps

Everything is stored as **UTC epoch seconds**. Day/week/month/year boundaries
are computed in the **configured local timezone**, because "today" is a local
concept. Weeks are **Monday-start** (ISO).

### 5. Two read paths into the gateway

`pvs_client.py` tries them in order, and records which one worked in
`Reading.source`:

1. `/sys/livedata/*` -- `pv_p`, `site_load_p`, `net_p`, already in kW.
2. `/sys/devices/meter/*` -- `p3phsumKw` per meter. Used when livedata reads
   back `N/A`, which happens until the telemetry subsystem is enabled.

Field names and units came from the pypvs repo's `doc/LocalAPI.md` and
`doc/varserver-variables-public-pvs6.csv`. **Verify against those files before
adding a new field** rather than guessing.

### 6. Rollups: two paths, one set of math

- `range=day` integrates **raw samples** (exact, <= 1440 rows).
- `range=week|month|year` sums the **`hourly_rollup`** table. A year of 60s
  samples is ~525,000 rows, which is why this table exists.
- Both go through `rollup.py`, so they cannot disagree. There is a test
  asserting the two paths agree for the same day.
- An interval longer than `MAX_SAMPLE_GAP_SECONDS` contributes **no energy** --
  an outage must not integrate as hours of flat power.
- A missing `hourly_rollup` row means "no data", not "zero kWh". The job skips
  hours the poller never observed so the table stays sparse across outages.

### 7. The frontend never re-derives backend decisions

`/api/live` already returns `flows`, `grid_direction` and `status_line`. The UI
renders those; it does not recompute the source mix in JavaScript. Same for
history: the backend returns per-bucket `has_data`, so the chart knows a gap
from a zero without guessing.

### 8. Chart colours are validated, not chosen by eye

`frontend/src/lib/chartTheme.js` holds three series colours that were snapped
onto the dark lightness band and checked with the dataviz validator against the
`#0F1829` chart surface. They are intentionally *different* from the bright UI
accent colours in `index.css`.

The brief asks for a neutral for home usage. A true neutral fails two checks: it
has no chroma (reads grey) and it collides with the teal for deuteranopia
(measured dE 3.9 — indistinguishable). Home is therefore the least-saturated
slot that passes: a cool slate-violet. **If you change any series colour, re-run
the validator** rather than eyeballing it, and keep the mark types different
(area / area / line) so identity is never colour-alone.

### 9. This gateway's net CT reads 2x high -- and so do two of its counters

Measured from the real PVS6: `pv_p` 6.04 kW, `net_p` -10.70 kW, and therefore
`site_load_p` -4.65 kW. A negative house load is impossible. Re-deriving load as
`solar + net / k` over 28h of samples, only **k = 2** makes it independent of
solar (corr -0.04) with equal day and night baselines (0.81 / 0.80 kW); any
k < 1.85 still goes negative. `GRID_SCALE=0.5` is the correction. README
"Measuring the error" has the table.

What follows from that, and must not be "simplified" away:

- **Only `pv_en` is a clean counter.** `site_load_en` and `net_en` are
  accumulated from the same net channel: `site_load_en` runs *backwards* while
  exporting. An earlier version of this file called them correct because they
  matched SunPower's monthly report -- but SunPower builds the report from them.
  Still capture all three on every `Reading` (history cannot be re-fetched);
  scaled by `GRID_SCALE`, their deltas are exact energy.
- **Every stored row records the `grid_scale` it was taken under** (schema v5),
  on `readings` and `daily_import`. `grid_kw / grid_scale` is the raw gateway
  value, so `scripts/recalibrate.py` can re-express history exactly under a new
  scale. Changing `GRID_SCALE` without running it leaves history mixed. Never
  drop the column or store corrected values without it.
- All scale arithmetic lives in `calibration.py`. House load is always
  re-derived as `solar + corrected net`, because the gateway computed its own
  `site_load_p` from the uncorrected net.
- When a reading is still impossible, `normalise()` shows the magnitude (as the
  SunStrong app does) and sets `implausible` so the UI warns. Do not silently
  clamp without the flag. The SunStrong app will disagree once corrected.

### 10. The gateway's IP moves

It is on Wi-Fi with a DHCP lease. `discovery.py` resolves a chain -- cached
last-good, `PVS_HOST`, mDNS (`pvs.local`), then ARP by the gateway's MAC (learned
on first connect) -- and caches the winner in `data/gateway-location.json`.
A poller pinned to one IP loses data permanently, so never reduce this to a
single host lookup.

### 11. Dependencies stay minimal

Single-household personal tool. Backend runtime deps are `fastapi`, `uvicorn`,
`pypvs`, `aiohttp`, `tzdata`. Do not add more without a real reason. `tzdata`
is there because Windows ships no IANA timezone database, so `zoneinfo` cannot
resolve `TIMEZONE=America/...` without it.

## Commands

```bash
# Backend (from the project root)
.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --port 8000

# Frontend (from frontend/)
npm install
npm run dev      # Vite dev server on 5173, proxies /api to 8000
npm run build    # -> frontend/dist, served by FastAPI at /
```

## Testing conventions

- `pytest.ini` sets `asyncio_mode = auto`; async tests need no decorator.
- Tests **never** touch a real gateway or a real database. Inject a fake via
  `app.state.gateway_client` (the seam `lifespan` reads) and use `tmp_path`.
- `conftest.TEST_DAY` is deliberately a **past** date: the fake poller writes a
  sample dated *today*, so seeding "today" would skew the row counts.
- `make_settings()` uses `192.0.2.10` (RFC 5737) so a leaked real connection
  attempt cannot reach anything.

### 12. Imported history is data, but it is not measurement

`daily_import` holds daily totals parsed from SunPower monthly report PDFs, for
the period before the poller existed. Rules:

- It lives in its own table and is never mixed into `readings`.
- `merge_daily_imports()` only fills buckets with **zero** measured coverage.
  Measured data always wins.
- Imported buckets are flagged `imported: true` in the API so the UI can say
  where a number came from.
- The reports give no grid split, so `grid_known` is False for any period
  containing an imported bucket and the grid/percentage totals return None.
  Never report those as 0 -- it reads as a confident, wrong 100%.
- "Energy Used" carries the rule-9 CT fault (negative on heavy-export days).
  `home_kwh_reported` keeps it as printed; `home_kwh` is
  `produced + (used - produced) * grid_scale`, and null only where that is
  still negative. Import production as fact.
- The parser must handle accounting parentheses `(19.42)` = negative, and a
  bare `-` in the Max AC Power column. Missing the hyphen silently dropped
  three days from the 2026 import.

### 13. Two themes, driven by the sun

`useTheme` picks day/night from `live.solar_kw` (clock only as a fallback), with
a manual override in `localStorage` under `solar.theme`. Both palettes live in
`index.css` under `:root[data-theme=...]`, including a **separately validated**
chart triple each -- see rule 8. Components must read colours through the CSS
variables, never hardcode hex, or one theme will silently break.

### 14. The backend must stay platform-neutral

It is moving to a Raspberry Pi. Nothing in `backend/app/` may assume
Windows: paths go through `pathlib`, and the only shell-out is the
neighbour lookup in `discovery.py`, which tries `ip neigh` before `arp -a`
because stock Raspberry Pi OS ships no net-tools. Platform-specific pieces
live in `backend/scripts/` (`.bat` + NSSM for Windows, `.sh` + systemd for
Linux) and nowhere else.

`verify_db.py` fingerprints table *contents*, not the file, so a copy can
be proved lossless across platforms. Never replace it with a file hash --
two healthy SQLite copies legitimately differ byte-for-byte.

## Frontend gotchas

- **Recharts animation is disabled on every mark** (`isAnimationActive={false}`).
  This view re-polls while open, so animating on each refresh makes the chart
  lurch — and a screenshot or a glance during the 1.5s mount animation shows a
  half-drawn series that looks exactly like missing data. Do not re-enable it.
- **`ResponsiveContainer` is keyed on `useResizeKey()`.** Without it, Recharts
  re-renders the axis at the new width but keeps the old width for the series,
  leaving the data squeezed into part of the frame after a window resize or a
  phone rotation.
- The flow triangle's `viewBox` must cover the label block *below* the base
  nodes (`y + r + 55`), not just the circles, or the kW values are clipped.
- `MIN_WIDTH_PER_CATEGORY` in `PeriodBars` is deliberately small (20px). Any
  larger and a 31-day month forces a horizontal scrollbar on a desktop that
  has room to spare; the container should only scroll on a phone.

- **No `manualChunks` in `vite.config.js`.** A manual `charts` chunk became a
  static dependency of the entry (it absorbed shared modules), so Recharts was
  preloaded on the live view after all. The lazy `AnalyzeView` import is what
  splits it now.
- **`DayChart`'s Y domain is set explicitly.** Recharts defaults to `[0, auto]`,
  which silently clips the export area drawn below zero.
- **The System view's coverage strip is the early warning for data loss.**
  Hours before `storage.first_timestamp` are "before recording", not
  "missing", and the running hour is allowed a poll interval of lag -- without
  both it cries wolf and stops being read.

## Gotchas found the hard way

- `PVS.discover()` runs *before* a password is set, and some firmware serves
  `/sys/info/*` unauthenticated while some does not. `pvs_client.connect()`
  therefore seeds the password from `PVS_SN` first and lets the gateway's own
  reported serial win afterwards.
- The PVS6 serves HTTPS with a self-signed cert. `pypvs` passes `ssl=False` per
  request, so a plain `aiohttp.ClientSession` is correct -- don't add a custom
  SSL context.
- `pypvs`'s FCGI client joins POST params without `&`, so only **one** query
  parameter per request actually works.
- `/sys/livedata/*` values can be the literal string `N/A`; `PVSLiveData`
  parses that to `None`, which is what triggers the meters fallback.
- `Bucket.totals` are rounded to 4 dp, so exact-float test assertions need an
  `abs=` tolerance.

## Change log

- **2026-09-28** -- UI production pass. Added a System view over the existing
  `/api/status` (health verdict, 48-hour recording-coverage strip, poller,
  gateway, storage, backups, jobs); the header's status dot opens it. Views
  and Analyze periods live in the URL hash, so refresh, bookmarks and the back
  button work. Error boundary per view; Analyze keyboard shortcuts
  (left/right, T, D/M/Y); grid export drawn below zero on the day chart in the
  validated teal (dashed mark, no new colour); live polling follows the
  backend's `poll_interval_seconds`; history is not fetched while Analyze is
  hidden; Analyze lazy-loaded (first paint ~79 KB gzip, was ~195); favicon,
  `theme-color` following the theme, tab title shows live solar kW.

- **2026-09-28** -- Applying the CT fix on the live host showed that
  `recalibrate.py --dry-run` was not read-only: opening the store ran the v5
  migration against the live database while the old service was still
  writing. `SqliteReadingStore(read_only=True)` now works on a migrated
  in-memory copy and never writes the file; dry runs use it. Also found that
  a blank `TIMEZONE` resolves to the *fixed* UTC offset at startup, not a DST
  zone, so winter month edges were an hour off -- set `TIMEZONE` explicitly.
  128 tests passing.
- **2026-09-28** -- Measured the CT error: net power (and the `site_load_en` /
  `net_en` counters) read 2x high, so `GRID_SCALE=0.5`. Corrected rule 9,
  which wrongly called those counters trustworthy. Schema v5 records the
  `grid_scale` of every reading and imported day, and `daily_import` keeps the
  printed usage in `home_kwh_reported`. Added `calibration.py` and
  `scripts/recalibrate.py` (snapshot, exact rescale, rollup rebuild). The report
  importer now corrects usage instead of dropping negative days: all 39 dropped
  days recover. `/api/export` carries `grid_scale` so merges stay exact.
  127 tests passing.

- **2026-09-27** -- Moved to a new Windows host and installed as an NSSM
  service. Fixed two credential leaks into the logs: the connect line in
  `pvs_client.py` printed the full serial (now masked, with a regression
  test), and `pypvs` logs the gateway session cookie at INFO on every login
  (its logger is now held at WARNING in `main.py`). Fixed
  `install-service-nssm.bat` rejecting `set NSSM=<full path>`: `where`
  cannot take a path, so a path is now checked with `if exist`. 113 tests
  passing.
- **2026-09-27** -- Prepared for the move to another machine / Raspberry
  Pi: made the neighbour lookup cross-platform (`ip neigh` then `arp -a`),
  added a POSIX launcher and a systemd unit, and added `verify_db.py` to
  prove a copied database is complete. Fixed a corrupted regex in
  `discovery.py` where `\b` had been written as a literal
  backspace byte, silently disabling both the ARP fallback and bare-MAC
  normalisation. 112 tests passing.
- **2026-09-27** -- Imported 243 days of 2026 history (Jan-Aug) from SunPower
  monthly report PDFs into a new `daily_import` table (schema v4), merged into
  the month/year views behind an `imported` flag. Verified the reports' daily
  "Energy Used" column really is household use (it sums exactly to their own
  header total) and that its negative days are the CT fault, not a net-export
  convention. Dropped the Week tab at the user's request. 109 tests passing.
- **2026-09-27** -- Connected to the real gateway (`pvs.local`, was a stale IP).
  Found its net/consumption CT reads ~2x high: `site_load_p` is negative and
  lifetime export exceeds lifetime production. Verified the lifetime counters
  against the SunPower monthly report -- they are correct -- and started storing
  them on every reading (schema v3, with migration). Added `GRID_SCALE`, an
  `implausible` flag surfaced in the UI, and `discovery.py` for the moving IP.
  Added day/night theming driven by live production. 91 tests passing.
- **2026-09-27** -- Frontend built: dark instrument-panel UI, Space Grotesk +
  IBM Plex Mono, live flow triangle whose current speed tracks real kW, Analyze
  view with day/week/month/year. Chart palette validated for colour-vision
  deficiency. Fixed three rendering bugs found by looking at the running app:
  clipped SVG labels, stale chart width on resize, and mount animation being
  mistaken for missing data. Added README with both Windows persistence paths,
  and `seed_demo_data.py` for gateway-free UI work.
- **2026-09-27** -- Backend built: config, storage layer (`ReadingStore` +
  SQLite), gateway client with livedata/meters fallback, poller with backoff,
  hourly rollup job, daily backup, FastAPI routes
  (`/api/live`, `/api/history`, `/api/export`, `/api/status`, `/api/health`,
  `/api/admin/*`), 67 passing tests. Added `tzdata` dep after `ZoneInfo` failed
  on Windows. Fixed a real leak: `/api/status` was returning the full PVS
  serial, i.e. the API password.
