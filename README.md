# Solar Dashboard

A personal solar monitoring dashboard for a SunPower **PVS6** gateway. It polls
the gateway's **local** API (via [`pypvs`](https://github.com/SunStrong-Management/pypvs)),
stores every reading in SQLite, and serves a React dashboard that mirrors the
core UX of the SunStrong Connect app.

Everything runs on one always-on Windows PC. No cloud, no accounts, no auth.

---

## The thing to understand first

**The PVS6 has no historical data endpoint.** It only reports what is happening
*right now*. Every chart in this app exists because the poller wrote readings
down, minute after minute.

Two consequences:

- **The service should stay running.** Time it is stopped is time that is
  permanently missing from your history. That is why the Windows service setup
  below matters more than it would for a normal web app.
- **`data/solar.db` is irreplaceable.** It cannot be re-downloaded from the
  gateway. Back it up (the app does this daily on its own) and never delete it.

---

## Quick start

Two commands, once dependencies are installed.

```bat
REM 1. Backend: poller + API + rollups + backups, all in one process
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000

REM 2. Frontend dev server (only needed while working on the UI)
cd frontend && npm run dev
```

- Dev: <http://localhost:5173> (Vite, proxies `/api` to port 8000)
- Production: <http://localhost:8000> (FastAPI serves the built frontend too)

### First-time setup

```bat
REM Python 3.11+ required. Verify with:  python --version
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

REM Configure the gateway
copy .env.example .env
notepad .env

REM Build the frontend so FastAPI can serve it
cd frontend
npm install
npm run build
cd ..
```

Then start the backend and open <http://localhost:8000>.

Other devices on your LAN can reach it at `http://<this-pc-ip>:8000` as long as
`API_HOST=0.0.0.0` and Windows Firewall allows the port:

```bat
netsh advfirewall firewall add rule name="Solar Dashboard" dir=in action=allow protocol=TCP localport=8000
```

---

## Configuration

All settings live in `.env` in the project root. See `.env.example` for the
annotated list. The ones you must set:

| Setting | What it is |
|---|---|
| `PVS_HOST` | `pvs.local` — the gateway's mDNS name. See below for why not an IP |
| `PVS_SN` | Full serial from the gateway label, e.g. `ZT999999999999A9999` |

**The last 5 characters of `PVS_SN` are the local API password.** The app derives
it automatically, never logs it, and masks the serial in `/api/status`. Keep
`.env` out of version control (it already is).

### The gateway's IP moves, and that is handled

This PVS6 is on Wi-Fi (`active_interface = sta0`) with a DHCP lease, so its
address changes. A poller pinned to one IP would silently stop recording, and
missed readings can never be recovered — so the host is resolved through a chain
of candidates on every connect:

1. The last address that actually worked (remembered in `data/gateway-location.json`).
2. Whatever `PVS_HOST` says — an IP or a name.
3. mDNS: `pvs.local`, `pvs6.local`, `pvs5.local`.
4. The ARP table, matched on the gateway's MAC, which is learned automatically
   on first connect.

`pvs.local` resolves correctly here, which is why it is the configured default.
When the app finds the gateway somewhere other than `PVS_HOST` it logs where,
and carries on.

**The permanent fix is a DHCP reservation for the PVS on your router** — pin
`<the PVS's MAC address>` to a fixed address and none of the above has to do any work.

Worth knowing:

| Setting | Default | Notes |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | 30-60 is the sensible range |
| `DB_PATH` | `data/solar.db` | Relative paths resolve from the project root |
| `MAX_SAMPLE_GAP_SECONDS` | `300` | Gaps longer than this contribute no energy, so an outage is not integrated as hours of flat power |
| `TIMEZONE` | system | IANA name for day/month boundaries; blank uses this PC's zone |
| `METER_CONSUMPTION_MODE` | `net` | Only used on the meters fallback. See troubleshooting |
| `GRID_SCALE` | `1.0` | Multiplier for the grid/net power channel. See the calibration note below |
| `BACKUP_KEEP_DAYS` | `14` | How many daily snapshots to retain |

---

## Running persistently

On Linux, use the systemd unit at `backend/scripts/solar-dashboard.service`
(covered in the migration section below). On Windows, pick **one** of these. Option A is what you want for a machine that should keep
recording whether or not anyone is logged in.

### Option A — Windows Service via NSSM (recommended)

A real service: starts at boot before login, survives logout, and restarts
itself if the process dies.

**1. Install NSSM.** Download from <https://nssm.cc/download>, unzip, and either
put `win64\nssm.exe` on your `PATH` or note its full path.

**2. Run the installer from an elevated Command Prompt** (Start → type `cmd` →
Ctrl+Shift+Enter):

```bat
cd /d D:\repos\solar-dashboard
REM If nssm is not on PATH:
REM set NSSM=C:\tools\nssm\win64\nssm.exe
backend\scripts\install-service-nssm.bat
```

That script creates the service and configures it for you. For reference, these
are the commands it runs:

```bat
nssm install SolarDashboard "D:\repos\solar-dashboard\backend\scripts\run-backend.bat"
nssm set SolarDashboard AppDirectory "D:\repos\solar-dashboard"
nssm set SolarDashboard DisplayName "Solar Dashboard (PVS6 poller and API)"
nssm set SolarDashboard Start SERVICE_AUTO_START

REM Always come back, but do not spin on a hard failure
nssm set SolarDashboard AppExit Default Restart
nssm set SolarDashboard AppRestartDelay 10000
nssm set SolarDashboard AppThrottle 10000

REM Rotating logs so months of uptime cannot fill the disk
nssm set SolarDashboard AppStdout "D:\repos\solar-dashboard\logs\service.out.log"
nssm set SolarDashboard AppStderr "D:\repos\solar-dashboard\logs\service.err.log"
nssm set SolarDashboard AppRotateFiles 1
nssm set SolarDashboard AppRotateOnline 1
nssm set SolarDashboard AppRotateSeconds 86400
nssm set SolarDashboard AppRotateBytes 10485760

REM Give the poller time to finish its write and close the database cleanly
nssm set SolarDashboard AppStopMethodConsole 15000

REM Do not start polling before the network is up
nssm set SolarDashboard DependOnService Tcpip Dnscache

nssm start SolarDashboard
```

**Managing it:**

```bat
nssm status  SolarDashboard      REM SERVICE_RUNNING when healthy
nssm restart SolarDashboard
nssm stop    SolarDashboard
nssm edit    SolarDashboard      REM GUI for every setting
sc query     SolarDashboard      REM built-in Windows alternative
```

**Removing it** (leaves `data\solar.db` alone):

```bat
backend\scripts\uninstall-service-nssm.bat
```

### Option B — Task Scheduler (simpler, weaker)

No extra software, but it starts at **logon** rather than at boot, and logging
out stops the poller. Use it only if you would rather not install NSSM.

```bat
backend\scripts\install-task-scheduler.bat
```

which runs:

```bat
schtasks /Create /TN "SolarDashboard" ^
    /TR "\"D:\repos\solar-dashboard\backend\scripts\run-backend.bat\"" ^
    /SC ONLOGON /RL HIGHEST /F
```

Managing it:

```bat
schtasks /Run    /TN "SolarDashboard"
schtasks /End    /TN "SolarDashboard"
schtasks /Query  /TN "SolarDashboard" /V /FO LIST
schtasks /Delete /TN "SolarDashboard" /F
```

### Checking health, logs and status

```bat
REM Is it alive?
curl http://localhost:8000/api/health

REM Full picture: poller state, last error, row counts, jobs, backups
curl http://localhost:8000/api/status

REM Service logs (NSSM)
type logs\service.out.log
powershell -Command "Get-Content logs\service.err.log -Tail 50 -Wait"
```

`/api/status` is the one to look at when something seems wrong. It reports
`poller.consecutive_failures`, `poller.last_error` (with the underlying network
cause), when the last reading landed, and how many rows are stored.

---

## Known issue: your consumption CT reads high

Your gateway reports numbers that cannot all be true at once. Measured directly
from it at midday:

```
/sys/livedata/pv_p          6.04 kW    solar production
/sys/livedata/net_p       -10.70 kW    net grid power (negative = exporting)
/sys/livedata/site_load_p  -4.65 kW    house load  <-- impossible
```

The PVS6 computes `site_load_p` as `pv_p + net_p`, so an over-reading net CT
drives the house load below zero. Three independent checks agree that the
net/consumption channel is the problem, not the app:

| Check | Value | Verdict |
|---|---|---|
| `site_load_p` | −4.5 to −4.65 kW on every sample | A house cannot consume negative power |
| Lifetime export vs production | 73,972 kWh exported vs 59,567 kWh ever generated | You cannot export more than you make |
| `ctSclFctr` | production `50`, consumption `200` | A 2× mismatch between the two meters |

**The SunStrong Connect app has the same bad data** — it shows "4.5 kW HOME
USAGE", which is the magnitude of that same negative number, and its own three
figures do not balance either (5.9 kW of solar cannot power a 4.5 kW house
*and* export 10.4 kW). This dashboard shows the same value so the two agree,
but flags it instead of hiding it: you get a "Meter readings disagree" banner,
`implausible_readings` on `/api/status`, and a warning in the service log.

**The lifetime counters carry the same fault.** `pv_en` is fine, but
`site_load_en` and `net_en` are accumulated from the same net channel:
`site_load_en` runs *backwards* by ~4 kWh every hour of heavy export. They match
the SunPower monthly report only because SunPower builds the report from them
too, which is also why the report prints negative "Energy Used" days.

### Measuring the error

28 hours of real samples (Sep 27–28), with house load re-derived as
`solar + net / k` for a range of divisors `k`:

| k | corr(solar, home) | lowest home | median home, day / night |
|---|---|---|---|
| 1 (as reported) | −0.76 | −5.13 kW | −3.35 / 1.60 kW |
| 1.75 | −0.30 | −0.32 kW | 0.19 / 0.91 kW |
| **2** | **−0.04** | **0.31 kW** | **0.81 / 0.80 kW** |
| 2.5 | +0.47 | 0.25 kW | 1.67 / 0.64 kW |

A house does not use less power because the sun is out. Only `k = 2` makes the
load independent of production, with the same baseline by day as by night. Any
`k` below 1.85 still gives a negative load somewhere. So the net CT reads **2×
high**, and `GRID_SCALE=0.5` corrects it.

### Fixing it

1. Set `GRID_SCALE=0.5` in `.env` and stop the service.
2. Correct the history already recorded, then start the service again:

   ```
   .venv/Scripts/python.exe backend/scripts/recalibrate.py --dry-run
   .venv/Scripts/python.exe backend/scripts/recalibrate.py
   ```

   Every reading records the scale it was stored under, so this is exact and
   reversible: it snapshots the database first, recovers the raw net power,
   re-derives the house load, re-corrects the imported report days, and rebuilds
   the hourly rollup. Re-running it is a no-op.
3. Re-import the monthly report PDFs once (see below) to recover the days the
   old importer dropped as negative.
4. Cross-check against your utility's smart-meter portal: its daily net export
   should match the corrected figure, not the doubled one. The printed rating on
   the consumption CT clamps against `ctSclFctr = 200` is the other confirmation.

After this the SunStrong Connect app will disagree with this dashboard, because
it still shows the uncorrected figures.

---

## Importing history from the monthly reports

The gateway has no history, so anything from before the poller started can only
come from SunPower's **Residential Monthly Performance Report** PDFs. Each has
one row per day: energy produced, energy used, and max AC power.

```bat
REM Preview without writing
.venv\Scripts\python.exe backend\scripts\import_reports.py --dry-run "%USERPROFILE%\Downloads\Resi*.pdf"

REM Import, limited to this year
.venv\Scripts\python.exe backend\scripts\import_reports.py --since 2026-01-01 "%USERPROFILE%\Downloads\Resi*.pdf"
```

Needs `pypdf` (in `requirements-dev.txt`). Re-importing a month replaces it, so
running it again with updated reports is safe. The usage column is corrected
with `GRID_SCALE` from `.env` (or `--grid-scale`), so set that first; see the
CT section above.

**What this does and does not give you**

- Imported days fill the **month and year** views. They cannot fill the day
  view, which is hourly — there is no intraday detail in the reports. Opening
  such a day shows its totals instead of a chart.
- **Measured readings always win.** An import only fills a bucket the poller
  never covered, so live data is never overwritten.
- The reports give no import/export split, so for imported periods "From grid"
  and "Self-consumption" show `--` rather than a misleading zero.
- **Reported usage is corrected with `GRID_SCALE`.** That column carries the same
  miscalibrated CT described above — SunPower derives household use as
  production plus net grid, so an overstated export drags it below zero. The
  printed value is kept as `home_kwh_reported`, and the usable figure is
  `produced + (used − produced) × GRID_SCALE`. A day still negative after that
  is stored without a usage figure. Production is unaffected.

The 2026 import covered 243 days (Jan–Aug). As printed, 39 days had impossible
negative usage. Corrected at `GRID_SCALE=0.5`, every day is usable:

| Month | Solar | Use as printed (days dropped) | Use corrected |
|---|---|---|---|
| Jan | 521.4 | 2,435.5 | 1,478.4 |
| Feb | 648.2 | 1,541.9 (1) | 1,093.8 |
| Mar | 832.7 | 1,216.4 (6) | 1,011.0 |
| Apr | 927.6 | 1,114.6 (8) | 978.1 |
| May | 1,204.0 | 557.7 (20) | 734.9 |
| Jun | 1,012.2 | 1,693.0 (4) | 1,306.5 |
| Jul | 1,122.0 | 2,761.1 | 1,941.5 |
| Aug | 914.4 | 1,916.7 | 1,415.6 |

Production totals match each report's own header figure exactly.

---

## Moving to another machine (including a Raspberry Pi)

Your reading history cannot be re-fetched from the gateway, so the move is built
around *proving* nothing was lost rather than assuming it.

**What actually has to move:**

| Path | Move it? |
|---|---|
| `data/solar.db` | **Yes — this is the irreplaceable part** |
| `.env` | Yes (it holds your PVS_SN) |
| the repo itself | Yes (or `git clone` it) |
| `data/gateway-location.json` | Optional — it re-learns on first connect |
| `frontend/dist/` | Optional — copy it to avoid needing Node, or rebuild it |
| `.venv/`, `node_modules/` | **No** — always rebuild these on the target |

SQLite files are byte-portable across Windows, Linux and ARM, so `solar.db`
moves from a Windows PC to a Pi as-is. There is no export/import step.

### What the new machine actually needs

| Requirement | Detail |
|---|---|
| **Python 3.10+** | Nothing newer is required; 3.11–3.14 all work. Only 3.10+ syntax is used |
| **On the same LAN as the PVS6** | It must be able to reach the gateway directly. Same subnet is simplest |
| **mDNS resolution** | For `pvs.local`. Built into Windows 10/11 and macOS; `avahi-daemon` on Linux |
| **~50 MB disk + room to grow** | The database grows roughly 15–20 MB per year at a 60s poll |
| **Node 18+** | **Only** if you want to rebuild the frontend — see below |

**Internet is not needed to run it.** Only to `pip install` the first time. The
page pulls its two fonts from Google Fonts, which simply falls back to system
fonts if the machine is offline.

**You can skip Node entirely.** `frontend/dist/` is 684 KB of static files with
no machine-specific paths in it — copy the folder across and FastAPI serves it
as-is. You only need Node if you plan to change the UI.

So the minimum on a second Windows PC is: Python, the repo, `.env`,
`data/solar.db`, `frontend/dist/`, and `pip install -r backend/requirements.txt`.

### The one mistake that loses data

**Do not copy `data/solar.db` on its own while the service is running.**

SQLite runs in WAL mode here, so recent writes live in a `solar.db-wal` sidecar
until they are checkpointed. On a running system the split can look like this:

```
data/solar.db            4,096 bytes     <- almost nothing
data/solar.db-wal    2,484,392 bytes     <- all your readings
```

Copying just the `.db` gives you an empty database. `verify_db.py` detects this
and says so, but it is easier to avoid:

- **Stop the service first** (that checkpoints the WAL into the `.db`), then
  copy — this is the simplest path; or
- **copy a file from `data/backups/`** instead. Those are written through
  SQLite's online backup API and are always self-contained, so they are safe to
  copy even while the poller is writing; or
- copy `solar.db`, `solar.db-wal` **and** `solar.db-shm` together.

Whichever you choose, run `verify_db.py` on both machines and compare the
`COMBINED` fingerprint before decommissioning the old one.


### The procedure

**1. Take a verified snapshot on the old machine.**

```bat
REM A consistent copy, safe even while the poller is running
curl -X POST http://localhost:8000/api/admin/backup

REM Fingerprint it - note the COMBINED line
.venv\Scripts\python.exe backend\scripts\verify_db.py data\solar.db
```

**2. Stop the service**, so nothing writes after the snapshot:

```bat
nssm stop SolarDashboard
```

Then copy `data\solar.db` and `.env` across.

Order matters here -- see *The one mistake that loses data* above. Copying
the `.db` while the service runs can give you an empty file.

**3. Set it up on the new machine.**

```bash
git clone <your repo> ~/solar-dashboard && cd ~/solar-dashboard
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt

mkdir -p data
cp /media/usb/solar.db data/solar.db
cp /media/usb/.env .env
```

**4. Prove nothing was lost** — this is the step that makes the move safe:

```bash
python3 backend/scripts/verify_db.py data/solar.db
```

The `COMBINED` fingerprint must match what the old machine printed. It hashes
the row *contents* of every table, not the file, so two healthy copies agree
even though their files differ byte-for-byte. A mismatch means stop and re-copy.

**5. Build the frontend and start the service.**

```bash
# Needs Node. Or copy frontend/dist/ from the old machine and skip this.
cd frontend && npm install && npm run build && cd ..

sudo cp backend/scripts/solar-dashboard.service /etc/systemd/system/
sudo nano /etc/systemd/system/solar-dashboard.service   # set User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now solar-dashboard

systemctl status solar-dashboard
journalctl -u solar-dashboard -f
```

**6. Decommission the old one.** Only once step 4 passes and the new machine is
recording (check `/api/status` shows `success_count` climbing), remove the old
service, so two pollers are not filling two separate databases:

```bat
backend\scripts\uninstall-service-nssm.bat
```

### The gap while you switch

Between stopping the old service and starting the new one, nothing is recorded,
and that hole is permanent. Keep it short: set the target up and test it
*before* stopping the source. Readings are keyed by timestamp with
insert-or-replace, so a brief overlap where both run is harmless — a long
silence is not.

### Raspberry Pi notes

- Use **64-bit Raspberry Pi OS**. Prebuilt wheels for `aiohttp` and
  `pydantic-core` exist for `aarch64`, so `pip install` does not have to
  compile anything.
- Nothing in the backend is Windows-specific. Host discovery uses `ip neigh`
  where `arp` is unavailable, which is the case on a stock Pi.
- A 60-second poll is ~1,440 small writes a day. That is fine for a while, but
  for multi-year running consider putting `data/` on an external SSD or USB
  stick rather than the SD card.
- `pvs.local` needs mDNS. Raspberry Pi OS ships Avahi, so it works out of the
  box; `sudo apt install avahi-daemon` if name resolution fails.
- Set the timezone (`sudo raspi-config` → Localisation), since day and month
  boundaries are computed in local time.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/live` | Current snapshot: kW per node, flow directions, status line, today's totals |
| `GET /api/history?range=day\|week\|month\|year&date=YYYY-MM-DD` | Aggregated series plus period totals (the UI offers day/month/year; `week` still works) |
| `GET /api/export?from=...&to=...&format=json\|csv` | Raw readings, for the Azure SQL migration |
| `GET /api/status` | Service health and configuration |
| `GET /api/health` | Cheap liveness probe; never touches the gateway |
| `POST /api/admin/rollup?full=true` | Rebuild the hourly rollup cache |
| `POST /api/admin/backup` | Take a backup now |

`date` accepts any date inside the window you want. `from`/`to` accept an ISO
date or datetime; naive values are read as local time.

Interactive docs are at <http://localhost:8000/docs>.

```bat
REM Export a month as CSV
curl -o january.csv "http://localhost:8000/api/export?from=2026-01-01&to=2026-02-01&format=csv"
```

---

## How it works

```
PVS6 gateway  --(pypvs, local HTTPS)-->  poller  -->  ReadingStore  -->  solar.db
                                                            |
                                    rollup job -->  hourly_rollup (derived cache)
                                                            |
                                          FastAPI  -->  /api/*  +  the React app
```

One process runs the poller, the rollup job, the daily backup and the HTTP API,
so there is exactly one service to install and watch.

### Storage is deliberately abstracted — for the Azure SQL move

`backend/app/sqlite_store.py` is **the only module in the project that contains
SQL or imports `sqlite3`.** Everything else talks to the `ReadingStore`
interface in `backend/app/store.py`, which speaks only in domain objects
(`Reading`, `HourlyRollup`, `StoreStats`).

That is not incidental tidiness — it is the whole point. When you migrate to
Azure SQL for backup and remote access, the change is:

1. Add `backend/app/azuresql_store.py` implementing the same `ReadingStore`
   methods.
2. Return it from `create_store()` in `store.py`.

No other module changes. The interface is already `async`, so an async driver
can implement it directly. Use `GET /api/export` to move existing history across
in one pass.

### Energy figures are integrated, not read

The gateway reports instantaneous power only, so every kWh in the app is
integrated from stored samples (trapezoidal, in `rollup.py`). Two paths share
that same math:

- **Day view** integrates raw samples directly — exact, and at most 1440 rows.
- **Week / month / year** sum the `hourly_rollup` table, because a year at 60s
  polling is ~525,000 raw rows. The job refreshes it every 5 minutes and is
  idempotent; the table is a cache and can be rebuilt from `readings` at any
  time with `POST /api/admin/rollup?full=true`.

An interval longer than `MAX_SAMPLE_GAP_SECONDS` contributes zero energy, so a
service restart does not show up as hours of imaginary generation. Hours the
poller never saw are stored as *absent*, not as zero, so the UI can honestly say
"no data" rather than "0 kWh".

### Backups

Once a day (and once at startup) the app writes `data/backups/solar-backup-<timestamp>.db`
using SQLite's online backup API, which is safe while the poller is writing —
unlike a plain file copy, which can catch a torn write-ahead log. The newest
`BACKUP_KEEP_DAYS` snapshots are kept and older ones deleted.

This is a stopgap until the data lives in Azure SQL. It protects against
corruption and mistakes, not against the disk failing — copy `data\backups\`
somewhere else periodically if that matters to you.

---

## Development

```bat
REM Install test dependencies and run the suite
.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q

REM Frontend
cd frontend
npm run dev        REM localhost:5173, /api proxied to 8000
npm run build      REM -> frontend/dist
```

Tests never touch a real gateway or a real database: they inject a fake gateway
through `app.state.gateway_client` and run against a temp SQLite file.

### Working on the UI without a gateway

```bat
.venv\Scripts\python.exe backend\scripts\seed_demo_data.py
set DB_PATH=data\demo.db
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
```

That writes ~70 days of plausible readings to `data/demo.db` — cloudy days,
evening cooking peaks, appliance spikes — so every history view has something
realistic to show. It writes to a **separate file** and will refuse to touch a
database that already holds readings unless you pass `--force`, so it cannot
damage real history.

### Day and night

The dashboard switches between a light "sky" theme and a dark instrument panel
based on whether the array is actually producing, so it brightens at dawn and
dims at dusk. The control in the top bar cycles **auto → day → night**; a dot
under the icon means it is following the sun. The choice is remembered per
browser in `localStorage`.

`AGENTS.md` documents the architecture rules and the gotchas found while
building this.

---

## Troubleshooting

**The gateway moved to a new IP.**
Nothing to do — the app tries the last known address, `PVS_HOST`, the mDNS names
and the ARP table in turn, and logs where it found it. If you want it to stop
moving, add a DHCP reservation on your router for `<the PVS's MAC address>`.

**The dashboard says the gateway is unreachable.**
Check `/api/status` → `poller.last_error`; it includes the underlying network
error. Then:

```bat
ping 192.168.4.10
curl -k https://192.168.4.10/
```

The PVS6's LAN port must be connected and on the same subnet. Note that its
installer/Wi-Fi interface and its LAN interface can have different addresses —
`PVS_HOST` must be the one your PC can reach.

**It connects but authentication fails.**
`PVS_SN` must be the full serial exactly as printed on the label; the password
is its last 5 characters. A typo there looks like an auth failure, not a config
error.

**"Meter readings disagree" banner, or the three numbers do not add up.**
See the calibration section above. Your consumption CT reads high; energy totals
are still correct.

**Numbers look inverted — "home usage" tracks grid import, or the arrows point the wrong way.**
This only affects the meters fallback (`source: "meters"` in `/api/live`). Your
consumption CTs are probably wired load-side rather than measuring net flow. Set
`METER_CONSUMPTION_MODE=load` in `.env` and restart.

**Solar shows 0 all day but the panels are producing.**
Check `/api/live` → `source`. If it says `meters`, the `/sys/livedata` block is
not being populated on your firmware and the app fell back to the meters, which
is fine. If production is genuinely missing, confirm the production meter
appears under `/sys/devices/meter/` on the gateway.

**A history range says "no data yet".**
The poller was not running then. History only exists from the moment the
service first started — there is nothing to backfill from.

**The service will not start.**
Check `logs\service.err.log`. The usual causes are a missing `.venv` (the
launcher says so explicitly), a missing `.env`, or port 8000 already in use
(`netstat -ano | findstr :8000`).

---

## Dependencies

Kept deliberately small: a single-household tool with no auth and no multi-user
concerns does not need a framework stack.

**Backend:** `fastapi`, `uvicorn`, `pypvs`, `aiohttp`, `tzdata`.
(`tzdata` is required because Windows ships no IANA timezone database, so
`zoneinfo` cannot resolve `TIMEZONE=America/...` without it.)

**Frontend:** `react`, `recharts`, built with `vite` and `tailwindcss`.
