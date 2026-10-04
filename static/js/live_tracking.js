(() => {
  "use strict";

  const root = document.getElementById("liveTrackingRoot");
  if (!root) return;

  const FLEET_API_URL = root.dataset.fleetApiUrl;
  const VEHICLE_URL_TEMPLATE = root.dataset.vehicleUrlTemplate;
  const TRIP_URL_TEMPLATE = root.dataset.tripUrlTemplate;
  const NO_TELEMETRY_URL = root.dataset.noTelemetryUrl;
  const NO_TELEMETRY_COUNT = parseInt(root.dataset.noTelemetryCount, 10) || 0;
  const GEOFENCES_URL = root.dataset.geofencesUrl;
  // Live Map popup: street-level view of ONE vehicle's current position only
  // (no history/route is drawn there — that's the Trip Report's job).
  const LIVE_MAP_ZOOM = 16;
  // comms reports track time in the Settings timezone; show it in that zone
  // (not the viewer's browser zone) so it matches the device/comms records.
  const DISPLAY_TZ = root.dataset.timezone || undefined;
  const ZERO_UUID = "00000000-0000-0000-0000-000000000000";

  const REFRESH_STORAGE_KEY = "fms.liveTracking.refreshMs";
  const DEFAULT_REFRESH_MS = 30000;
  const CAN_MANAGE_FEED = root.dataset.canManageFeed === "1";
  const FOLLOW_STORAGE_KEY = "fms.liveTracking.follow";

  // Tile provider config lives in static/js/fms_map.js — shared with the
  // Trip Report route modal — so there is one place a tile URL is defined.

  // A neutral, non-misleading default view — NOT any specific city/depot,
  // since that would read as a real (fake) fleet location.
  const DEFAULT_CENTER = [20, 0];
  const DEFAULT_ZOOM = 2;
  // Zoom used when a vehicle is selected — street level, so the marker sits on
  // its exact spot instead of the wide fleet view.
  const FOCUS_ZOOM = 17;

  const CONNECTION_TOKEN = { ONLINE: "--fms-success", STALE: "--fms-warning", OFFLINE: "--fms-text-muted" };

  // Geofence layer: one fixed colour per geofence TYPE (design-system tokens,
  // the same in the legend) — never random.
  const GEOFENCE_TYPE_TOKEN = {
    ENTRY: "--fms-success", EXIT: "--fms-warning", SPEED_LIMIT: "--fms-danger", ENTRY_AND_EXIT: "--fms-purple",
  };
  const GEOFENCE_STORAGE_KEY = "fms.liveTracking.geofences";
  // Geofences change rarely; they refresh on their own slow cycle, never with
  // the vehicle feed, and are only redrawn when the data actually changed.
  const GEOFENCE_REFRESH_MS = 30000;

  // Real states only — there is no "STOPPED" bucket in the backend data
  // model (only connection_status ONLINE/STALE/OFFLINE and movement_state
  // MOVING/IDLE/null), so the filter bar exposes exactly those, never an
  // invented one.
  const STATUS_PILLS = [
    { key: "", label: "All", icon: "bi-collection" },
    { key: "MOVING", label: "Moving", icon: "bi-truck", tone: "success" },
    { key: "IDLE", label: "Idle", icon: "bi-pause-circle", tone: "warning" },
    { key: "STALE", label: "Stale", icon: "bi-hourglass-split", tone: "warning" },
    { key: "OFFLINE", label: "Offline", icon: "bi-wifi-off", tone: "neutral" },
  ];

  const state = {
    vehicles: new Map(), // uuid -> vehicle payload from the fleet API
    markers: new Map(), // uuid -> L.Marker
    selectedUuid: root.dataset.initialVehicle || null,
    followMode: localStorage.getItem(FOLLOW_STORAGE_KEY) === "1",
    filters: {
      search: "",
      statusPill: (root.dataset.initialStatus || "").toUpperCase(),
      availability: "",
      telemetry: "",
    },
    map: null,
    hasFitOnce: false,
    initialSelectionApplied: false,
    refresher: null, // FmsAutoRefresh controller (static/js/auto_refresh.js)
    tickTimerId: null,
    lastUpdatedAt: null,
    firstLoadDone: false,
    // Live Map popup (one vehicle): its own short-lived Leaflet map.
    liveMap: { uuid: null, map: null, marker: null, lastTimestamp: null },
    // Geofence layer group (shown/hidden as a whole by the Geofences toggle).
    geofenceLayer: null,
    geofenceSignature: null,
    geofencesVisible: true,
  };

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#64748b";
  }

  function vehicleUrl(uuid) {
    return VEHICLE_URL_TEMPLATE.replace(ZERO_UUID, uuid);
  }

  function tripUrl(uuid) {
    return TRIP_URL_TEMPLATE.replace(ZERO_UUID, uuid);
  }

  // Shared with every other "live" page — see static/js/fms_format.js.
  const escapeHtml = FmsFormat.escapeHtml;
  const timeAgo = FmsFormat.timeAgo;

  function trackTime(isoTimestamp) {
    return FmsFormat.trackTime(isoTimestamp, DISPLAY_TZ);
  }

  function gpsOdometerText(vehicle) {
    if (vehicle.gps_odometer === null || vehicle.gps_odometer === undefined) return "—";
    return `${parseFloat(vehicle.gps_odometer).toFixed(1)} km`;
  }

  // The table's Odometer column: the device's own odometer reading exactly as
  // stored (VehicleCurrentTelemetry.odometer, refreshed on every poll) — never
  // a derived/daily distance. Missing or unparsable values render as "—".
  // Main Power column: the external (main) power voltage from comms
  // (mainpower = externalvoltage / 1000). The state (DISCONNECTED / LOW /
  // NORMAL) comes from the server — the voltage ranges live only in
  // apps.alerts.events.main_power_state.
  function mainPowerHtml(vehicle) {
    const volts = parseFloat(vehicle.main_power);
    if (vehicle.main_power === null || vehicle.main_power === undefined || Number.isNaN(volts)) {
      return '<span class="text-muted-fms">—</span>';
    }
    const text = `${volts.toFixed(2)} V`;
    if (vehicle.main_power_state === "DISCONNECTED") {
      return `<span class="main-power is-off" title="Main power disconnected"><i class="bi bi-plug me-1" aria-hidden="true"></i>${text} · Disconnected</span>`;
    }
    if (vehicle.main_power_state === "LOW") {
      return `<span class="main-power is-low" title="Low voltage"><i class="bi bi-battery-half me-1" aria-hidden="true"></i>${text} · Low</span>`;
    }
    return `<span class="main-power is-on"><i class="bi bi-plug-fill me-1" aria-hidden="true"></i>${text}</span>`;
  }

  function deviceOdometerText(vehicle) {
    const km = parseFloat(vehicle.odometer);
    if (vehicle.odometer === null || vehicle.odometer === undefined || Number.isNaN(km)) return "—";
    return `${km.toLocaleString("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} km`;
  }

  function initials(name) {
    if (!name) return "?";
    const parts = name.trim().split(/\s+/).filter(Boolean);
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  /* ---------------- Map ---------------- */

  function initMap() {
    if (typeof L === "undefined") {
      // Leaflet's <script> didn't load — fail loudly and visibly instead of
      // leaving a blank div with no clue why, and skip map-only calls
      // elsewhere so the rest of the page (table, filters) still works.
      const mapEl = document.getElementById("liveMap");
      if (mapEl) {
        mapEl.innerHTML =
          '<div class="map-empty-overlay" style="position:static;height:100%;">' +
          '<i class="bi bi-exclamation-triangle" style="font-size:1.5rem;"></i>' +
          "<p class='mt-2 mb-0'>Map library failed to load. Check your connection and reload the page.</p></div>";
      }
      hideMapLoading();
      return;
    }
    // Zoom control lives bottom-left — top-left is reserved for the
    // floating vehicle panel and top-right for Follow/Fit Fleet.
    state.map = L.map("liveMap", { zoomControl: false }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.control.zoom({ position: "bottomleft" }).addTo(state.map);
    FmsMap.addTileLayer(state.map);
    initGeofences();
  }

  /* ---------------- Geofence layer ----------------
   * Active geofences the viewer may see (server-scoped), drawn in one
   * Leaflet layer group on the overlay pane — below the vehicle markers,
   * which stay on the marker pane and keep working inside any geofence.
   * The Geofences toggle only adds/removes this group from the map: it is
   * purely visual (monitoring and alerts run on the server regardless).
   * A 403 or network error just means no geofences are drawn.
   */
  function geofenceDetailsHtml(g) {
    const rows = [
      `<div class="geofence-tip-title">${escapeHtml(g.name)}</div>`,
      `<div><span class="cell-secondary">Type:</span> ${escapeHtml(g.type_name || g.type_label || "")}</div>`,
      g.type === "SPEED_LIMIT" && g.speed_limit_kmh
        ? `<div><span class="cell-secondary">Speed Limit:</span> <strong>${escapeHtml(g.speed_limit_kmh)} km/h</strong></div>` : "",
      `<div><span class="cell-secondary">Status:</span> ${escapeHtml(g.status || "Active")}</div>`,
      `<div><span class="cell-secondary">Assigned Vehicles:</span> ${escapeHtml(g.assigned_vehicles ?? 0)}</div>`,
    ];
    return `<div class="geofence-tip">${rows.join("")}</div>`;
  }

  function geofenceShape(g) {
    const color = cssVar(GEOFENCE_TYPE_TOKEN[g.type] || "--fms-info");
    const style = { color, fillColor: color, weight: 2, opacity: 0.9, fillOpacity: 0.1 };
    const layer = g.shape === "POLYGON" && g.polygon && g.polygon.length >= 3
      ? L.polygon(g.polygon, style)
      : L.circle([parseFloat(g.latitude), parseFloat(g.longitude)], { ...style, radius: g.radius_meters });
    const html = geofenceDetailsHtml(g);
    // Hover: details only. Click: the same details in a small popup — no navigation.
    layer.bindTooltip(html, { sticky: true, direction: "top", className: "geofence-tooltip", opacity: 1 });
    layer.bindPopup(html, { className: "geofence-popup", closeButton: true, autoPan: false });
    layer.on("mouseover", () => layer.setStyle({ weight: 3, fillOpacity: 0.2 }));
    layer.on("mouseout", () => layer.setStyle({ weight: 2, fillOpacity: 0.1 }));
    return layer;
  }

  function renderGeofences(results) {
    const signature = JSON.stringify(results);
    if (signature === state.geofenceSignature) return; // unchanged: keep the existing layers untouched
    state.geofenceSignature = signature;
    state.geofenceLayer.clearLayers();
    results.forEach((g) => state.geofenceLayer.addLayer(geofenceShape(g)));
    const counts = { ENTRY: 0, EXIT: 0, SPEED_LIMIT: 0, ENTRY_AND_EXIT: 0 };
    results.forEach((g) => { if (g.type in counts) counts[g.type] += 1; });
    document.querySelectorAll("[data-geofence-count]").forEach((el) => {
      el.textContent = counts[el.dataset.geofenceCount] || 0;
    });
    updateGeofenceUi(results.length);
  }

  function loadGeofences() {
    if (!GEOFENCES_URL || !state.map || !state.geofenceLayer) return;
    fetch(GEOFENCES_URL, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data) => renderGeofences(data.results || []))
      .catch(() => {});
  }

  function updateGeofenceUi(total) {
    const btn = document.getElementById("liveGeofenceToggle");
    const legend = document.getElementById("liveGeofenceLegend");
    if (!btn) return;
    const count = total ?? state.geofenceLayer.getLayers().length;
    btn.setAttribute("aria-pressed", String(state.geofencesVisible));
    btn.classList.toggle("is-on", state.geofencesVisible);
    btn.title = state.geofencesVisible ? "Hide geofences" : "Show geofences";
    btn.querySelector("i").className = `bi ${state.geofencesVisible ? "bi-eye" : "bi-eye-slash"}`;
    btn.querySelector("[data-geofence-state]").textContent = state.geofencesVisible ? "ON" : "OFF";
    if (legend) legend.classList.toggle("d-none", !state.geofencesVisible || count === 0);
  }

  function setGeofencesVisible(visible) {
    state.geofencesVisible = visible;
    try {
      localStorage.setItem(GEOFENCE_STORAGE_KEY, visible ? "on" : "off");
    } catch (unavailable) {
      /* private mode — the choice just won't persist */
    }
    if (visible) state.geofenceLayer.addTo(state.map);
    else state.map.removeLayer(state.geofenceLayer);
    updateGeofenceUi();
  }

  function initGeofences() {
    if (!GEOFENCES_URL || !state.map) return;
    try {
      state.geofencesVisible = localStorage.getItem(GEOFENCE_STORAGE_KEY) !== "off";
    } catch (unavailable) {
      state.geofencesVisible = true;
    }
    state.geofenceLayer = L.featureGroup();
    if (state.geofencesVisible) state.geofenceLayer.addTo(state.map);
    document.getElementById("liveGeofenceToggle")?.addEventListener("click", () => setGeofencesVisible(!state.geofencesVisible));
    updateGeofenceUi(0);
    loadGeofences();
    // Created / edited / deactivated geofences appear without a page reload.
    const timerId = window.setInterval(() => { if (!document.hidden) loadGeofences(); }, GEOFENCE_REFRESH_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadGeofences(); });
    window.addEventListener("pagehide", () => window.clearInterval(timerId));
  }

  function iconFor(vehicle) {
    const color = cssVar(CONNECTION_TOKEN[vehicle.connection_status] || "--fms-text-muted");
    const isMoving = vehicle.movement_state === "MOVING";
    const rotation = isMoving && vehicle.heading !== null && vehicle.heading !== undefined ? vehicle.heading : 0;
    const isSelected = vehicle.vehicle_uuid === state.selectedUuid;
    const arrow = isMoving ? '<i class="bi bi-caret-up-fill"></i>' : '<i class="bi bi-record-fill" style="font-size:8px;"></i>';
    const html = `<div class="fleet-marker-dot${isSelected ? " is-selected" : ""}${vehicle.connection_status === "OFFLINE" ? " is-offline" : ""}" style="background:${color};transform:rotate(${rotation}deg);">${arrow}</div>`;
    return L.divIcon({ html, className: "fleet-marker-icon", iconSize: [26, 26], iconAnchor: [13, 13] });
  }

  function renderMarkers(vehicles) {
    if (!state.map) return; // Leaflet failed to load — table/filters still work without markers.
    const seen = new Set();
    vehicles.forEach((vehicle) => {
      seen.add(vehicle.vehicle_uuid);
      const latlng = [parseFloat(vehicle.latitude), parseFloat(vehicle.longitude)];
      let marker = state.markers.get(vehicle.vehicle_uuid);
      if (!marker) {
        marker = L.marker(latlng, { icon: iconFor(vehicle) });
        marker.on("click", () => selectVehicle(vehicle.vehicle_uuid, { recenter: true }));
        marker.addTo(state.map);
        state.markers.set(vehicle.vehicle_uuid, marker);
      } else {
        marker.setLatLng(latlng);
        marker.setIcon(iconFor(vehicle));
      }
    });
    state.markers.forEach((marker, uuid) => {
      if (!seen.has(uuid)) {
        state.map.removeLayer(marker);
        state.markers.delete(uuid);
      }
    });
  }

  // Fly to a vehicle's exact position at street-level zoom (never zooms *out*
  // if the user is already closer), and bring the map into view — the vehicle
  // table sits below it, so a row click would otherwise happen off-screen.
  function focusVehicle(vehicle, { scroll = false } = {}) {
    if (!state.map) return;
    const target = [parseFloat(vehicle.latitude), parseFloat(vehicle.longitude)];
    state.map.flyTo(target, Math.max(state.map.getZoom(), FOCUS_ZOOM), { duration: 0.8 });
    if (scroll) {
      document.querySelector(".live-map-shell")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function fitFleet() {
    if (!state.map || state.markers.size === 0) return;
    if (state.markers.size === 1) {
      const only = Array.from(state.markers.values())[0];
      state.map.setView(only.getLatLng(), 14);
      return;
    }
    const group = L.featureGroup(Array.from(state.markers.values()));
    state.map.fitBounds(group.getBounds().pad(0.2));
  }

  /* ---------------- Fleet fetch / refresh ---------------- */

  function setLastUpdatedNow() {
    state.lastUpdatedAt = new Date();
    tickLastUpdated();
  }

  function tickLastUpdated() {
    const el = document.getElementById("liveLastUpdated");
    if (!el) return;
    el.textContent = state.lastUpdatedAt ? `Updated ${timeAgo(state.lastUpdatedAt.toISOString())}` : "Not yet loaded";
  }

  function showRefreshError(reason) {
    const text = document.getElementById("fleetRefreshErrorText");
    if (text) text.textContent = reason ? `Unable to refresh live data — ${reason}` : "Unable to refresh live data";
    document.getElementById("fleetRefreshError").classList.remove("d-none");
  }

  function refreshErrorReason(status) {
    if (status === 401 || status === 403) return "your session expired or you no longer have access. Reload the page or sign in again.";
    if (status === 429) return "too many requests. It will retry automatically.";
    if (status >= 500) return `the server returned an error (HTTP ${status}).`;
    return `unexpected response (HTTP ${status}).`;
  }

  function hideRefreshError() {
    document.getElementById("fleetRefreshError").classList.add("d-none");
  }

  function toggleEmptyState(isEmpty) {
    document.getElementById("fleetEmptyState").classList.toggle("d-none", !isEmpty);
    document.getElementById("liveMap").classList.toggle("d-none", isEmpty);
  }

  function hideMapLoading() {
    const overlay = document.getElementById("liveMapLoading");
    if (overlay) overlay.classList.add("d-none");
  }

  // Thrown by loadFleet(); `reason` is the user-facing explanation shown in the
  // banner, `retryAfterMs` (HTTP 429) tells the refresher how long to back off.
  class FleetLoadError extends Error {
    constructor(reason, retryAfterMs) {
      super(reason);
      this.name = "FleetLoadError";
      this.reason = reason;
      this.retryAfterMs = retryAfterMs || 0;
    }
  }

  function retryAfterFrom(response) {
    const seconds = parseInt(response.headers.get("Retry-After"), 10);
    return Number.isFinite(seconds) ? Math.min(seconds, 300) * 1000 : 0;
  }

  // The refresher's task: fetch the fleet feed and render it. Only this data
  // is requested — the page itself is never reloaded.
  async function loadFleet({ signal } = {}) {
    let response;
    try {
      response = await fetch(FLEET_API_URL, { headers: { Accept: "application/json" }, cache: "no-store", signal });
    } catch (networkError) {
      if (networkError && networkError.name === "AbortError") throw networkError;
      hideMapLoading();
      throw new FleetLoadError("cannot reach the server. Check that it is running and your connection is up.");
    }
    if (!response.ok) {
      hideMapLoading();
      throw new FleetLoadError(refreshErrorReason(response.status), response.status === 429 ? retryAfterFrom(response) : 0);
    }

    let data;
    try {
      data = await response.json();
    } catch (parseError) {
      // e.g. a login redirect page (HTML) returned in place of JSON.
      hideMapLoading();
      throw new FleetLoadError("the server did not return fleet data. Reload the page or sign in again.");
    }
    try {
      applyFleet(data);
    } catch (renderError) {
      console.error("Live tracking render failed", renderError);
      throw new FleetLoadError("the data arrived but could not be displayed (see browser console).");
    }
  }

  /* ---------------- Auto-refresh state -> UI ---------------- */

  const REFRESH_INDICATOR_MIN_MS = 500; // keep the spinner visible long enough to be noticed
  let refreshingSince = 0;
  let refreshingClearTimer = null;

  function setRefreshingUi(isLoading) {
    const buttons = ["liveRefreshBtn", "vehicleStatusRefreshBtn"].map((id) => document.getElementById(id));
    const dot = document.getElementById("liveRefreshDot");
    const apply = (on) => {
      buttons.forEach((btn) => btn && btn.classList.toggle("is-refreshing", on));
      dot?.classList.toggle("is-active", on);
      root.setAttribute("aria-busy", on ? "true" : "false");
    };
    if (isLoading) {
      window.clearTimeout(refreshingClearTimer);
      refreshingSince = Date.now();
      apply(true);
    } else {
      const remaining = Math.max(0, REFRESH_INDICATOR_MIN_MS - (Date.now() - refreshingSince));
      refreshingClearTimer = window.setTimeout(() => apply(false), remaining);
    }
  }

  function onRefreshState(refreshState) {
    setRefreshingUi(refreshState.loading);
    if (refreshState.loading) return;
    if (refreshState.error) {
      showRefreshError(refreshState.error.reason || "");
      hideMapLoading();
    }
  }

  function refreshNow() {
    return state.refresher ? state.refresher.refreshNow() : Promise.resolve();
  }

  /* ---------------- Data-feed health (bridge from the comms program) ---------------- */

  function formatAge(seconds) {
    if (seconds < 90) return `${seconds} sec`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 90) return `${minutes} min`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} h`;
    return `${Math.round(hours / 24)} days`;
  }

  function renderSyncNotice(sync) {
    const box = document.getElementById("fleetFeedNotice");
    const text = document.getElementById("fleetFeedNoticeText");
    if (!box || !text) return;
    if (!sync || !sync.enabled || sync.healthy) {
      box.classList.add("d-none");
      return;
    }
    const since = sync.age_seconds === null || sync.age_seconds === undefined ? "never synced" : `last synced ${formatAge(sync.age_seconds)} ago`;
    text.textContent = `The live data feed is not running (${since}), so positions below may be out of date.` +
      (CAN_MANAGE_FEED ? " Start it with: python manage.py sync_comms_data --loop" : " Please contact your administrator.");
    box.classList.remove("d-none");
  }

  function applyFleet(data) {
    renderSyncNotice(data.sync);
    const previousSelected = state.selectedUuid ? state.vehicles.get(state.selectedUuid) : null;

    state.vehicles = new Map(data.results.map((v) => [v.vehicle_uuid, v]));
    hideRefreshError();
    setLastUpdatedNow();
    hideMapLoading();
    state.firstLoadDone = true;

    renderMarkers(data.results);
    renderStatusPills();
    renderVehicleTable();
    toggleEmptyState(data.count === 0);

    if (!state.initialSelectionApplied) {
      state.initialSelectionApplied = true;
      if (state.selectedUuid && state.vehicles.has(state.selectedUuid)) {
        selectVehicle(state.selectedUuid, { recenter: false, pushState: false });
      }
      if (!state.hasFitOnce && data.count > 0) {
        state.hasFitOnce = true;
        fitFleet();
      }
    } else if (state.selectedUuid) {
      const updated = state.vehicles.get(state.selectedUuid);
      if (updated) {
        renderPanel(updated);
        const advanced = !previousSelected || updated.timestamp !== previousSelected.timestamp;
        if (state.map && state.followMode && advanced) {
          state.map.panTo([parseFloat(updated.latitude), parseFloat(updated.longitude)]);
        }
      } else {
        renderPanelUnavailable();
      }
    }
    // The Live Map popup rides on the same feed — no polling of its own.
    if (state.liveMap.uuid) updateLiveMap();
  }

  /* ---------------- Status pill bar ---------------- */

  function renderStatusPills() {
    const container = document.getElementById("liveStatusPills");
    if (!container) return;
    const all = Array.from(state.vehicles.values());
    const counts = { "": all.length };
    STATUS_PILLS.slice(1).forEach((pill) => {
      counts[pill.key] = all.filter((v) =>
        pill.key === "MOVING" || pill.key === "IDLE" ? v.movement_state === pill.key : v.connection_status === pill.key
      ).length;
    });

    container.innerHTML = STATUS_PILLS.map((pill) => {
      const isActive = state.filters.statusPill === pill.key;
      return `
        <button type="button" class="live-status-pill${isActive ? " active" : ""}" data-pill="${pill.key}" role="tab" aria-selected="${isActive}">
          <i class="bi ${pill.icon}"></i>
          <span>${pill.label}</span>
          <span class="live-status-pill-count">${counts[pill.key]}</span>
        </button>`;
    }).join("");

    container.querySelectorAll("[data-pill]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.filters.statusPill = btn.dataset.pill;
        renderStatusPills();
        renderVehicleTable();
        updateUrlState();
      });
    });
  }

  /* ---------------- Search / filters / table ---------------- */

  function matchesFilters(vehicle) {
    const q = state.filters.search.trim().toLowerCase();
    if (q) {
      const haystack = `${vehicle.registration_number} ${vehicle.vehicle_type} ${vehicle.client} ${vehicle.driver_name || ""} ${vehicle.location || ""}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    const pill = state.filters.statusPill;
    if (pill === "MOVING" || pill === "IDLE") {
      if (vehicle.movement_state !== pill) return false;
    } else if (pill === "OFFLINE" || pill === "STALE") {
      if (vehicle.connection_status !== pill) return false;
    }
    if (state.filters.availability && vehicle.availability_status !== state.filters.availability) return false;
    return true;
  }

  function connectionBadgeHtml(status) {
    const tone = status === "ONLINE" ? "success" : status === "STALE" ? "warning" : "neutral";
    return `<span class="status-pill tone-${tone}"><span class="dot"></span>${status}</span>`;
  }

  function ignitionHtml(ignition) {
    if (ignition === true) return '<span class="ignition-indicator is-on" title="Telemetry-reported ignition state"><span class="dot"></span>ON</span>';
    if (ignition === false) return '<span class="ignition-indicator is-off" title="Telemetry-reported ignition state"><span class="dot"></span>OFF</span>';
    return '<span class="text-muted-fms">—</span>';
  }

  function locationHtml(vehicle) {
    const text = (vehicle.location || "").trim();
    if (!text) return '<span class="text-muted-fms" title="No address reported for this position">—</span>';
    return `<span class="vehicle-location" title="${escapeHtml(text)}"><i class="bi bi-geo-alt-fill me-1"></i>${escapeHtml(text)}</span>`;
  }

  function driverCellHtml(vehicle) {
    const name = vehicle.driver_name;
    return `
      <div class="d-flex align-items-center gap-2">
        <span class="avatar-chip" style="width:30px;height:30px;font-size:11px;">${escapeHtml(initials(name))}</span>
        <div>
          <div class="fw-semibold">${escapeHtml(vehicle.registration_number)}</div>
          <div class="text-small text-muted-fms">${name ? escapeHtml(name) : "Unassigned"}</div>
        </div>
      </div>`;
  }

  function renderVehicleTable() {
    const tbody = document.getElementById("vehicleStatusTableBody");
    const countsEl = document.getElementById("vehicleStatusCounts");
    const emptyEl = document.getElementById("vehicleStatusEmpty");
    const noTelemetryEl = document.getElementById("fleetNoTelemetryNote");
    const tableWrap = document.getElementById("vehicleStatusTable").closest(".table-responsive-fms");
    if (!tbody) return;

    if (state.filters.telemetry === "none") {
      tableWrap.classList.add("d-none");
      emptyEl.classList.add("d-none");
      noTelemetryEl.classList.remove("d-none");
      countsEl.textContent = `${NO_TELEMETRY_COUNT} vehicle${NO_TELEMETRY_COUNT === 1 ? "" : "s"} with no telemetry`;
      return;
    }
    noTelemetryEl.classList.add("d-none");

    const all = Array.from(state.vehicles.values());
    const visible = all.filter(matchesFilters);
    countsEl.textContent = `${visible.length} of ${all.length} tracked vehicle${all.length === 1 ? "" : "s"} shown`;

    if (all.length > 0 && visible.length === 0) {
      tableWrap.classList.add("d-none");
      emptyEl.classList.remove("d-none");
      return;
    }
    tableWrap.classList.remove("d-none");
    emptyEl.classList.add("d-none");

    if (all.length === 0) {
      tbody.innerHTML = "";
      return;
    }

    tbody.innerHTML = visible
      .map((vehicle) => {
        const isSelected = vehicle.vehicle_uuid === state.selectedUuid;
        const speedText = vehicle.speed !== null && vehicle.speed !== undefined ? `${vehicle.speed} km/h` : "—";
        return `
          <tr class="vehicle-status-row${isSelected ? " is-selected" : ""}" data-vehicle-uuid="${vehicle.vehicle_uuid}">
            <td>${driverCellHtml(vehicle)}</td>
            <td>${ignitionHtml(vehicle.ignition)}</td>
            <td class="cell-secondary" title="${escapeHtml(timeAgo(vehicle.timestamp))}">${escapeHtml(trackTime(vehicle.timestamp))}</td>
            <td>${speedText}</td>
            <td>${escapeHtml(deviceOdometerText(vehicle))}</td>
            <td class="text-nowrap">${mainPowerHtml(vehicle)}</td>
            <td class="cell-location">${locationHtml(vehicle)}</td>
            <td>${connectionBadgeHtml(vehicle.connection_status)}</td>
            <td class="live-map-cell">
              <button type="button" class="btn btn-sm btn-outline-primary live-map-btn" data-live-map="${vehicle.vehicle_uuid}"
                      aria-label="Open live map for ${escapeHtml(vehicle.registration_number)}">
                <i class="bi bi-geo-alt me-1" aria-hidden="true"></i>Live Map
              </button>
            </td>
          </tr>`;
      })
      .join("");

    tbody.querySelectorAll("[data-vehicle-uuid]").forEach((row) => {
      row.addEventListener("click", () => selectVehicle(row.dataset.vehicleUuid, { recenter: true, scroll: true }));
    });
    // The Live Map button opens the one-vehicle popup; it must not ALSO trigger
    // the row's "select on the main map" click.
    tbody.querySelectorAll("[data-live-map]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        openLiveMap(btn.dataset.liveMap);
      });
    });
  }

  /* ---------------- Selection / floating panel / URL state ---------------- */

  function updateUrlState() {
    const params = new URLSearchParams(window.location.search);
    if (state.selectedUuid) {
      params.set("vehicle", state.selectedUuid);
    } else {
      params.delete("vehicle");
    }
    if (state.filters.statusPill) {
      params.set("status", state.filters.statusPill.toLowerCase());
    } else {
      params.delete("status");
    }
    const query = params.toString();
    const newUrl = `${window.location.pathname}${query ? `?${query}` : ""}`;
    window.history.pushState({}, "", newUrl);
  }

  function selectVehicle(uuid, { recenter = false, pushState = true, scroll = false } = {}) {
    state.selectedUuid = uuid;
    const vehicle = state.vehicles.get(uuid);
    document.getElementById("vehiclePanel").classList.add("is-open");
    if (vehicle) {
      renderPanel(vehicle);
      if (recenter) focusVehicle(vehicle, { scroll });
    } else {
      renderPanelUnavailable();
    }
    renderVehicleTable();
    renderMarkers(Array.from(state.vehicles.values()));
    if (pushState) updateUrlState();
  }

  function closePanel() {
    state.selectedUuid = null;
    document.getElementById("vehiclePanel").classList.remove("is-open");
    updateUrlState();
    renderVehicleTable();
    renderMarkers(Array.from(state.vehicles.values()));
  }

  function renderPanelUnavailable() {
    document.getElementById("vehiclePanelBody").innerHTML = `
      <div class="empty-state">
        <div class="empty-icon"><i class="bi bi-exclamation-triangle"></i></div>
        <h4>This vehicle is no longer available</h4>
        <p>It may have gone offline or been unassigned since this page loaded.</p>
      </div>`;
  }

  function renderPanel(vehicle) {
    const activeTripHtml = vehicle.active_trip
      ? `
        <div class="mt-3 pt-3" style="border-top:1px solid var(--fms-border);">
          <div class="cell-secondary mb-1">Active Trip</div>
          <a href="${tripUrl(vehicle.active_trip.uuid)}" class="fw-semibold text-decoration-none">${escapeHtml(vehicle.active_trip.trip_number)}</a>
        </div>`
      : "";

    document.getElementById("vehiclePanelBody").innerHTML = `
      <div class="d-flex align-items-center gap-2 mb-1">
        <span class="avatar-chip" style="width:40px;height:40px;font-size:13px;">${escapeHtml(initials(vehicle.driver_name))}</span>
        <div>
          <div class="fw-semibold" style="font-size:1.05rem;">${escapeHtml(vehicle.registration_number)}</div>
          <div class="text-small text-muted-fms">${vehicle.driver_name ? escapeHtml(vehicle.driver_name) : "Unassigned"}</div>
        </div>
      </div>
      <div class="mb-2">${connectionBadgeHtml(vehicle.connection_status)}</div>
      <p class="text-small text-muted-fms mb-3">Updated ${timeAgo(vehicle.timestamp)}${vehicle.connection_status !== "ONLINE" ? " — data may not reflect the vehicle's current position." : ""}</p>
      <div class="d-flex flex-column gap-2">
        <div class="d-flex justify-content-between gap-3"><span class="cell-secondary">Location</span><span class="text-end">${vehicle.location ? escapeHtml(vehicle.location) : "—"}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Track Time</span><span>${escapeHtml(trackTime(vehicle.timestamp))}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">GPS Odometer</span><span>${escapeHtml(gpsOdometerText(vehicle))}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Coordinates</span><span>${parseFloat(vehicle.latitude).toFixed(5)}, ${parseFloat(vehicle.longitude).toFixed(5)}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Speed</span><span>${vehicle.speed !== null ? vehicle.speed + " km/h" : "—"}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Heading</span><span>${vehicle.heading !== null ? vehicle.heading + "°" : "—"}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Ignition</span><span>${ignitionHtml(vehicle.ignition)}</span></div>
        ${vehicle.movement_state ? `<div class="d-flex justify-content-between"><span class="cell-secondary">Movement</span><span>${vehicle.movement_state}</span></div>` : ""}
        <div class="d-flex justify-content-between"><span class="cell-secondary">Odometer</span><span>${vehicle.odometer !== null ? vehicle.odometer : "—"}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Availability</span><span>${escapeHtml(vehicle.availability_status)}</span></div>
      </div>
      ${activeTripHtml}
      <div class="d-flex flex-wrap gap-2 mt-3">
        <a href="${vehicleUrl(vehicle.vehicle_uuid)}" class="btn btn-sm btn-primary">Open Vehicle Details</a>
        <button type="button" class="btn btn-sm btn-outline-secondary" id="panelCenterMapBtn">Center Map</button>
      </div>`;

    document.getElementById("panelCenterMapBtn")?.addEventListener("click", () => {
      focusVehicle(vehicle);
    });
  }

  /* ---------------- Live Map popup (one vehicle) ---------------- */

  function liveMapDetailsHtml(vehicle) {
    const row = (label, value) =>
      `<div class="live-vehicle-row"><span class="cell-secondary">${label}</span><span>${value}</span></div>`;
    return `
      <div class="live-vehicle-status">
        ${connectionBadgeHtml(vehicle.connection_status)}
        ${vehicle.movement_state ? `<span class="text-small text-muted-fms">${escapeHtml(vehicle.movement_state.toLowerCase())}</span>` : ""}
      </div>
      ${row("Driver", vehicle.driver_name ? escapeHtml(vehicle.driver_name) : '<span class="text-muted-fms">Unassigned</span>')}
      ${row("Location", vehicle.location ? escapeHtml(vehicle.location) : '<span class="text-muted-fms">—</span>')}
      ${row("Speed", vehicle.speed !== null && vehicle.speed !== undefined ? `${escapeHtml(vehicle.speed)} km/h` : "—")}
      ${row("Odometer", escapeHtml(deviceOdometerText(vehicle)))}
      ${row("Main power", mainPowerHtml(vehicle))}
      ${row("Ignition", ignitionHtml(vehicle.ignition))}
      ${row("Last update", `${escapeHtml(trackTime(vehicle.timestamp))}<div class="cell-secondary">${escapeHtml(timeAgo(vehicle.timestamp))}</div>`)}
      ${row("Coordinates", `${parseFloat(vehicle.latitude).toFixed(5)}, ${parseFloat(vehicle.longitude).toFixed(5)}`)}
      ${vehicle.heading !== null && vehicle.heading !== undefined ? row("Heading", `${escapeHtml(vehicle.heading)}°`) : ""}
      ${vehicle.connection_status !== "ONLINE"
        ? '<p class="text-small text-muted-fms mb-0 mt-2"><i class="bi bi-info-circle me-1"></i>This is the last reported position — the vehicle is not reporting live right now.</p>'
        : ""}`;
  }

  function renderLiveMapHeader(vehicle) {
    document.getElementById("liveVehicleModalTitle").textContent = `${vehicle.registration_number} — Live Map`;
    document.getElementById("liveVehicleModalSubtitle").textContent = vehicle.driver_name || "Unassigned";
    document.getElementById("liveVehicleModalUpdated").textContent = `Updated ${timeAgo(vehicle.timestamp)} · refreshes with the page`;
  }

  function openLiveMap(uuid) {
    const vehicle = state.vehicles.get(uuid);
    if (!vehicle) return;
    const modalEl = document.getElementById("liveVehicleModal");
    state.liveMap.uuid = uuid;
    renderLiveMapHeader(vehicle);
    document.getElementById("liveVehicleDetails").innerHTML = liveMapDetailsHtml(vehicle);
    if (modalEl.classList.contains("show")) {
      buildLiveMap(); // already open: "shown" won't fire again, rebuild for this vehicle now
      return;
    }
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }

  function hasGpsFix(vehicle) {
    // (0,0) is the device's "no fix yet" sentinel (see trip_report._has_gps_fix).
    const lat = parseFloat(vehicle.latitude);
    const lon = parseFloat(vehicle.longitude);
    return Number.isFinite(lat) && Number.isFinite(lon) && !(lat === 0 && lon === 0);
  }

  function setLiveMapNotice(text) {
    const note = document.getElementById("liveVehicleMapNotice");
    note.textContent = text || "";
    note.classList.toggle("d-none", !text);
  }

  // Tears down everything the popup map owns, so the next vehicle always
  // starts from a blank map (no marker/view left over from the previous one).
  function destroyLiveMap() {
    if (state.liveMap.map) state.liveMap.map.remove();
    Object.assign(state.liveMap, { map: null, marker: null, lastTimestamp: null });
    document.getElementById("liveVehicleMap").innerHTML = "";
    setLiveMapNotice("");
  }

  function buildLiveMap() {
    destroyLiveMap();
    const vehicle = state.vehicles.get(state.liveMap.uuid);
    const container = document.getElementById("liveVehicleMap");
    if (!vehicle) return;
    if (typeof L === "undefined") {
      container.innerHTML = '<div class="p-4 text-center text-muted-fms">Map library failed to load. Reload the page and try again.</div>';
      return;
    }
    const map = L.map(container, { zoomControl: false });
    L.control.zoom({ position: "bottomleft" }).addTo(map);
    FmsMap.addTileLayer(map);
    state.liveMap.map = map;
    state.liveMap.lastTimestamp = vehicle.timestamp;
    if (!hasGpsFix(vehicle)) {
      map.setView([20, 0], 2);
      setLiveMapNotice("Waiting for this vehicle's first GPS fix — no position has been reported yet.");
      return;
    }
    const latlng = [parseFloat(vehicle.latitude), parseFloat(vehicle.longitude)];
    map.setView(latlng, LIVE_MAP_ZOOM);
    // Same status marker as the main map — this vehicle only, at its latest position.
    state.liveMap.marker = L.marker(latlng, { icon: iconFor(vehicle), zIndexOffset: 1000 }).addTo(map);
  }

  // Called after every fleet refresh (the page's own feed) while the popup is open.
  function updateLiveMap() {
    const vehicle = state.vehicles.get(state.liveMap.uuid);
    const details = document.getElementById("liveVehicleDetails");
    if (!vehicle) {
      details.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon"><i class="bi bi-exclamation-triangle"></i></div>
          <h4>This vehicle is no longer in the live feed</h4>
          <p>It may have gone offline or been unassigned since the map was opened.</p>
        </div>`;
      return;
    }
    renderLiveMapHeader(vehicle);
    details.innerHTML = liveMapDetailsHtml(vehicle);
    const { map } = state.liveMap;
    if (!map || !hasGpsFix(vehicle)) return; // keep showing the last good position
    const latlng = L.latLng(parseFloat(vehicle.latitude), parseFloat(vehicle.longitude));
    if (!state.liveMap.marker) {
      // First fix arrived while the popup was open.
      setLiveMapNotice("");
      state.liveMap.marker = L.marker(latlng, { icon: iconFor(vehicle), zIndexOffset: 1000 }).addTo(map);
      map.setView(latlng, LIVE_MAP_ZOOM);
    } else {
      state.liveMap.marker.setLatLng(latlng); // old location -> new location
      state.liveMap.marker.setIcon(iconFor(vehicle));
    }
    if (vehicle.timestamp !== state.liveMap.lastTimestamp) {
      state.liveMap.lastTimestamp = vehicle.timestamp;
      // Keep the moving vehicle in view without fighting the user's zoom.
      if (!map.getBounds().pad(-0.15).contains(latlng)) map.panTo(latlng);
    }
  }

  function wireLiveMapModal() {
    const modalEl = document.getElementById("liveVehicleModal");
    // Leaflet needs the container's real size, so build only once the modal is visible.
    modalEl.addEventListener("shown.bs.modal", buildLiveMap);
    modalEl.addEventListener("hidden.bs.modal", () => {
      destroyLiveMap();
      state.liveMap.uuid = null;
    });
  }

  /* ---------------- Controls ---------------- */

  function wireControls() {
    document.getElementById("fleetSearchInput").addEventListener("input", (event) => {
      state.filters.search = event.target.value;
      renderVehicleTable();
    });
    document.getElementById("fleetAvailabilityFilter").addEventListener("change", (event) => {
      state.filters.availability = event.target.value;
      renderVehicleTable();
    });
    document.getElementById("fleetTelemetryFilter").addEventListener("change", (event) => {
      state.filters.telemetry = event.target.value;
      renderVehicleTable();
    });

    document.getElementById("vehiclePanelClose").addEventListener("click", closePanel);
    wireLiveMapModal();

    const followBtn = document.getElementById("liveFollowToggle");
    followBtn.classList.toggle("active", state.followMode);
    followBtn.addEventListener("click", () => {
      state.followMode = !state.followMode;
      followBtn.classList.toggle("active", state.followMode);
      localStorage.setItem(FOLLOW_STORAGE_KEY, state.followMode ? "1" : "0");
    });

    document.getElementById("liveFitFleetBtn").addEventListener("click", fitFleet);
    document.getElementById("liveRefreshBtn").addEventListener("click", refreshNow);
    document.getElementById("vehicleStatusRefreshBtn").addEventListener("click", refreshNow);
    document.getElementById("fleetRefreshRetryBtn").addEventListener("click", refreshNow);

    // One controller owns the timer, the in-flight request, tab-visibility
    // pausing and back-off. Its <select> options are the only allowed intervals.
    state.refresher = FmsAutoRefresh.create({
      task: loadFleet,
      select: document.getElementById("liveRefreshInterval"),
      storageKey: REFRESH_STORAGE_KEY,
      defaultMs: DEFAULT_REFRESH_MS,
      onStateChange: onRefreshState,
    });

    // "Updated N sec ago" is a pure clock read — no network involved.
    state.tickTimerId = window.setInterval(tickLastUpdated, 5000);
    window.addEventListener("pagehide", () => window.clearInterval(state.tickTimerId));
  }

  /* ---------------- Theme reactivity (markers only — no dark tile layer) ---------------- */

  function wireThemeReactivity() {
    const rerender = () => renderMarkers(Array.from(state.vehicles.values()));
    new MutationObserver(rerender).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerender);
  }

  /* ---------------- Boot ---------------- */

  renderStatusPills();
  initMap();
  wireControls();
  wireThemeReactivity();
  state.refresher.start(); // first load now, then on the selected interval
})();
