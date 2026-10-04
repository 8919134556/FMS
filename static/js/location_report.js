/*
 * Location Data Report — every GPS record of a vehicle (or all vehicles) for a
 * period. Filters are staged: nothing loads until "Apply Filter". Records are
 * paged/sorted/filtered on the server (apps.tracking.location_report); the
 * browser only ever holds one page. Range bar, toasts and downloads come from
 * FmsReportKit (report_common.js); the path map reuses FmsMap + the existing
 * route endpoint used by the Trip Report and Live Map popups.
 */
(() => {
  "use strict";

  const root = document.getElementById("locationReportRoot");
  if (!root) return;

  const REPORT_URL = root.dataset.reportUrl;
  const EXPORT_URL = root.dataset.exportUrl;
  const ROUTE_URL = root.dataset.routeUrl;
  const MAX_CUSTOM_DAYS = parseInt(root.dataset.maxCustomDays, 10) || 31;
  const DISPLAY_TZ = root.dataset.timezone || undefined;
  const { escapeHtml, durationText } = FmsFormat;

  // Every column the history table can supply. ``optional`` columns are only
  // shown when the applied selection actually has data in them.
  const COLUMNS = [
    { key: "time", label: "Date / Time", sort: "time" },
    { key: "vehicle", label: "Vehicle", sort: "vehicle", allVehiclesOnly: true },
    { key: "latitude", label: "Latitude", sort: "latitude", num: true },
    { key: "longitude", label: "Longitude", sort: "longitude", num: true },
    { key: "speed", label: "Speed", sort: "speed", num: true },
    { key: "heading", label: "Heading", sort: "heading", num: true, optional: "heading" },
    { key: "altitude", label: "Altitude", sort: "altitude", num: true, optional: "altitude" },
    { key: "ignition", label: "Ignition", sort: "ignition" },
    { key: "gps_status", label: "GPS", optional: "gps_status" },
    { key: "satellite_count", label: "Satellites", sort: "satellites", num: true, optional: "satellite_count" },
    { key: "odometer", label: "Odometer", sort: "odometer", num: true },
    { key: "gps_odometer_km", label: "Trip Meter", num: true, optional: "gps_odometer_km" },
    { key: "engine_hours", label: "Engine Hours", sort: "engine_hours", num: true, optional: "engine_hours" },
    { key: "battery_voltage", label: "Battery", sort: "battery", num: true, optional: "battery_voltage" },
    { key: "external_power", label: "Ext. Power", optional: "external_power" },
    { key: "signal_strength", label: "Signal", sort: "signal", num: true, optional: "signal_strength" },
    { key: "location", label: "Location", optional: "location" },
    { key: "status", label: "Data Status", sort: "status" },
    { key: "action", label: "Action", mapOnly: true },
  ];

  const QUALITY_PILLS = [
    { key: "", label: "All", icon: "bi-collection" },
    { key: "valid", label: "Valid", icon: "bi-check-circle" },
    { key: "issues", label: "Flagged", icon: "bi-exclamation-triangle" },
  ];

  const state = {
    rangeControl: null,
    applied: null, // { vehicle, isAll, params } — what the table/downloads show
    columns: [], // optional columns with data (from the last analysis)
    quality: null,
    page: 1,
    pageSize: parseInt(document.getElementById("locPageSize").value, 10) || 100,
    sort: "time",
    dir: "asc",
    qualityFilter: "",
    map: null,
    highlight: null,
    period: null,
  };

  /* ---------------- Formatting ---------------- */

  const DASH = '<span class="text-muted-fms">—</span>';

  function num(value, places) {
    if (value === null || value === undefined || value === "") return DASH;
    return Number(value).toLocaleString("en-GB", { minimumFractionDigits: places, maximumFractionDigits: places });
  }

  function trackTime(iso) {
    return FmsFormat.trackTime(iso, DISPLAY_TZ);
  }

  function dateOnly(iso) {
    return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  }

  function hasFix(row) {
    return row.latitude !== null && row.longitude !== null && !(row.latitude === 0 && row.longitude === 0)
      && !row.flags.some((f) => f.startsWith("Invalid coordinates"));
  }

  function cellHtml(col, row) {
    switch (col.key) {
      case "time": return `<span class="text-nowrap">${escapeHtml(trackTime(row.timestamp))}</span>`;
      case "vehicle": return `<span class="fw-semibold">${escapeHtml(row.registration_number)}</span>`;
      case "latitude": return num(row.latitude, 6);
      case "longitude": return num(row.longitude, 6);
      case "speed": return row.speed === null ? DASH : `${num(row.speed, 1)} km/h`;
      case "heading": return row.heading === null ? DASH : `${row.heading}°`;
      case "altitude": return row.altitude === null ? DASH : `${num(row.altitude, 0)} m`;
      case "ignition":
        if (row.ignition === true) return '<span class="ignition-indicator is-on"><span class="dot"></span>ON</span>';
        if (row.ignition === false) return '<span class="ignition-indicator is-off"><span class="dot"></span>OFF</span>';
        return DASH;
      case "gps_status": return { 1: "Fix", 0: '<span class="text-warning">No fix</span>' }[row.gps_status] || (row.gps_status ?? DASH);
      case "satellite_count": return row.satellite_count ?? DASH;
      case "odometer": return row.odometer === null ? DASH : `${num(row.odometer, 1)} km`;
      case "gps_odometer_km": return row.gps_odometer_km === null ? DASH : `${num(row.gps_odometer_km, 3)} km`;
      case "engine_hours": return row.engine_hours === null ? DASH : `${num(row.engine_hours, 1)} h`;
      case "battery_voltage": return row.battery_voltage === null ? DASH : `${num(row.battery_voltage, 2)} V`;
      case "external_power": return row.external_power === null ? DASH : row.external_power ? "Yes" : "No";
      case "signal_strength": return row.signal_strength ?? DASH;
      case "location":
        return row.location ? `<span class="trip-location" title="${escapeHtml(row.location)}">${escapeHtml(row.location)}</span>` : DASH;
      case "status":
        if (!row.flags.length) return '<span class="status-pill tone-success"><span class="dot"></span>Valid</span>';
        return row.flags.map((f) => `<span class="status-pill tone-warning loc-flag"><span class="dot"></span>${escapeHtml(f)}</span>`).join(" ");
      case "action":
        return hasFix(row)
          ? `<button type="button" class="btn btn-sm btn-outline-primary" data-locate="${row.latitude},${row.longitude}" data-when="${escapeHtml(trackTime(row.timestamp))}"><i class="bi bi-crosshair me-1" aria-hidden="true"></i>Map</button>`
          : DASH;
      default: return DASH;
    }
  }

  /* ---------------- Filters (staged until Apply) ---------------- */

  function showError(message) {
    const box = document.getElementById("locError");
    document.getElementById("locErrorText").textContent = message;
    box.classList.toggle("d-none", !message);
  }

  function markPending() {
    document.getElementById("locPendingHint").classList.toggle("d-none", !state.applied);
  }

  function apply() {
    const vehicle = document.getElementById("locVehicle").value;
    if (!vehicle) {
      FmsReportKit.showToast("Select a vehicle (or All vehicles) first.", "warning");
      document.getElementById("locVehicle").focus();
      return;
    }
    const problem = state.rangeControl.commitCustomInputs();
    if (problem) {
      FmsReportKit.showToast(problem, "warning");
      return;
    }
    const params = state.rangeControl.appendTo(new URLSearchParams());
    params.set("vehicle", vehicle);
    state.applied = { vehicle, isAll: vehicle === "all", params };
    state.page = 1;
    state.sort = "time";
    state.dir = "asc";
    state.qualityFilter = "";
    document.getElementById("locPendingHint").classList.add("d-none");
    load({ analysis: true });
  }

  /* ---------------- Data ---------------- */

  function queryFor({ analysis }) {
    const params = new URLSearchParams(state.applied.params);
    params.set("page", state.page);
    params.set("page_size", state.pageSize);
    params.set("sort", state.sort);
    params.set("dir", state.dir);
    if (state.qualityFilter) params.set("quality", state.qualityFilter);
    if (analysis) params.set("analysis", "1");
    return params;
  }

  let loadSeq = 0;
  async function load({ analysis = false } = {}) {
    const seq = ++loadSeq; // only the newest request may render
    const applyBtn = document.getElementById("locApplyBtn");
    applyBtn.disabled = true;
    root.setAttribute("aria-busy", "true");
    document.getElementById("locTableBody").style.opacity = "0.5";
    try {
      const response = await fetch(`${REPORT_URL}?${queryFor({ analysis }).toString()}`, {
        headers: { Accept: "application/json" }, cache: "no-store",
      });
      const body = await response.json().catch(() => ({}));
      if (seq !== loadSeq) return;
      if (!response.ok) {
        const reason = body.detail
          || (response.status === 403 ? "Your session expired or you no longer have access." : `The server returned HTTP ${response.status}.`);
        showError(reason);
        if (analysis) {
          document.getElementById("locResults").classList.add("d-none");
          document.getElementById("locIntro").classList.remove("d-none");
        }
        return;
      }
      showError("");
      if (analysis) renderAnalysisBlock(body);
      renderRecords(body.records);
    } catch (networkError) {
      if (seq === loadSeq) showError("Cannot reach the server. Check your connection and try again.");
    } finally {
      if (seq === loadSeq) {
        applyBtn.disabled = false;
        root.setAttribute("aria-busy", "false");
        document.getElementById("locTableBody").style.opacity = "";
      }
    }
  }

  /* ---------------- Analysis + quality ---------------- */

  function tile(label, value, hint) {
    return `
      <div class="trip-metric">
        <span class="trip-metric-label">${escapeHtml(label)}</span>
        <span class="trip-metric-value">${value}</span>
        ${hint ? `<span class="trip-metric-hint">${escapeHtml(hint)}</span>` : ""}
      </div>`;
  }

  function kmText(value) {
    return value === null || value === undefined ? "N/A" : `${num(value, 2)} km`;
  }

  function speedText(value) {
    return value === null || value === undefined ? "N/A" : `${num(value, 1)} km/h`;
  }

  function renderAnalysisBlock(body) {
    const sel = body.selection;
    const q = body.quality;
    const a = body.analysis;
    state.quality = q;
    state.columns = body.columns || [];
    state.period = { start: sel.period_start, end: sel.period_end, vehicle: sel.vehicle };
    state.rangeControl.showPeriod(sel.range);

    const span = sel.range.start === sel.range.end ? dateOnly(sel.range.start) : `${dateOnly(sel.range.start)} – ${dateOnly(sel.range.end)}`;
    document.getElementById("locSelectionLine").innerHTML =
      `<i class="bi bi-truck-front me-1"></i><strong>${escapeHtml(sel.vehicle_label)}</strong> · ${escapeHtml(span)} · ${q.records.toLocaleString("en-GB")} GPS records`;
    document.getElementById("locIntro").classList.add("d-none");
    document.getElementById("locResults").classList.remove("d-none");

    const analysisEl = document.getElementById("locAnalysis");
    if (!a) {
      analysisEl.innerHTML = '<p class="text-small text-muted-fms mb-0">No GPS records in this period, so there is nothing to analyse.</p>';
      document.getElementById("locAnalysisNote").textContent = "";
    } else {
      document.getElementById("locAnalysisNote").textContent =
        `Moving = speed ≥ ${a.moving_threshold_kmh} km/h · gaps over ${a.gap_threshold_minutes} min count as no data`;
      analysisEl.innerHTML = `
        <div class="loc-metric-groups">
          <div><h5 class="trip-panel-subtitle">Distance &amp; time</h5><div class="trip-metric-grid">
            ${tile("GPS distance", kmText(a.gps_distance_km), "from latitude/longitude")}
            ${tile("First record", escapeHtml(trackTime(a.first_record)), "")}
            ${tile("Last record", escapeHtml(trackTime(a.last_record)), "")}
            ${tile("Tracking duration", durationText(a.tracking_seconds), "first → last record")}
          </div></div>
          <div><h5 class="trip-panel-subtitle">Movement</h5><div class="trip-metric-grid">
            ${tile("Moving", durationText(a.moving_seconds), "")}
            ${tile("Idle", durationText(a.idle_seconds), "ignition on, not moving")}
            ${tile("Stopped", durationText(a.stopped_seconds), "idle + ignition off")}
            ${tile("Stops ≥ 1 min", String(a.stops), a.longest_stop_seconds ? `longest ${durationText(a.longest_stop_seconds)}` : "")}
            ${a.no_data_seconds ? tile("No data", durationText(a.no_data_seconds), `gaps > ${a.gap_threshold_minutes} min`) : ""}
          </div></div>
          <div><h5 class="trip-panel-subtitle">Speed</h5><div class="trip-metric-grid">
            ${tile("Maximum speed", speedText(a.max_speed_kmh), a.max_speed_at ? `at ${trackTime(a.max_speed_at)}` : "")}
            ${tile("Average moving speed", speedText(a.avg_moving_speed_kmh), "distance ÷ moving time")}
          </div></div>
          <div><h5 class="trip-panel-subtitle">Ignition</h5><div class="trip-metric-grid">
            ${tile("Ignition ON", durationText(a.ignition_on_seconds), "")}
            ${tile("Ignition OFF", durationText(a.ignition_off_seconds), "")}
            ${tile("ON / OFF events", `${a.ignition_on_events} / ${a.ignition_off_events}`, "")}
          </div></div>
        </div>`;
    }

    const qualityItems = [
      ["Total records", q.records, ""],
      ["Valid GPS records", q.valid_fixes, ""],
      ["No GPS fix (0,0)", q.no_fix, ""],
      ["Device reports no fix", q.device_no_fix, ""],
      ["Invalid coordinates", q.invalid_coords, ""],
      ["Duplicate timestamps", q.duplicate, ""],
      ["GPS gaps", q.gap, q.longest_gap_seconds ? `longest ${durationText(q.longest_gap_seconds)}` : ""],
      ["GPS jumps", q.jump, ""],
      ["Invalid speed", q.invalid_speed, ""],
    ];
    // The first two are totals; any non-zero count after them is a problem worth colouring.
    document.getElementById("locQuality").innerHTML = `<div class="trip-metric-grid loc-quality-grid">${qualityItems
      .map(([label, value, hint], i) => tile(label, `<span class="${i > 1 && value ? "text-warning" : ""}">${Number(value).toLocaleString("en-GB")}</span>`, hint))
      .join("")}</div>`;

    renderMap(sel);
  }

  /* ---------------- Records table ---------------- */

  function visibleColumns() {
    return COLUMNS.filter((c) => {
      if (c.allVehiclesOnly && !state.applied.isAll) return false;
      if (c.mapOnly && (state.applied.isAll || !state.map)) return false;
      if (c.optional && !state.columns.includes(c.optional)) return false;
      return true;
    });
  }

  function renderHeader(columns) {
    document.getElementById("locHeadRow").innerHTML = columns
      .map((c) => {
        if (!c.sort) return `<th${c.num ? ' class="text-end"' : ""}>${c.label}</th>`;
        const active = state.sort === c.sort;
        const aria = active ? (state.dir === "asc" ? "ascending" : "descending") : "none";
        const icon = active ? (state.dir === "asc" ? "bi-sort-up" : "bi-sort-down") : "bi-arrow-down-up";
        return `<th aria-sort="${aria}"${c.num ? ' class="text-end"' : ""}><button type="button" class="loc-sort${active ? " is-active" : ""}" data-sort="${c.sort}">${c.label}<i class="bi ${icon}" aria-hidden="true"></i></button></th>`;
      })
      .join("");
    document.querySelectorAll("#locHeadRow [data-sort]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.sort;
        state.dir = state.sort === key && state.dir === "asc" ? "desc" : "asc";
        state.sort = key;
        state.page = 1;
        load();
      });
    });
  }

  function renderQualityPills() {
    const container = document.getElementById("locQualityPills");
    const q = state.quality;
    const counts = q ? { "": q.records, valid: q.records - q.flagged, issues: q.flagged } : {};
    container.innerHTML = QUALITY_PILLS.map((pill) => {
      const isActive = state.qualityFilter === pill.key;
      return `
        <button type="button" class="live-status-pill${isActive ? " active" : ""}" data-quality="${pill.key}" role="tab" aria-selected="${isActive}">
          <i class="bi ${pill.icon}"></i><span>${pill.label}</span>
          <span class="live-status-pill-count">${(counts[pill.key] ?? 0).toLocaleString("en-GB")}</span>
        </button>`;
    }).join("");
    container.querySelectorAll("[data-quality]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.qualityFilter = btn.dataset.quality;
        state.page = 1;
        load();
      });
    });
  }

  function renderRecords(records) {
    const columns = visibleColumns();
    renderHeader(columns);
    renderQualityPills();
    const { total, page, pages, page_size: size } = records;
    state.page = page;
    const first = total ? (page - 1) * size + 1 : 0;
    const last = Math.min(page * size, total);
    document.getElementById("locRecordCount").textContent =
      `Showing ${first.toLocaleString("en-GB")}–${last.toLocaleString("en-GB")} of ${total.toLocaleString("en-GB")} records`;

    const tbody = document.getElementById("locTableBody");
    tbody.innerHTML = records.rows
      .map((row) => `<tr class="${row.flags.length ? "loc-row-flagged" : ""}">${columns
        .map((c) => `<td class="${c.num ? "text-end text-nowrap" : ""}${c.key === "action" ? " loc-action-cell" : ""}">${cellHtml(c, row)}</td>`)
        .join("")}</tr>`)
      .join("");
    document.getElementById("locTableEmpty").classList.toggle("d-none", total !== 0);
    document.getElementById("locationTable").closest(".table-responsive-fms").classList.toggle("d-none", total === 0);
    tbody.querySelectorAll("[data-locate]").forEach((btn) => {
      btn.addEventListener("click", () => locate(btn.dataset.locate, btn.dataset.when));
    });
    renderPagination(page, pages);
  }

  function renderPagination(page, pages) {
    const nav = document.getElementById("locPagination");
    if (pages <= 1) {
      nav.innerHTML = "";
      return;
    }
    const numbers = new Set([1, pages, page - 1, page, page + 1].filter((p) => p >= 1 && p <= pages));
    const sorted = [...numbers].sort((x, y) => x - y);
    const items = [];
    sorted.forEach((p, i) => {
      if (i > 0 && p - sorted[i - 1] > 1) items.push('<li class="page-item disabled"><span class="page-link">…</span></li>');
      items.push(`<li class="page-item${p === page ? " active" : ""}"><button type="button" class="page-link" data-page="${p}"${p === page ? ' aria-current="page"' : ""}>${p}</button></li>`);
    });
    nav.innerHTML = `
      <ul class="pagination pagination-sm mb-0">
        <li class="page-item${page === 1 ? " disabled" : ""}"><button type="button" class="page-link" data-page="${page - 1}" aria-label="Previous page"><i class="bi bi-chevron-left"></i></button></li>
        ${items.join("")}
        <li class="page-item${page === pages ? " disabled" : ""}"><button type="button" class="page-link" data-page="${page + 1}" aria-label="Next page"><i class="bi bi-chevron-right"></i></button></li>
      </ul>`;
    nav.querySelectorAll("[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = parseInt(btn.dataset.page, 10);
        if (!target || target === state.page || target < 1 || target > pages) return;
        state.page = target;
        load();
        document.getElementById("locationTable").scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  /* ---------------- Route map (one vehicle, the applied period) ---------------- */

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#2563eb";
  }

  function renderMap(sel) {
    const card = document.getElementById("locMapCard");
    const note = document.getElementById("locMapNote");
    if (state.map) {
      state.map.remove();
      state.map = null;
      state.highlight = null;
    }
    if (sel.vehicle === "all") {
      card.classList.remove("d-none");
      document.getElementById("locMap").classList.add("d-none");
      note.textContent = "Select a single vehicle to see its route on the map.";
      return;
    }
    document.getElementById("locMap").classList.remove("d-none");
    if (typeof L === "undefined") {
      note.textContent = "Map library failed to load. Reload the page to see the route.";
      return;
    }
    state.map = L.map("locMap", { zoomControl: false }).setView([20, 0], 2);
    L.control.zoom({ position: "bottomleft" }).addTo(state.map);
    FmsMap.addTileLayer(state.map);
    note.textContent = "Loading route…";
    const params = new URLSearchParams({ vehicle: sel.vehicle, start: sel.period_start, end: sel.period_end });
    fetch(`${ROUTE_URL}?${params.toString()}`, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data) => {
        if (!state.map) return;
        const latlngs = data.points.map((p) => [parseFloat(p.lat), parseFloat(p.lon)]);
        if (!latlngs.length) {
          note.textContent = "No valid GPS fixes in this period — nothing to draw.";
          return;
        }
        const line = L.polyline(latlngs, { color: cssVar("--fms-primary"), weight: 4, opacity: 0.8 }).addTo(state.map);
        const dot = (ll, color, label) => L.circleMarker(ll, { radius: 7, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 })
          .bindTooltip(label).addTo(state.map);
        dot(latlngs[0], cssVar("--fms-success"), "First fix");
        dot(latlngs[latlngs.length - 1], cssVar("--fms-danger"), "Last fix");
        state.map.fitBounds(line.getBounds().pad(0.15), { maxZoom: 16 });
        note.textContent = data.truncated
          ? `Path drawn from ${latlngs.length} evenly spaced valid fixes of this period (the table lists every record).`
          : `Path drawn from all ${latlngs.length} valid fixes of this period.`;
      })
      .catch(() => {
        note.textContent = "The route could not be loaded right now.";
      });
  }

  // "Map" on a row: show that exact record on the route map.
  function locate(latlon, when) {
    if (!state.map) return;
    const [lat, lon] = latlon.split(",").map(Number);
    if (state.highlight) state.highlight.remove();
    state.highlight = L.circleMarker([lat, lon], { radius: 10, color: "#fff", weight: 3, fillColor: cssVar("--fms-warning"), fillOpacity: 1 })
      .bindTooltip(when, { permanent: true, direction: "top", offset: [0, -10] })
      .addTo(state.map);
    ensureMapVisible();
    state.map.setView([lat, lon], Math.max(state.map.getZoom(), 17));
    document.getElementById("locMapCard").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function ensureMapVisible() {
    const body = document.getElementById("locMapBody");
    if (!body.classList.contains("d-none")) return;
    toggleMap();
  }

  function toggleMap() {
    const body = document.getElementById("locMapBody");
    const btn = document.getElementById("locMapToggle");
    const hidden = body.classList.toggle("d-none");
    btn.setAttribute("aria-expanded", String(!hidden));
    btn.innerHTML = hidden
      ? '<i class="bi bi-eye me-1" aria-hidden="true"></i><span>Show map</span>'
      : '<i class="bi bi-eye-slash me-1" aria-hidden="true"></i><span>Hide map</span>';
    if (!hidden && state.map) window.setTimeout(() => state.map.invalidateSize(), 50);
  }

  /* ---------------- Boot ---------------- */

  state.rangeControl = FmsReportKit.createRangeControl({
    prefix: "loc", storageKey: "fms.locationReport.range", maxDays: MAX_CUSTOM_DAYS, onChange: markPending,
  });
  document.querySelectorAll("#locRangeBar .trip-range-pill").forEach((btn) => btn.addEventListener("click", markPending));
  document.getElementById("locVehicle").addEventListener("change", markPending);
  document.getElementById("locApplyBtn").addEventListener("click", apply);
  document.getElementById("locMapToggle").addEventListener("click", toggleMap);
  document.getElementById("locPageSize").addEventListener("change", (event) => {
    state.pageSize = parseInt(event.target.value, 10) || 100;
    state.page = 1;
    if (state.applied) load();
  });

  // Downloads cover the APPLIED filters — every matching record, not just this page.
  FmsReportKit.wireDownloadMenu({
    prefix: "loc",
    exportUrl: EXPORT_URL,
    rangeControl: state.rangeControl,
    precheck: () => (state.applied ? "" : "Select a vehicle and a period, then click Apply Filter before downloading."),
    buildParams: () => {
      const params = new URLSearchParams(state.applied.params);
      if (state.qualityFilter) params.set("quality", state.qualityFilter);
      return params;
    },
    fallbackName: "location-data",
  });
})();
