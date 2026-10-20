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
  // Vehicle immobilizer (Live Map popup): FMS endpoints only — FMS itself talks to the comms command API.
  const IMMOBILIZER_URL_TEMPLATE = root.dataset.immobilizerUrlTemplate || "";
  const IMMOBILIZER_COMMAND_URL_TEMPLATE = root.dataset.immobilizerCommandUrlTemplate || "";
  const CSRF_TOKEN = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
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


  // Geofence layer: one fixed colour per geofence TYPE (design-system tokens,
  // the same in the legend) — never random.
  const GEOFENCE_TYPE_TOKEN = {
    ENTRY: "--fms-success", EXIT: "--fms-warning", SPEED_LIMIT: "--fms-danger", ENTRY_AND_EXIT: "--fms-purple",
  };
  const GEOFENCE_STORAGE_KEY = "fms.liveTracking.geofences";
  const GEOFENCE_LEGEND_KEY = "fms.liveTracking.geofenceLegend"; // sessionStorage: this tab only
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
    // Immobilizer panel: per-vehicle command state (kept apart from the actual state, which always
    // comes from the fleet feed) and the single poll timer of the open popup.
    immo: { entries: new Map(), uuid: null, timer: null, generation: 0 },
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
    // Marker detail follows the zoom: compact far out, labels close in.
    FmsVehicleIcons.applyZoomClass(state.map);
    state.map.on("zoomend", () => FmsVehicleIcons.applyZoomClass(state.map));
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

  /* Minimize / maximize the geofence legend panel. UI only: geofence shapes, the ON/OFF toggle,
   * monitoring and alerts are not touched. Remembered for this tab's session. */
  function setGeofenceLegendCollapsed(collapsed) {
    const legend = document.getElementById("liveGeofenceLegend");
    const btn = document.getElementById("liveGeofenceLegendBtn");
    if (!legend || !btn) return;
    legend.classList.toggle("is-collapsed", collapsed);
    const label = collapsed ? "Maximize Geofence Options" : "Minimize Geofence Options";
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.querySelector("i").className = `bi ${collapsed ? "bi-plus-lg" : "bi-dash-lg"}`;
    try {
      sessionStorage.setItem(GEOFENCE_LEGEND_KEY, collapsed ? "min" : "max");
    } catch (unavailable) {
      /* private mode: the state still holds for this page */
    }
  }

  function initGeofenceLegendCollapse() {
    const btn = document.getElementById("liveGeofenceLegendBtn");
    if (!btn) return;
    let collapsed = false;
    try {
      collapsed = sessionStorage.getItem(GEOFENCE_LEGEND_KEY) === "min";
    } catch (unavailable) {
      collapsed = false;
    }
    setGeofenceLegendCollapsed(collapsed);
    btn.addEventListener("click", () => {
      const legend = document.getElementById("liveGeofenceLegend");
      setGeofenceLegendCollapsed(!legend.classList.contains("is-collapsed"));
    });
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
    initGeofenceLegendCollapse();
    updateGeofenceUi(0);
    loadGeofences();
    // Created / edited / deactivated geofences appear without a page reload.
    const timerId = window.setInterval(() => { if (!document.hidden) loadGeofences(); }, GEOFENCE_REFRESH_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadGeofences(); });
    window.addEventListener("pagehide", () => window.clearInterval(timerId));
  }

  /* Vehicle markers: static/js/vehicle_icons.js (FmsVehicleIcons) is the one
   * marker system — type silhouette, status, heading, selection, label and
   * tooltip — shared with the Live Map popup, the panel and the table.
   * A marker is built ONCE; afterwards its position glides to each new fix and
   * only the parts that changed are patched in place. */
  function addVehicleMarker(map, vehicle, { onClick = null, zIndexOffset = 0 } = {}) {
    const latlng = [parseFloat(vehicle.latitude), parseFloat(vehicle.longitude)];
    const marker = L.marker(latlng, { icon: FmsVehicleIcons.markerIcon(vehicle), zIndexOffset, riseOnHover: true });
    if (onClick) marker.on("click", onClick);
    marker.addTo(map);
    return marker;
  }

  function renderMarkers(vehicles) {
    if (!state.map) return; // Leaflet failed to load — table/filters still work without markers.
    const seen = new Set();
    vehicles.forEach((vehicle) => {
      seen.add(vehicle.vehicle_uuid);
      const latlng = [parseFloat(vehicle.latitude), parseFloat(vehicle.longitude)];
      let marker = state.markers.get(vehicle.vehicle_uuid);
      if (!marker) {
        marker = addVehicleMarker(state.map, vehicle, {
          onClick: () => selectVehicle(vehicle.vehicle_uuid, { recenter: true }),
        });
        state.markers.set(vehicle.vehicle_uuid, marker);
      } else {
        FmsVehicleIcons.moveMarker(marker, latlng, { map: state.map });
      }
      FmsVehicleIcons.updateMarker(marker, vehicle, { selected: vehicle.vehicle_uuid === state.selectedUuid });
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
        <button type="button" class="live-status-pill${isActive ? " active" : ""}" data-pill="${pill.key}" data-tone="${(pill.key || "all").toLowerCase()}" role="tab" aria-selected="${isActive}">
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
        ${FmsVehicleIcons.badgeHtml(vehicle.vehicle_type, { size: 30 })}
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
        ${FmsVehicleIcons.badgeHtml(vehicle.vehicle_type, { size: 40 })}
        <div>
          <div class="fw-semibold" style="font-size:1.05rem;">${escapeHtml(vehicle.registration_number)}</div>
          <div class="text-small text-muted-fms">${vehicle.driver_name ? escapeHtml(vehicle.driver_name) : "Unassigned"}</div>
        </div>
      </div>
      <div class="mb-2">${connectionBadgeHtml(vehicle.connection_status)}</div>
      <p class="text-small text-muted-fms mb-3">Updated ${timeAgo(vehicle.timestamp)}${vehicle.connection_status !== "ONLINE" ? " — data may not reflect the vehicle's current position." : ""}</p>
      <div class="d-flex flex-column gap-2">
        <div class="d-flex justify-content-between"><span class="cell-secondary">Type</span><span>${escapeHtml(FmsVehicleIcons.labelFor(vehicle.vehicle_type))}</span></div>
        <div class="d-flex justify-content-between gap-3"><span class="cell-secondary">Location</span><span class="text-end">${vehicle.location ? escapeHtml(vehicle.location) : "—"}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Track Time</span><span>${escapeHtml(trackTime(vehicle.timestamp))}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">GPS Odometer</span><span>${escapeHtml(gpsOdometerText(vehicle))}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Coordinates</span><span>${parseFloat(vehicle.latitude).toFixed(5)}, ${parseFloat(vehicle.longitude).toFixed(5)}</span></div>
        <div class="d-flex justify-content-between"><span class="cell-secondary">Speed</span><span>${vehicle.speed !== null ? vehicle.speed + " km/h" : "—"}</span></div>
        <div class="d-flex justify-content-between align-items-center"><span class="cell-secondary">Direction</span><span>${directionHtml(vehicle)}</span></div>
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

  // Direction cell: compass + "North-East · 45°" (moving) / "Stationary · last heading NE" / unknown.
  function directionHtml(vehicle) {
    const d = FmsVehicleIcons.directionOf(vehicle);
    let text;
    let sub = "";
    if (d.degrees === null) {
      text = d.moving ? "Calculating…" : "Stationary";
      sub = d.moving ? "Waiting for enough movement" : "No recent movement";
    } else if (d.moving) {
      text = `${d.label} · ${d.degrees}°`;
      sub = d.source === "device" ? "From the device heading" : "From the GPS track";
    } else {
      text = "Stationary";
      sub = `Last heading ${d.label} · ${d.degrees}°`;
    }
    return `<span class="live-direction">${FmsVehicleIcons.compassHtml(vehicle)}
      <span class="live-direction-text"><span class="live-direction-main">${escapeHtml(text)}</span><span class="live-direction-sub">${escapeHtml(sub)}</span></span></span>`;
  }

  function liveMapDetailsHtml(vehicle) {
    const row = (label, value) =>
      `<div class="live-vehicle-row"><span class="cell-secondary">${label}</span><span>${value}</span></div>`;
    return `
      <div class="live-vehicle-status">
        ${connectionBadgeHtml(vehicle.connection_status)}
        <span class="text-small text-muted-fms">${escapeHtml(FmsVehicleIcons.statusFor(vehicle).label.toLowerCase())}</span>
      </div>
      <section class="live-immobilizer" id="liveImmobilizer" aria-label="Vehicle immobilizer"></section>
      ${row("Vehicle", `<strong>${escapeHtml(vehicle.registration_number)}</strong>`)}
      ${row("Type", `<span class="d-inline-flex align-items-center gap-1">${FmsVehicleIcons.badgeHtml(vehicle.vehicle_type, { size: 24 })}${escapeHtml(FmsVehicleIcons.labelFor(vehicle.vehicle_type))}</span>`)}
      ${row("Driver", vehicle.driver_name ? escapeHtml(vehicle.driver_name) : '<span class="text-muted-fms">Unassigned</span>')}
      ${row("Status", (() => { const st = FmsVehicleIcons.statusFor(vehicle); return `<span class="vm-status-text vm-status-text--${st.key}"><span class="dot"></span>${st.label}</span>`; })())}
      ${vehicle.active_alert ? row("Active alert", `<span class="vm-status-text vm-status-text--alert"><i class="bi bi-exclamation-triangle-fill"></i>${escapeHtml(vehicle.active_alert.label)}</span>`) : ""}
      ${row("Location", vehicle.location ? escapeHtml(vehicle.location) : '<span class="text-muted-fms">—</span>')}
      ${row("Speed", vehicle.speed !== null && vehicle.speed !== undefined ? `${escapeHtml(vehicle.speed)} km/h` : "—")}
      ${row("Odometer", escapeHtml(deviceOdometerText(vehicle)))}
      ${row("Main power", mainPowerHtml(vehicle))}
      ${row("Ignition", ignitionHtml(vehicle.ignition))}
      ${row("Last update", `${escapeHtml(trackTime(vehicle.timestamp))}<div class="cell-secondary">${escapeHtml(timeAgo(vehicle.timestamp))}</div>`)}
      ${row("Coordinates", `${parseFloat(vehicle.latitude).toFixed(5)}, ${parseFloat(vehicle.longitude).toFixed(5)}`)}
      ${row("Direction", directionHtml(vehicle))}
      ${vehicle.connection_status !== "ONLINE"
        ? '<p class="text-small text-muted-fms mb-0 mt-2"><i class="bi bi-info-circle me-1"></i>This is the last reported position — the vehicle is not reporting live right now.</p>'
        : ""}`;
  }

  function renderLiveMapHeader(vehicle) {
    const headerIcon = document.getElementById("liveVehicleModalIcon");
    if (headerIcon) headerIcon.innerHTML = FmsVehicleIcons.svg(vehicle.vehicle_type, 30);
    document.getElementById("liveVehicleModalTitle").textContent = `${vehicle.registration_number} — Live Map`;
    document.getElementById("liveVehicleModalSubtitle").textContent = vehicle.driver_name || "Unassigned";
    document.getElementById("liveVehicleModalUpdated").textContent = `Updated ${timeAgo(vehicle.timestamp)} · refreshes with the page`;
  }

  function openLiveMap(uuid) {
    const vehicle = state.vehicles.get(uuid);
    if (!vehicle) return;
    const modalEl = document.getElementById("liveVehicleModal");
    state.liveMap.uuid = uuid;
    immoOpen(uuid);
    renderLiveMapHeader(vehicle);
    setLiveDetails(vehicle);
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
    // The same vehicle marker as the main map — this vehicle only, labelled (street-level zoom).
    FmsVehicleIcons.applyZoomClass(map);
    map.on("zoomend", () => FmsVehicleIcons.applyZoomClass(map));
    state.liveMap.marker = addVehicleMarker(map, vehicle, { zIndexOffset: 1000 });
    FmsVehicleIcons.updateMarker(state.liveMap.marker, vehicle, { selected: true });
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
    setLiveDetails(vehicle);
    const { map } = state.liveMap;
    if (!map || !hasGpsFix(vehicle)) return; // keep showing the last good position
    const latlng = L.latLng(parseFloat(vehicle.latitude), parseFloat(vehicle.longitude));
    if (!state.liveMap.marker) {
      // First fix arrived while the popup was open.
      setLiveMapNotice("");
      state.liveMap.marker = addVehicleMarker(map, vehicle, { zIndexOffset: 1000 });
      map.setView(latlng, LIVE_MAP_ZOOM);
    } else {
      FmsVehicleIcons.moveMarker(state.liveMap.marker, latlng, { map }); // glide old -> new location
    }
    FmsVehicleIcons.updateMarker(state.liveMap.marker, vehicle, { selected: true });
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
      immoClose();
      state.liveMap.uuid = null;
    });
    // The immobilizer panel is re-rendered with the details: one delegated listener.
    document.getElementById("liveVehicleDetails").addEventListener("click", onImmobilizerClick);
  }

  /* ---------------- Vehicle immobilizer (Live Map popup) ----------------
   * Two separate things, never mixed:
   *   ACTUAL state   = vehicle.immobilizer from the fleet feed (the device data / comms current_table:
   *                    true = ON, false = OFF, null = unknown) — the marker uses the same value;
   *   COMMAND status = entry.info.command, from FMS (which asks the comms command API):
   *                    0 Pending, 1 Processing, 2 Sent to device, 3 Failed, 4 Completed.
   * Sending or completing a command never changes the displayed actual state. */

  const IMMO_TERMINAL = new Set([3, 4]);
  const IMMO_CONFIRM_WATCH_MS = 60000; // after Completed: refresh the feed until the device reports the new state
  const IMMO_CONFIRM_EVERY_MS = 5000;

  function immoEntry(uuid) {
    if (!state.immo.entries.has(uuid)) {
      state.immo.entries.set(uuid, {
        info: null, loading: false, loadError: "", submitting: false, confirming: null, message: null, polling: false, watchUntil: 0,
      });
    }
    return state.immo.entries.get(uuid);
  }

  function immoUrl(template, uuid) {
    return template.replace(ZERO_UUID, uuid);
  }

  function immoStopTimer() {
    if (state.immo.timer) window.clearTimeout(state.immo.timer);
    state.immo.timer = null;
  }

  // Popup closed or another vehicle opened: stop every timer; a late reply is dropped by the generation check.
  function immoClose() {
    immoStopTimer();
    state.immo.generation += 1;
    const entry = state.immo.uuid && state.immo.entries.get(state.immo.uuid);
    if (entry) Object.assign(entry, { polling: false, confirming: null, watchUntil: 0 });
    state.immo.uuid = null;
  }

  function immoOpen(uuid) {
    immoClose();
    if (!IMMOBILIZER_URL_TEMPLATE) return;
    state.immo.uuid = uuid;
    const generation = state.immo.generation;
    const entry = immoEntry(uuid);
    entry.message = null;
    entry.loading = !entry.info;
    entry.loadError = "";
    renderImmobilizer();
    fetch(immoUrl(IMMOBILIZER_URL_TEMPLATE, uuid), { headers: { Accept: "application/json" }, cache: "no-store" })
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then((body) => {
        if (generation !== state.immo.generation) return; // closed / switched meanwhile
        entry.info = body;
        entry.loading = false;
        if (body.command && body.command.in_progress) immoSchedulePoll(uuid);
        renderImmobilizer();
      })
      .catch(() => {
        if (generation !== state.immo.generation) return;
        entry.loading = false;
        entry.loadError = "Could not load the immobilizer status.";
        renderImmobilizer();
      });
  }

  function immoSchedulePoll(uuid) {
    immoStopTimer();
    const entry = immoEntry(uuid);
    entry.polling = true;
    const intervalMs = Math.max(1, (entry.info && entry.info.poll_interval_seconds) || 2) * 1000;
    state.immo.timer = window.setTimeout(() => immoPollOnce(uuid), intervalMs);
  }

  // One status request at a time: the next one is scheduled only after this one has ended.
  function immoPollOnce(uuid) {
    state.immo.timer = null;
    const entry = immoEntry(uuid);
    const command = entry.info && entry.info.command;
    if (state.immo.uuid !== uuid || !command) return;
    const generation = state.immo.generation;
    const token = command.uuid;
    const deadline = new Date(command.created_at).getTime() + (command.timeout_seconds + 15) * 1000;
    fetch(immoUrl(IMMOBILIZER_COMMAND_URL_TEMPLATE, token), { headers: { Accept: "application/json" }, cache: "no-store" })
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then((body) => {
        // Only the command this popup is tracking, for this vehicle, may change the panel.
        if (generation !== state.immo.generation || state.immo.uuid !== uuid) return;
        if (!entry.info.command || entry.info.command.uuid !== token || !body.command || body.command.uuid !== token) return;
        entry.info.command = body.command;
        entry.message = body.warning ? { tone: "warning", text: `${body.warning} Retrying…` } : null;
        immoAfterUpdate(uuid);
      })
      .catch(() => {
        if (generation !== state.immo.generation || state.immo.uuid !== uuid) return;
        if (Date.now() < deadline) {
          entry.message = { tone: "warning", text: "Connection problem while checking the command — retrying…" };
          immoSchedulePoll(uuid);
        } else {
          entry.polling = false;
          entry.message = { tone: "danger", text: "Stopped checking: no answer in time. Reopen the map to check again." };
        }
        renderImmobilizer();
      });
  }

  function immoAfterUpdate(uuid) {
    const entry = immoEntry(uuid);
    const command = entry.info.command;
    if (command.in_progress) {
      immoSchedulePoll(uuid);
    } else {
      entry.polling = false;
      immoStopTimer();
      if (command.status === 4) {
        // Completed is not proof: refresh the feed until the device data reports the requested state.
        entry.watchUntil = Date.now() + IMMO_CONFIRM_WATCH_MS;
        immoConfirmWatch(uuid);
      }
    }
    renderImmobilizer();
  }

  function immoActual(uuid) {
    const vehicle = state.vehicles.get(uuid);
    return vehicle && typeof vehicle.immobilizer === "boolean" ? vehicle.immobilizer : null;
  }

  function immoConfirmWatch(uuid) {
    state.immo.timer = null;
    const entry = immoEntry(uuid);
    const command = entry.info && entry.info.command;
    if (state.immo.uuid !== uuid || !command) return;
    if (immoActual(uuid) === (command.requested_state === "on") || Date.now() > entry.watchUntil) {
      entry.watchUntil = 0;
      renderImmobilizer();
      return;
    }
    state.refresher.refreshNow(); // the page's own feed: updates the actual state, marker and table together
    state.immo.timer = window.setTimeout(() => immoConfirmWatch(uuid), IMMO_CONFIRM_EVERY_MS);
  }

  function immoSubmit(uuid, wanted) {
    const entry = immoEntry(uuid);
    if (entry.submitting || (entry.info && entry.info.command && entry.info.command.in_progress)) return; // no duplicates
    entry.submitting = true;
    entry.sendingState = wanted;
    entry.confirming = null;
    entry.message = null;
    renderImmobilizer();
    const generation = state.immo.generation;
    fetch(immoUrl(IMMOBILIZER_URL_TEMPLATE, uuid), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": CSRF_TOKEN },
      body: JSON.stringify({ state: wanted }),
    })
      .then((res) => res.json().catch(() => ({})).then((body) => ({ ok: res.ok, status: res.status, body })))
      .then(({ ok, status, body }) => {
        entry.submitting = false;
        if (body && body.vehicle_uuid === uuid) entry.info = body;
        if (generation !== state.immo.generation || state.immo.uuid !== uuid) return;
        if (ok && body.command) {
          entry.message = null; // the step tracker shows the progress
          immoSchedulePoll(uuid);
        } else {
          const fallback = status === 403 ? "You don't have permission to send commands." : "The command could not be sent.";
          entry.message = { tone: "danger", text: `${(body && body.detail) || fallback} The immobilizer state is unchanged.` };
          if (body && body.command && body.command.in_progress) immoSchedulePoll(uuid);
        }
        renderImmobilizer();
      })
      .catch(() => {
        entry.submitting = false;
        if (generation !== state.immo.generation || state.immo.uuid !== uuid) return;
        entry.message = { tone: "danger", text: "Network error — the command was not confirmed as sent. Reopen the map to check its status." };
        renderImmobilizer();
      });
  }

  function onImmobilizerClick(event) {
    const button = event.target.closest("[data-immo]");
    const uuid = state.immo.uuid;
    if (!button || !uuid || button.disabled) return;
    const entry = immoEntry(uuid);
    const action = button.dataset.immo;
    if (action === "on" || action === "off") entry.confirming = action;
    else if (action === "cancel") entry.confirming = null;
    else if (action === "confirm" && entry.confirming) {
      immoSubmit(uuid, entry.confirming);
      return;
    }
    renderImmobilizer();
  }

  /* ---- Panel rendering ----
   * The ACTUAL state is the card's header (from the fleet feed: the device data); the COMMAND
   * status is a separate step tracker underneath. They never borrow each other's labels. */

  const IMMO_STEPS = ["Pending", "Processing", "Sent to device", "Completed"];

  // Where a command is on the tracker: { done: n steps done, active, final: success|failed|timeout|rejected|null }
  function immoProgress(command, sending) {
    if (sending) return { done: 0, active: 0, final: null, sending: true };
    if (command.status === null) return { done: 0, active: -1, final: command.in_progress ? null : "rejected" };
    if (command.status === 4) return { done: 4, active: -1, final: "success" };
    if (command.status === 3) return { done: 3, active: -1, final: "failed" };
    if (command.timed_out) return { done: command.status + 1, active: -1, final: "timeout" };
    return { done: command.status, active: command.status, final: null };
  }

  function immoStepsHtml(progress) {
    return `<ol class="immo-steps" aria-label="Command progress">${IMMO_STEPS.map((label, i) => {
      let cls = "todo";
      let text = label;
      if (i < progress.done) cls = "done";
      if (i === progress.active) cls = "active";
      if (i === 3 && progress.final === "failed") { cls = "failed"; text = "Failed"; }
      if (i === 3 && progress.final === "timeout") { cls = "timeout"; text = "Timed out"; }
      if (i === 0 && progress.final === "rejected") { cls = "failed"; text = "Not accepted"; }
      const icon = { done: "bi-check-lg", failed: "bi-x-lg", timeout: "bi-clock" }[cls] || "";
      return `<li class="immo-step is-${cls}"${cls === "active" ? ' aria-current="step"' : ""}>
          <span class="immo-step-dot">${icon ? `<i class="bi ${icon}" aria-hidden="true"></i>` : ""}</span>
          <span class="immo-step-label">${escapeHtml(text)}</span></li>`;
    }).join("")}</ol>`;
  }

  function immoCanSend(info) {
    return !!(info && info.can_command && info.service_configured && info.has_unit_no);
  }

  function immoActionButton(wanted, busy) {
    const enable = wanted === "on";
    const label = enable ? "Enable immobilizer" : "Disable immobilizer";
    const inner = busy
      ? '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Processing…</span>'
      : `<i class="bi ${enable ? "bi-lock-fill" : "bi-unlock-fill"}" aria-hidden="true"></i><span>${label}</span>`;
    return `<button type="button" class="immo-action immo-action--${enable ? "enable" : "disable"}" data-immo="${wanted}"
      aria-label="${label}"${busy ? ' disabled aria-busy="true"' : ""}>${inner}</button>`;
  }

  function immobilizerHtml(vehicle) {
    if (!IMMOBILIZER_URL_TEMPLATE) return "";
    const entry = immoEntry(vehicle.vehicle_uuid);
    const info = entry.info;
    const actual = typeof vehicle.immobilizer === "boolean" ? vehicle.immobilizer : null;
    const command = info && info.command;
    const sending = entry.submitting;
    const busy = sending || !!(command && command.in_progress);
    const stateKey = actual === null ? "unknown" : actual ? "on" : "off";

    // Header: the ACTUAL state.
    const hero = {
      on: { icon: "bi-shield-lock-fill", title: "IMMOBILIZER ON", sub: "Engaged — reported by the vehicle" },
      off: { icon: "bi-shield-check", title: "IMMOBILIZER OFF", sub: "Not engaged — reported by the vehicle" },
      unknown: { icon: "bi-shield", title: "STATE UNKNOWN", sub: "The vehicle has not reported its immobilizer yet" },
    }[stateKey];

    // Body: the control.
    let body = "";
    if (entry.loading) {
      body = '<div class="immo-skeleton" aria-label="Loading"><span></span></div>';
    } else if (entry.loadError) {
      body = `<div class="immo-note immo-note--danger"><i class="bi bi-wifi-off" aria-hidden="true"></i>${escapeHtml(entry.loadError)}</div>`;
    } else if (info && !info.can_command) {
      body = '<div class="immo-note"><i class="bi bi-eye" aria-hidden="true"></i>View only — you can\'t send immobilizer commands.</div>';
    } else if (info && !info.service_configured) {
      body = '<div class="immo-note"><i class="bi bi-plug" aria-hidden="true"></i>The command service is not configured.</div>';
    } else if (info && !info.has_unit_no) {
      body = '<div class="immo-note"><i class="bi bi-cpu" aria-hidden="true"></i>No GPS device unit number — commands are unavailable.</div>';
    } else if (info && entry.confirming && !busy) {
      const enable = entry.confirming === "on";
      const moving = vehicle.movement_state === "MOVING";
      const speed = vehicle.speed !== null && vehicle.speed !== undefined ? ` at ${Math.round(Number(vehicle.speed))} km/h` : "";
      body = `
        <div class="immo-confirm" role="group" aria-label="Confirm immobilizer command">
          <div class="immo-confirm-title"><i class="bi ${enable ? "bi-lock-fill" : "bi-unlock-fill"}" aria-hidden="true"></i>${enable ? "Enable" : "Disable"} immobilizer?</div>
          <p class="immo-confirm-text">An immobilizer <strong>${enable ? "ON" : "OFF"}</strong> command will be sent to <strong>${escapeHtml(vehicle.registration_number)}</strong>.</p>
          ${enable && moving ? `<div class="immo-warn"><i class="bi bi-exclamation-triangle-fill" aria-hidden="true"></i>This vehicle is moving${speed}.</div>` : ""}
          <div class="immo-confirm-actions">
            <button type="button" class="immo-btn-ghost" data-immo="cancel">Cancel</button>
            <button type="button" class="immo-action immo-action--${enable ? "enable" : "disable"} immo-action--sm" data-immo="confirm">
              <i class="bi bi-check2" aria-hidden="true"></i><span>${enable ? "Enable" : "Disable"}</span></button>
          </div>
        </div>`;
    } else if (info) {
      const options = actual === null ? ["on", "off"] : [actual ? "off" : "on"]; // both while unknown
      body = `<div class="immo-actions${options.length > 1 ? " immo-actions--pair" : ""}">${options.map((w) => immoActionButton(w, busy)).join("")}</div>`;
    }

    // Footer: the COMMAND (last one, or the one being sent).
    let commandHtml = "";
    if (command || sending) {
      const requested = sending ? entry.sendingState : command.requested_state;
      const progress = sending ? immoProgress(null, true) : immoProgress(command, false);
      const meta = [];
      if (!sending) {
        if (command.command_id !== null && command.command_id !== undefined) meta.push(`#${command.command_id}`);
        meta.push(trackTime(command.created_at));
        if (command.requested_by) meta.push(command.requested_by);
      }
      const word = requested === "on" ? "ON" : "OFF";
      const reached = actual === (requested === "on");
      let tone = "progress";
      let icon = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>';
      let text = "";
      let retry = false;
      if (progress.sending) text = "Sending command…";
      else if (progress.final === "success" && reached) {
        tone = "success"; icon = '<i class="bi bi-check-circle-fill immo-pop" aria-hidden="true"></i>';
        text = `Done — the vehicle reports IMMOBILIZER ${word}.`;
      } else if (progress.final === "success") {
        tone = "info";
        if (!entry.watchUntil) icon = '<i class="bi bi-hourglass-split" aria-hidden="true"></i>';
        text = `Command completed. Waiting for the vehicle to report ${word}…`;
      } else if (progress.final === "failed") {
        tone = "danger"; icon = '<i class="bi bi-x-octagon-fill immo-pop" aria-hidden="true"></i>';
        text = `The device did not carry out the command. The immobilizer is ${actual === null ? "unchanged" : `still ${actual ? "ON" : "OFF"}`}.`;
        retry = true;
      } else if (progress.final === "timeout") {
        tone = "warning"; icon = '<i class="bi bi-clock-history" aria-hidden="true"></i>';
        text = "No final response in time. The vehicle data will still show any change.";
        retry = true;
      } else if (progress.final === "rejected") {
        tone = "danger"; icon = '<i class="bi bi-exclamation-octagon-fill immo-pop" aria-hidden="true"></i>';
        text = `${command.error || "The command was not accepted."} The immobilizer is unchanged.`;
        retry = true;
      } else if (command.status === 2) text = "Waiting for device response…";
      else text = "Waiting for the command service…";
      retry = retry && !reached && immoCanSend(info) && !busy;
      // Connection hiccups while checking (only while it still runs), or "stopped checking".
      const showNotice = entry.message && !sending
        && ((entry.message.tone === "warning" && command.in_progress) || (entry.message.tone === "danger" && command.in_progress));
      const notice = showNotice
        ? `<div class="immo-notice immo-notice--${entry.message.tone}"><i class="bi ${entry.message.tone === "danger" ? "bi-wifi-off" : "bi-arrow-repeat"}" aria-hidden="true"></i>${escapeHtml(entry.message.text)}</div>` : "";
      commandHtml = `
        <div class="immo-command${progress.final ? ` is-${progress.final}` : " is-running"}">
          <div class="immo-command-head">
            <span class="immo-command-title">Command · Turn ${word}</span>
            <span class="immo-command-meta">${escapeHtml(meta.join(" · "))}</span>
          </div>
          ${immoStepsHtml(progress)}
          <div class="immo-feedback immo-feedback--${tone}" role="status">${icon}<span>${escapeHtml(text)}</span>
            ${retry ? `<button type="button" class="immo-retry" data-immo="${requested}"><i class="bi bi-arrow-clockwise" aria-hidden="true"></i>Retry</button>` : ""}
          </div>
          ${notice}
        </div>`;
    } else if (entry.message && entry.message.tone === "danger") {
      commandHtml = `<div class="immo-feedback immo-feedback--danger" role="status"><i class="bi bi-exclamation-octagon-fill" aria-hidden="true"></i><span>${escapeHtml(entry.message.text)}</span></div>`;
    }

    return `
      <div class="immo-card immo-card--${stateKey}${busy ? " is-busy" : ""}">
        <div class="immo-busy-bar" aria-hidden="true"></div>
        <div class="immo-hero">
          <span class="immo-hero-icon"><i class="bi ${hero.icon}" aria-hidden="true"></i></span>
          <div class="immo-hero-text">
            <span class="immo-eyebrow">Vehicle immobilizer · actual state</span>
            <span class="immo-hero-state">${hero.title}</span>
            <span class="immo-hero-sub">${hero.sub}</span>
          </div>
        </div>
        <div class="immo-body">${body}</div>
        ${commandHtml}
      </div>`;
  }

  // Draws the panel only when its content changed, so live refreshes never restart its animations
  // or steal focus (the panel node itself survives detail re-renders — see setLiveDetails).
  function renderImmobilizer() {
    const panel = document.getElementById("liveImmobilizer");
    const vehicle = state.immo.uuid && state.vehicles.get(state.immo.uuid);
    if (!panel || !vehicle) return;
    const html = immobilizerHtml(vehicle);
    if (panel.fmsHtml === html) return;
    const focused = document.activeElement && panel.contains(document.activeElement) ? document.activeElement.dataset.immo : null;
    panel.innerHTML = html;
    panel.fmsHtml = html;
    panel.dataset.uuid = vehicle.vehicle_uuid;
    if (focused) panel.querySelector(`[data-immo="${focused}"]`)?.focus();
  }

  // Popup details: rebuilt on each refresh, but the immobilizer panel node is carried over.
  function setLiveDetails(vehicle) {
    const details = document.getElementById("liveVehicleDetails");
    const kept = details.querySelector("#liveImmobilizer");
    details.innerHTML = liveMapDetailsHtml(vehicle);
    const slot = details.querySelector("#liveImmobilizer");
    if (kept && slot && kept.dataset.uuid === vehicle.vehicle_uuid) slot.replaceWith(kept);
    renderImmobilizer();
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
    // A new alert announced by the bell (static/js/alert_center.js) -> fetch now, so its pin turns red
    // without waiting for the next interval.
    window.addEventListener("fms:alert", () => state.refresher.refreshNow());

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
