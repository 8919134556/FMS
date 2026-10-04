# FMS Admin Portal

Internal admin/back-office application for a Fleet Management System. Administrators
and operations staff manage users, roles/permissions, and (from Phase 2 onward)
clients, vehicles, drivers, documents, maintenance, trips, and the rest of the
fleet operation from this portal. This is **not** a driver app, customer app, or
public site — see `PROJECT_STATUS.md`-equivalent notes below for what's built.

## Status: Phase 1 of 8

Phase 1 delivers the platform foundation and is fully working end-to-end:

- Project scaffold, settings, Docker/Compose, Celery wiring
- Custom `User` model + RBAC (`Role`, `Permission`, `RolePermission`)
- Session-based auth: login, logout, self-service password change/reset,
  admin-forced password reset
- Full User & Role CRUD portal UI (search, filter, sort, pagination, CSV export,
  activate/deactivate/lock/unlock)
- Immutable, queryable Audit Log (every create/update/lifecycle action + login/logout)
- Dashboard with real (not mocked) charts and stat tiles for the modules that exist
- REST API for Users/Roles/Permissions (DRF) with OpenAPI docs at `/api/docs/`
- `apps/clients`, `apps/vehicles`, `apps/drivers`, etc. are scaffolded as installed
  apps (so the sidebar, settings, and project layout already reflect the full
  system) but carry no models yet — they arrive in Phases 2–7. The sidebar marks
  each of these with the phase that will implement it, rather than linking to a
  page that doesn't exist yet.

## Architecture

```
config/           Django project settings, root URLconf, Celery app
apps/
  core/           Abstract base models, RBAC helpers, dashboard, template tags
  accounts/       Custom User, Role, Permission, RBAC, auth views, User/Role portal UI + API
  audit/          Immutable AuditLog model, logging service, read-only log viewer
  clients/ locations/ vendors/ contracts/     Phase 2 (scaffolded)
  vehicles/ drivers/                          Phase 3 (scaffolded)
  documents/ maintenance/                     Phase 4 (scaffolded)
  tracking/ geofences/ alerts/                Phase 5 (scaffolded)
  routes/ trips/                              Phase 6 (scaffolded)
  notifications/                              Phase 7 (scaffolded)
templates/        Django templates (Bootstrap 5, HTMX)
static/           CSS/JS
```

Each implemented app follows the same internal layering: `models.py` (schema),
`forms.py`/`serializers.py` (validation), `views.py`/`api_views.py` (thin,
delegate to models/services), `permissions.py` (RBAC), `admin.py` (Django admin
registration). Business logic that isn't a one-liner lives in a `services.py`
(see `apps/audit/services.py`), not in views.

RBAC is custom, not Django's built-in groups/permissions: `Role` has a
many-to-many to `Permission` (via `RolePermission`), each `Permission` is a
`(module, action)` pair (e.g. `vehicle.create`). `apps.core.permissions.user_has_permission`
is the single function every view, template, and API endpoint calls to check
access — see `apps/accounts/management/commands/seed_roles.py` for the seeded
role → permission matrix.

## Requirements

- Python 3.12+ (developed/tested against 3.10+ as well)
- PostgreSQL 14+
- Redis 7+ (Celery broker/result backend — no Celery tasks ship until Phase 7,
  but the worker/beat services are wired up now)

## Local setup (without Docker)

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash; use .venv\Scripts\activate on cmd
pip install -r requirements.txt

cp .env.example .env
# edit .env: set SECRET_KEY, DATABASE_URL (Postgres), etc.

python manage.py migrate
python manage.py seed_roles          # seeds the permission catalog + 7 standard roles
python manage.py createsuperuser
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` — you'll be redirected to `/accounts/login/`.
After logging in as the superuser you land on `/dashboard/`.

- Custom admin portal: `/admin/users/`, `/admin/roles/`
- Django's built-in admin (break-glass access): `/django-admin/`
- REST API: `/api/v1/users/`, `/api/v1/roles/`, `/api/v1/permissions/`
- API docs (Swagger UI): `/api/docs/`

## Running Celery

No scheduled/async jobs exist yet (they arrive in Phase 7 with document/insurance
expiry reminders), but the worker and beat processes are fully wired and will
start cleanly:

```bash
celery -A config worker --loglevel=info
celery -A config beat --loglevel=info
```

## Running tests

```bash
pytest
```

34 tests cover models (soft delete, status/is_active sync, RBAC uniqueness
constraints), the permission-check function, and views (auth flow, permission
enforcement, CRUD, lifecycle actions, audit log creation). Uses `pytest-django`
+ `factory_boy`; `conftest.py` overrides the stock `admin_user` fixture because
the custom `User` model has additional required fields.

## Telematics data flow & client isolation

```
Client (clients_client)
  └─ 1..n Vehicle (vehicles_vehicle.client_id)              <- Vehicle master
       └─ 0..1 TrackingDevice (tracking_trackingdevice.vehicle_id, UNIQUE, IMEI)
            └─ Socket server (manage.py run_telematics_server) authenticates by IMEI
                 └─ TelemetryIngestionService.ingest(), on EVERY packet:
                      device -> vehicle -> client re-read from the DB
                      ├─ Raw table   tracking_rawtelemetryevent   (payload + vehicle + client snapshot)
                      ├─ App history tracking_telemetryevent      (normalized + vehicle + client snapshot)
                      └─ App latest  tracking_vehiclecurrenttelemetry (one row per vehicle)
                           └─ Live map / fleet API / dashboards / reports -> scoped to the viewer's client
```

* **Nothing is hardcoded.** Adding a client, a vehicle or a device is data, not code: register the
  client, register vehicles under it (Client page -> Vehicles -> *Add Vehicle*), register the device
  and assign it to the vehicle. The next packet from that IMEI is mapped automatically.
* **History belongs to whoever owned the vehicle at the time.** `client` is stamped on each raw/history
  row at ingest, so moving a vehicle to another client never hands over the previous client's history.
  Data from an unmapped device (or a vehicle with no client) is stored with `client = NULL` and is visible
  to internal staff only.
* **Ownership is never cached.** The socket server keeps a device object per open connection; ingest
  re-reads device/vehicle/client (and status) for each packet, so a re-assigned or suspended device
  takes effect immediately without reconnecting.
* **Client users** (`User.client` set, see Users -> edit) are a hard tier on top of RBAC
  (`apps/core/scoping.py`): every list/detail/API/report/dashboard is filtered to their client,
  they are read-only (`view`/`export` only, whatever the role says), and modules that are not
  client-aware (users, roles, vendors, geofences, audit, settings, dispatch board, master data)
  are refused. New modules are denied to client users until they are added to `CLIENT_SCOPED_MODULES`.
* **Scale.** Per-client reads are single indexed filters (`(client, timestamp)` indexes); the fleet feed
  is a constant number of queries regardless of fleet size; `python manage.py purge_raw_telemetry`
  enforces `TELEMATICS_RAW_EVENT_RETENTION_DAYS` on the raw buffer (schedule it daily). For very large
  volumes, partition `tracking_telemetryevent` by time (PostgreSQL native partitioning or TimescaleDB).

### Feeding FMS from the comms program (Raw DB / App DB)

If devices report to the separate comms program (`main.py` -> `process.py` -> PostgreSQL `COMMSDB` raw
data + `APPDB` `current_table`/`history_table`), FMS does not read those databases directly. The bridge
copies their App DB rows into the FMS tracking tables:

```
device -> comms (COMMSDB raw, APPDB) -> manage.py sync_comms_data -> tracking_* tables -> map / dashboards
```

1. Set `COMMS_APP_DATABASE_URL=postgres://user:pass@host:5432/APPDB` in `.env`.
2. Register the vehicle under its client and a GPS Device whose **IMEI equals the comms `unitno`**,
   status ACTIVE, assigned to that vehicle. Mapping is by IMEI only; comms' own client/vehicle columns
   are ignored. Unknown or non-active IMEIs are skipped and reported (nothing is auto-created).
3. Feed it automatically: set `COMMS_SYNC_AUTOSTART=True` (and optionally `COMMS_SYNC_INTERVAL_SECONDS=10`)
   in `.env`. A background thread inside the web server (runserver, or WSGI/ASGI via `config/wsgi.py` /
   `config/asgi.py`) then syncs on that interval — no second terminal. A PostgreSQL advisory lock makes it safe
   with several workers or with the command below also running. **Restart the web server after changing `.env`.**
   Alternatively run it as its own process: `python manage.py sync_comms_data --loop` (default every 10 s;
   `--interval`, `--history-days`, `--batch-size`, `--max-rows`). Without `--loop` it does one pass
   (cron-friendly). The first run imports the last 7 days of history (`--history-days`); later runs
   continue from a saved cursor (`CommsSyncState`). The connection is read-only, `tracktime` is read as
   the Settings default timezone, and `odometer` is converted metres -> km.

The map shows a vehicle as ONLINE only while data is under 5 minutes old, so the feed must keep running; the
Live Tracking page warns when it has stopped.

The reverse-geocoded address from comms' `current_table.location` is stored on the vehicle's current position
and shown in the Live Tracking **Vehicle Status** table and detail panel; selecting a vehicle (table row or
marker) flies the map to its exact position at street-level zoom.

### Panic / Idle / Over Speeding / Main Power alerts and the Alert Report

* **Source of truth.** The comms stored procedure `insert_current_and_history_json` sets
  `panic = 1` when the device's analog input `analog1 / 1000 >= 10 V` (else 0) in APPDB `history_table` /
  `current_table`. FMS never recomputes it: the bridge copies it unchanged into each history record's
  `metadata["panic"]`. With `COMMS_RAW_DATABASE_URL` (COMMSDB, read-only) set, it also reads that `analog1`
  for panic readings so alerts show the voltage.
* **One alert per episode** (`apps/alerts/events.py`, run inside ingestion for every batch): per vehicle, in
  device-time order, `0->1` creates one `Alert` (category `PANIC`), `1->1` extends it, `1->0` stamps
  `signal_cleared_at`. Late/out-of-order rows are handled, re-delivery is a no-op, and a per-vehicle advisory
  lock prevents duplicates under concurrent ingestion. A failure there is logged and never loses telemetry.
* **Notification.** A new event notifies (bell, `Notification.alert`) every active user with `alert.view` who
  can see that vehicle's client. The bell polls every 15 s (60 s in a background tab); `static/js/alert_center.js`
  shows the popup and plays a short alarm once per notification (remembered per browser; the alarm waits
  for the first click if the browser blocks audio). Only events newer than `ALERT_NOTIFY_MAX_AGE_MINUTES`
  (default 1440) notify, so a history import does not page anyone.
* **Reports > Alert Report** (`/alerts/report/`, API `/api/v1/alerts/`): date range, vehicle, alert type and
  status filters, server-side paging/sorting, details with map, acknowledge/resolve (`alert.update`),
  PDF/Excel of every matching alert. Client users (now allowed `alert.view`) see only their client's alerts.
* **Idle alerts** (`apps/alerts/idle.py`, MEDIUM): ignition ON and the vehicle genuinely stationary,
  continuously, for `Vehicle.idle_alert_minutes` (default 5, set per vehicle under "Trip & Idle Detection").
  "Stationary" reuses the vehicle's movement settings (min speed / min distance, the same ones trip detection
  uses): a reading beyond the distance only counts as movement when the next one confirms it or the speed is
  up; one speed spike or GPS jump is ignored; the odometer only decides when there is no GPS fix (it creeps
  while parked on these devices). A gap over `IDLE_ALERT_MAX_GAP_SECONDS` (180) ends a run, and a run needs
  `IDLE_ALERT_MIN_READINGS` (3). One alert per idle episode; it ends when the vehicle moves or the ignition goes
  off, and its duration is the real idle time. Independent from trips. Backfill:
  `python manage.py backfill_idle_alerts --days 7` (no notifications).
* **Over Speeding alerts** (`apps/alerts/overspeed.py`, HIGH): comms stores the device's triggering event id in
  `eventioval`; the website maps `eventioval == 255` -> `overspeed = 1` (`overspeed_from_eventioval`, the only place
  the mapping lives; the bridge stores just `metadata["overspeed"]`, never `eventioval`). The device sends a 255
  record when the vehicle goes over the limit and another when it drops back under, so a 255 opens an alert and
  the next 255 (within `OVERSPEED_MAX_EPISODE_MINUTES`, default 30, no ignition OFF between) closes it; the alert's
  speed is the episode's top speed. Out-of-order and re-delivered records are handled. Backfill from the comms
  Raw DB: `python manage.py backfill_overspeed_alerts --days 7` (no notifications).
* **Main power voltage alerts** (same level engine as Panic): comms stores `mainpower` = externalvoltage / 1000 (V).
  `main_power_state` in `apps/alerts/events.py` (the only place the ranges live) classifies each reading exactly:
  `0 <= V < 5` **Main Power Disconnected** (HIGH), `5 <= V < 8.5` **Low Voltage** (MEDIUM), `V >= 8.5` normal;
  NULL / negative = no information. Each reading carries `power_cut` / `low_voltage` flags; a change INTO an alert
  state opens one alert (+ notification and sound), further readings in the same state change nothing, and a
  change out of it (another range or >= 8.5 V) clears it — per vehicle. The voltage and its state are also on the
  vehicle's current position (Live Tracking -> Vehicle Status -> Main Power). `backfill_panic_alerts` also
  backfills main power.
* **Device battery alerts** (same engine, its own flags): comms stores `device_battery_voltage` = batteryvoltage /
  1000 (V). `device_battery_state` (same exact-band classifier as main power, `DEVICE_BATTERY_BANDS`) gives
  `0 <= V < 2` **Device Battery Disconnected** (HIGH), `2 <= V < 3` **Device Battery Low Voltage** (MEDIUM),
  `V >= 3` normal. The voltage is an alert input only: kept in the history record's metadata, never on the
  current position, the Live Tracking feed/page, or a visible history column; battery alert messages carry no
  voltage (the report and exports show it on the alert itself).
* **Geofence alerts** (`apps/geofences/services.py`, run in the same ingestion hook for every reading): a geofence
  is a circle or a polygon drawn on the form's map, of type **Entry** (Geofence Entry, MEDIUM, when an assigned
  vehicle enters), **Exit** (Geofence Exit, MEDIUM, when it leaves), **Entry and Exit** (both, from one geofence)
  or **Speed Limit** (Geofence Speeding, HIGH,
  while inside and speed > the geofence's km/h limit; cleared when back under the limit or on leaving; the
  alert keeps the limit and the episode's top speed). Only ACTIVE geofences and their ASSIGNED vehicles are
  evaluated. State per vehicle + geofence = the latest `GeofenceEvent` (ENTER/EXIT); a crossing needs
  `GEOFENCE_CONFIRM_READINGS` (2) consecutive trustworthy fixes, and leaving needs to be more than
  `GEOFENCE_EXIT_TOLERANCE_METERS` (20) outside the boundary, so GPS jitter and single jumps never flap. Alerts
  link their geofence (Alert Report filter, PDF/Excel "Geofence" and "Speed limit" columns).
  Live Tracking draws every active geofence the viewer may see (client users: only those assigned to their
  vehicles), coloured by type (Entry green, Exit orange, Speed Limit red, Entry and Exit purple), with hover / click
  details, a legend, and a Geofences ON/OFF button that only hides them visually.
* **Fast notifications (no WebSockets in this deployment):** the bell asks `/notifications/pulse/` every 2 s (10 s
  in a background tab) — one indexed lookup of the newest unread alert notification — and loads the full feed,
  popup and sound only when it changes. With the comms bridge at `COMMS_SYNC_INTERVAL_SECONDS=2` (a pass takes
  ~0.1 s), a device record reaches the popup within a few seconds of comms storing it.
* **Backfill** history imported before this existed: `python manage.py backfill_panic_alerts --days 7`
  (idempotent, sends no notifications). New types (overspeed, geofence, ...) = a new `Alert.Category` plus a
  detector in `apps/alerts/events.py`; the report, bell and exports need no change.

### Live Tracking auto-refresh

The Live Tracking page polls `/api/v1/tracking/fleet/current/` (the fleet feed only — the page never reloads)
on a selectable interval: **5 s, 10 s, 30 s (default), 60 s, 5 min**; the choice is remembered per browser.
The logic lives in a reusable module, `static/js/auto_refresh.js` (`FmsAutoRefresh.create({...})`), so any
other real-time page can reuse it. It guarantees a single timer and a single in-flight request, keeps the
cadence measured from each request's start, pauses while the tab is hidden (and catches up on return),
backs off (max 30 s, honouring `Retry-After`) while the server is failing, and cleans up on `pagehide`.
The fleet feed has its own throttle bucket (`fleet_read`, 4000/hour) so 5 s polling can't lock a user out
of the rest of the API. When the comms bridge is not running the page says so (`sync` block in the feed)
instead of silently showing old positions.

### Trip Report — ignition-derived trips

Reports > **Trip Report** (`/tracking/trip-report/`) shows what each vehicle actually did, computed live from
the existing telemetry history — no separate trip table, nothing pre-aggregated. This is unrelated to
`apps.trips.Trip` (the dispatch/logistics model: a planned client shipment, origin/destination Site, driver
assignment, DRAFT→...→COMPLETED workflow), which is untouched; Trip Report answers "what did the GPS see".

* **Detection rule** (`apps/tracking/trip_report.py`): ignition ON only opens a **candidate** trip; it
  becomes a trip once the history confirms genuine movement — (A) enough of the last N readings at the
  minimum speed *and* the vehicle away from where the ignition came on, (B) GPS displacement corroborated
  by the odometer, or (C) a real, plausible odometer advance — so a parked vehicle with the ignition on,
  ON/OFF without driving, GPS drift or a single speed spike never creates a trip. The trip starts where
  the vehicle departed, not at ignition ON. Ignition OFF for less than the vehicle's **Trip Closure Time**
  is still the same trip (a red light, a delivery stop); OFF for that long or longer closes it at the
  moment it actually went off. All of it is configured **per vehicle** on the Vehicle create/edit screen
  (`trip_closure_minutes` default 5; `trip_validation_records` 5, `trip_min_moving_records` 3,
  `trip_min_speed_kmh` 5, `trip_min_distance_m` 50) — changing one vehicle never affects another.
* **Never stored.** A trip is a Python object built by scanning `TelemetryEvent` ignition transitions —
  reusing the exact history table the Live Tracking bridge already fills. A hand-written SQL query
  (`LAG() OVER (PARTITION BY vehicle ORDER BY timestamp)`) does the reduction inside PostgreSQL, since
  Django can't filter a queryset on a window function's result — only the actual ON/OFF transitions
  (typically a handful a day per vehicle) cross into Python, not the full 10-30s-interval history.
* **Active trips update live**, not just on refresh: the current position/odometer comes from
  `VehicleCurrentTelemetry` — the same row the map uses — so distance/location grow each poll without the
  trip ever being "closed" by loading the page. A trip that started yesterday and is still running shows
  under Today with its real start time, not clipped at midnight.
* **Date filter**: Today / Yesterday / Last 3 / 5 / 7 Days / Custom range (`range`/`from`/`to` on
  `/api/v1/tracking/trip-report/`), plus a vehicle picker and search — all server-side, updated without a
  page reload, on the same `FmsAutoRefresh` polling module as Live Tracking (`static/js/trip_report.js`).
* Same RBAC as Live Tracking (`tracking_device` module) — a client user sees only their own fleet.
* **Map column.** Each row draws that trip's real path as a lightweight canvas sparkline (not a live tile
  map — dozens of those in one table would multiply OSM tile load well past what's documented as safe
  elsewhere in this file). Clicking a thumbnail opens a full Leaflet/OSM map with the actual polyline and
  start/end markers, fetched from `/api/v1/tracking/trip-report/route/?vehicle=&start=&end=` — the real GPS
  points between the trip's start and end (or "now" for an active trip), downsampled to at most 300 points.
  A completed trip's route is fetched once and cached in the browser for the rest of the page's life; an
  active trip's is re-fetched each time it redraws, since it can still be growing.
* **No-GPS-fix readings are excluded, never plotted.** Real devices report `(0, 0)` ("Null Island") before
  they've acquired satellites — this fleet's own history has them right as ignition turns on
  (`metadata.gps_status: 0`). `trip_route()` drops them from the path; `_finalize()`/`_apply_live_position()`
  leave a trip's start/end position (and Start/End Location text) blank rather than `(0, 0)` when the
  boundary reading itself has no fix — see `apps/tracking/trip_report._has_gps_fix()`.

### Responsive UI

`static/css/responsive.css` is loaded last and adapts the shared components for tablets/phones (Bootstrap
breakpoints; the sidebar is an off-canvas drawer below 1200 px): compact top bar, collapsible filter bars,
tables that scroll on tablets and become stacked cards on phones (`app.js` copies each column header onto
its cells as `data-label`; add class `no-stack` to a table to opt out), touch-sized controls with 16 px
inputs, bottom-anchored form action bars, and viewport-fitted dropdowns/modals. Prefer these shared
classes over per-page media queries.

## Docker

```bash
cp .env.example .env   # set POSTGRES_* / DATABASE_URL to match
docker compose up --build
```

Brings up `db` (PostgreSQL), `redis`, `web` (gunicorn, runs migrate +
collectstatic on start), `celery-worker`, and `celery-beat`. The app is served
directly by gunicorn on `:8000` — there's no bundled TLS-terminating reverse
proxy, so `SECURE_SSL_REDIRECT` etc. default to `False` (see `.env.example`).
Put a real reverse proxy with TLS in front for production and flip those flags on.

## Security notes for Phase 1

- Passwords hashed via Django's default PBKDF2 hasher; validated against
  Django's built-in validators (min length 10, common-password check, etc.)
- CSRF protection on all forms; session cookies `HttpOnly`
- RBAC enforced server-side on every view/API endpoint (`ModulePermissionRequiredMixin`
  / `HasModulePermission`) — template-level `{% has_module_perm %}` checks only
  control what's *shown*, never what's allowed
- Audit log entries are immutable at the ORM level (`save()`/`delete()` raise
  after the first write) — not just a UI convention
- Soft delete on `User` (`is_deleted`/`deleted_at`/`deleted_by`); the default
  manager excludes deleted rows, `User.all_objects` is the explicit escape hatch

## What's next

Say **"CONTINUE PHASE 2"** to build Clients, Client Sites, Branches/Depots,
Vendors, and Contracts on top of this foundation.
