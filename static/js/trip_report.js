(() => {
  "use strict";

  const root = document.getElementById("tripReportRoot");
  if (!root) return;

  const TRIP_REPORT_URL = root.dataset.tripReportUrl;
  const TRIP_ROUTE_URL = root.dataset.tripRouteUrl;
  const TRIP_EXPORT_URL = root.dataset.tripExportUrl;
  const TRIP_ANALYSIS_URL = root.dataset.tripAnalysisUrl;
  const TRIP_SINGLE_EXPORT_URL = root.dataset.tripSingleExportUrl;
  const MAX_CUSTOM_DAYS = parseInt(root.dataset.maxCustomDays, 10) || 31;
  const DISPLAY_TZ = root.dataset.timezone || undefined;

  const REFRESH_STORAGE_KEY = "fms.tripReport.refreshMs";
  const RANGE_STORAGE_KEY = "fms.tripReport.range";
  const DEFAULT_REFRESH_MS = 30000;
  const SEARCH_DEBOUNCE_MS = 400;
  // Thumbnails load a handful at a time, not all at once — a page of 25+ rows
  // must not fire 25+ simultaneous requests every time the table re-renders.
  const ROUTE_THUMB_CONCURRENCY = 3;

  const STATUS_PILLS = [
    { key: "", label: "All", icon: "bi-collection" },
    { key: "ACTIVE", label: "Active", icon: "bi-broadcast" },
    { key: "COMPLETED", label: "Completed", icon: "bi-check-circle" },
  ];

  const { escapeHtml, timeAgo, durationText } = FmsFormat;
  // Shared with every page under Reports (static/js/report_common.js).
  const { showToast, downloadReport } = FmsReportKit;
  function trackTime(iso) {
    return FmsFormat.trackTime(iso, DISPLAY_TZ);
  }

  const state = {
    trips: [], // last page of results from the server (already scoped to the active range/vehicle/search)
    visibleTrips: [], // state.trips after the status-pill filter — what's actually on screen, in row order
    filters: { statusPill: "", search: "", vehicle: "" },
    rangeControl: null, // the server-side date filter (FmsReportKit.createRangeControl)
    refreshUi: null,
    refresher: null,
  };

  // Route points per trip, keyed by "vehicle_uuid|start_time" (a trip's start never
  // changes, so this is a stable identity across auto-refreshes). A COMPLETED trip's
  // route is immutable and cached for the rest of the page's life; an ACTIVE trip's
  // is re-fetched each time it's (re)drawn, since it can still be growing.
  const routeCache = new Map();
  let routeQueueActive = 0;
  const routeQueue = [];
  let routeModalMap = null;
  let routePlayer = null; // FmsRoutePlayback for the trip currently open in the popup
  // The popup asks the route endpoint for full detail (thumbnails keep 300 points).
  const DETAIL_ROUTE_POINTS = 5000;
  let pendingRouteTrip = null;

  function initials(name) {
    if (!name) return "?";
    const parts = name.trim().split(/\s+/).filter(Boolean);
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function refreshNow() {
    return state.refresher ? state.refresher.refreshNow() : Promise.resolve();
  }

  /* ---------------- Fetch ---------------- */

  function buildQuery() {
    const params = state.rangeControl.appendTo(new URLSearchParams());
    if (state.filters.vehicle) params.set("vehicle", state.filters.vehicle);
    if (state.filters.search) params.set("q", state.filters.search);
    return params.toString();
  }

  function loadTrips({ signal } = {}) {
    return FmsReportKit.loadJson(`${TRIP_REPORT_URL}?${buildQuery()}`, { signal, noun: "trip data", render: applyData });
  }

  function applyData(data) {
    state.trips = data.results;
    state.refreshUi.markUpdated();
    renderSummary(data.summary);
    state.rangeControl.showPeriod(data.range);
    renderStatusPills();
    renderTable();
    toggleEmptyState(data.count === 0);
  }

  function toggleEmptyState(isEmpty) {
    document.getElementById("tripReportEmpty").classList.toggle("d-none", !isEmpty);
    document.getElementById("tripReportTable").closest(".table-responsive-fms").classList.toggle("d-none", isEmpty);
  }

  /* ---------------- Summary strip ---------------- */

  function renderSummary(summary) {
    document.getElementById("tripKpiTotal").textContent = summary.total_trips;
    document.getElementById("tripKpiActive").textContent = summary.active_trips;
    document.getElementById("tripKpiDistance").textContent =
      summary.total_distance_km !== null && summary.total_distance_km !== undefined
        ? `${parseFloat(summary.total_distance_km).toFixed(1)} km`
        : "—";
    document.getElementById("tripKpiDuration").textContent = durationText(summary.avg_duration_seconds);
  }

  /* ---------------- Status pill bar ---------------- */

  function renderStatusPills() {
    const container = document.getElementById("tripStatusPills");
    if (!container) return;
    const counts = { "": state.trips.length };
    STATUS_PILLS.slice(1).forEach((pill) => {
      counts[pill.key] = state.trips.filter((t) => t.status === pill.key).length;
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
        renderTable();
      });
    });
  }

  /* ---------------- Route thumbnails (Map column) ---------------- */

  function routeCacheKey(trip) {
    return `${trip.vehicle_uuid}|${trip.start_time}`;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#64748b";
  }

  function fetchRoutePoints(trip, signal, maxPoints) {
    const params = new URLSearchParams({ vehicle: trip.vehicle_uuid, start: trip.start_time });
    if (trip.end_time) params.set("end", trip.end_time);
    if (maxPoints) params.set("max_points", String(maxPoints));
    return fetch(`${TRIP_ROUTE_URL}?${params.toString()}`, { headers: { Accept: "application/json" }, cache: "no-store", signal }).then(
      (res) => (res.ok ? res.json() : Promise.reject(new Error(`route request failed (${res.status})`)))
    );
  }

  // Runs at most ROUTE_THUMB_CONCURRENCY fetches at once; everything else waits its turn.
  function enqueueRouteFetch(task) {
    routeQueue.push(task);
    pumpRouteQueue();
  }

  function pumpRouteQueue() {
    while (routeQueueActive < ROUTE_THUMB_CONCURRENCY && routeQueue.length > 0) {
      const task = routeQueue.shift();
      routeQueueActive += 1;
      task().finally(() => {
        routeQueueActive -= 1;
        pumpRouteQueue();
      });
    }
  }

  function drawRouteDot(ctx, x, y, color) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  }

  function drawRouteUnavailable(canvas) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = cssVar("--fms-text-muted");
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("No route", canvas.width / 2, canvas.height / 2);
  }

  // A lightweight sparkline, not a tiled map: the polyline is scaled to fill the
  // thumbnail's own bounding box (not real-world map scale) — legible at 88x50px
  // without loading map tiles for every row. See trip_report.py's module docstring
  // for why: N simultaneous tile maps in one table would multiply OSM tile load.
  function drawRouteThumbnail(canvas, points) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!points || points.length === 0) {
      drawRouteUnavailable(canvas);
      return;
    }

    const lats = points.map((p) => parseFloat(p.lat));
    const lons = points.map((p) => parseFloat(p.lon));
    const pad = 6;

    if (points.length === 1) {
      drawRouteDot(ctx, w / 2, h / 2, cssVar("--fms-primary"));
      return;
    }

    const minLat = Math.min(...lats);
    const maxLat = Math.max(...lats);
    const minLon = Math.min(...lons);
    const maxLon = Math.max(...lons);
    const spanLat = Math.max(maxLat - minLat, 0.0001);
    const spanLon = Math.max(maxLon - minLon, 0.0001);
    const toXY = (lat, lon) => [
      pad + ((lon - minLon) / spanLon) * (w - pad * 2),
      h - pad - ((lat - minLat) / spanLat) * (h - pad * 2), // lat increases north; canvas y increases down
    ];

    ctx.strokeStyle = cssVar("--fms-primary");
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    points.forEach((p, index) => {
      const [x, y] = toXY(parseFloat(p.lat), parseFloat(p.lon));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    const [sx, sy] = toXY(lats[0], lons[0]);
    const [ex, ey] = toXY(lats[lats.length - 1], lons[lons.length - 1]);
    drawRouteDot(ctx, sx, sy, cssVar("--fms-success"));
    drawRouteDot(ctx, ex, ey, cssVar("--fms-danger"));
  }

  function loadRouteThumbnail(trip, canvas) {
    const key = routeCacheKey(trip);
    const cached = routeCache.get(key);
    if (cached && cached.status === "COMPLETED") {
      drawRouteThumbnail(canvas, cached.points);
      return;
    }
    const wrap = canvas.closest(".trip-route-thumb-btn");
    wrap?.classList.add("is-loading");
    enqueueRouteFetch(() =>
      fetchRoutePoints(trip)
        .then((data) => {
          routeCache.set(key, { status: trip.status, points: data.points });
          drawRouteThumbnail(canvas, data.points);
        })
        .catch(() => drawRouteUnavailable(canvas))
        .finally(() => wrap?.classList.remove("is-loading"))
    );
  }

  function renderRouteThumbnails(trips) {
    const canvases = document.querySelectorAll("#tripReportTableBody .trip-route-thumb");
    trips.forEach((trip, index) => {
      if (canvases[index]) loadRouteThumbnail(trip, canvases[index]);
    });
  }

  /* ---------------- Route modal (the selected trip's full route) ---------------- */

  function openRouteModal(trip) {
    pendingRouteTrip = trip;
    document.getElementById("tripRouteModalTitle").textContent = `${trip.registration_number} — Trip`;
    document.getElementById("tripRouteModalTripId").textContent = trip.trip_id ? `Trip ID ${trip.trip_id}` : "";
    document.getElementById("tripRouteModalSubtitle").textContent =
      `${trackTime(trip.start_time)} → ${trip.end_time ? trackTime(trip.end_time) : "Ongoing"}`;
    renderTripDetails(trip, null);
    document.getElementById("tripModalAnalysis").innerHTML =
      '<div class="trip-panel-loading"><div class="map-loading-spinner"></div><span>Analysing GPS history…</span></div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById("tripRouteModal")).show();
  }

  /* ---------------- Trip details + analysis panel (MAP popup) ---------------- */

  const analysisCache = new Map(); // COMPLETED trips only — immutable once finished

  function kmText(value, places = 1) {
    return value === null || value === undefined ? "—" : `${parseFloat(value).toFixed(places)} km`;
  }

  function speedText(value) {
    return value === null || value === undefined ? "—" : `${parseFloat(value).toFixed(1)} km/h`;
  }

  function detailRow(label, valueHtml) {
    return `<dt>${escapeHtml(label)}</dt><dd>${valueHtml}</dd>`;
  }

  // Renders straight away from the row's own data; ``analysis`` (when loaded)
  // adds the fields only the server-side GPS walk can provide.
  function renderTripDetails(trip, analysis) {
    const place = (text, lat, lon) => locationHtml(text, lat, lon).replace('class="trip-location"', 'class="trip-location is-wrapped"');
    const distance = analysis ? analysis.distance_km : trip.distance_km;
    const source = analysis && analysis.distance_source ? ` <span class="text-muted-fms">(${escapeHtml(analysis.distance_source)})</span>` : "";
    document.getElementById("tripModalDetails").innerHTML = [
      detailRow("Trip ID", `<span class="font-monospace">${escapeHtml(trip.trip_id || "—")}</span>`),
      detailRow("Vehicle", `${escapeHtml(trip.registration_number)}${trip.vehicle_type ? ` <span class="text-muted-fms">· ${escapeHtml(trip.vehicle_type)}</span>` : ""}`),
      detailRow("Driver", trip.driver_name ? escapeHtml(trip.driver_name) : '<span class="text-muted-fms">Unassigned</span>'),
      detailRow("Status", statusBadgeHtml(trip.status)),
      detailRow("Start", escapeHtml(trackTime(trip.start_time))),
      detailRow("End", endTimeHtml(trip)),
      detailRow("Duration", escapeHtml(durationText(analysis ? analysis.duration_seconds : trip.duration_seconds))),
      detailRow("Distance", `${escapeHtml(kmText(distance, 2))}${source}`),
      detailRow("Start location", place(trip.start_location, trip.start_latitude, trip.start_longitude)),
      detailRow("End location", place(trip.end_location, trip.end_latitude, trip.end_longitude)),
    ].join("");
  }

  function metricTile(label, value, hint) {
    return `
      <div class="trip-metric">
        <span class="trip-metric-label">${escapeHtml(label)}</span>
        <span class="trip-metric-value">${escapeHtml(value)}</span>
        ${hint ? `<span class="trip-metric-hint">${escapeHtml(hint)}</span>` : ""}
      </div>`;
  }

  // Moving / idle / engine-off as one proportional bar — each segment is also
  // labelled in the legend beneath it, so the state never relies on colour alone.
  function activityBarHtml(a) {
    const parts = [
      { key: "moving", label: "Moving", seconds: a.moving_seconds },
      { key: "idle", label: "Idling", seconds: a.idle_seconds },
      { key: "engine-off", label: "Stopped (engine off)", seconds: a.engine_off_seconds },
      { key: "no-data", label: "No data", seconds: a.no_data_seconds },
    ].filter((p) => p.seconds > 0);
    const total = parts.reduce((sum, p) => sum + p.seconds, 0);
    if (!total) return "";
    const bar = parts
      .map((p) => `<span class="trip-activity-seg is-${p.key}" style="flex-grow:${p.seconds}" title="${escapeHtml(`${p.label}: ${durationText(p.seconds)}`)}"></span>`)
      .join("");
    const legend = parts
      .map((p) => `<li><span class="trip-activity-swatch is-${p.key}"></span>${escapeHtml(p.label)} <strong>${escapeHtml(durationText(p.seconds))}</strong> <span class="text-muted-fms">${Math.round((p.seconds / total) * 100)}%</span></li>`)
      .join("");
    return `<div class="trip-activity" role="img" aria-label="Time split by state"><div class="trip-activity-bar">${bar}</div><ul class="trip-activity-legend">${legend}</ul></div>`;
  }

  function renderTripAnalysis(a) {
    const container = document.getElementById("tripModalAnalysis");
    if (!a.gps_points) {
      container.innerHTML = '<p class="text-small text-muted-fms mb-0"><i class="bi bi-info-circle me-1"></i>No GPS records were stored for this trip, so no analysis is available.</p>';
      return;
    }
    const tiles = [
      metricTile("Avg speed", speedText(a.avg_speed_kmh), a.avg_moving_speed_kmh !== null ? `${speedText(a.avg_moving_speed_kmh)} moving` : ""),
      metricTile("Max speed", speedText(a.max_speed_kmh), a.max_speed_time ? `at ${trackTime(a.max_speed_time).split(" ").pop()}` : ""),
      metricTile("Moving", durationText(a.moving_seconds), ""),
      metricTile("Idle", durationText(a.idle_seconds), "engine on"),
      metricTile("Stops", String(a.stop_count), a.longest_stop_seconds ? `longest ${durationText(a.longest_stop_seconds)}` : "≥ 1 min"),
      metricTile("GPS points", a.gps_points.toLocaleString(), `${a.gps_fix_points.toLocaleString()} with fix`),
    ].join("");
    const stops = a.stops.length
      ? `<h5 class="trip-panel-subtitle">Longest stops</h5><ol class="trip-stop-list">${a.stops
          .slice(0, 5)
          .map((s) => `<li><strong>${escapeHtml(durationText(s.duration_seconds))}</strong> <span class="text-muted-fms">${escapeHtml(trackTime(s.start_time).split(" ").pop())} · ${s.engine_off ? "engine off" : "idling"}</span><div class="trip-stop-place">${escapeHtml(s.location || (s.latitude !== null ? `${s.latitude}, ${s.longitude}` : "Location unavailable"))}</div></li>`)
          .join("")}</ol>`
      : "";
    const insights = a.insights.length
      ? `<h5 class="trip-panel-subtitle">Findings</h5><ul class="trip-insight-list">${a.insights
          .map((i) => `<li><span>${escapeHtml(i.label)}</span><strong>${escapeHtml(i.value)}</strong><small>${escapeHtml(i.detail)}</small></li>`)
          .join("")}</ul>`
      : "";
    container.innerHTML = `<div class="trip-metric-grid">${tiles}</div>${activityBarHtml(a)}${stops}${insights}
      <p class="trip-panel-note">Moving = speed ≥ ${a.moving_threshold_kmh} km/h. Computed from the trip's complete GPS history.</p>`;
  }

  function loadTripAnalysis(trip) {
    const key = routeCacheKey(trip);
    const cached = analysisCache.get(key);
    if (cached) {
      renderTripDetails(trip, cached);
      renderTripAnalysis(cached);
      return;
    }
    const params = new URLSearchParams({ vehicle: trip.vehicle_uuid, start: trip.start_time });
    fetch(`${TRIP_ANALYSIS_URL}?${params.toString()}`, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(async (res) => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
        return body;
      })
      .then((analysis) => {
        if (pendingRouteTrip !== trip) return; // modal closed or another trip opened meanwhile
        if (analysis.status === "COMPLETED") analysisCache.set(key, analysis);
        renderTripDetails(trip, analysis);
        renderTripAnalysis(analysis);
      })
      .catch((error) => {
        if (pendingRouteTrip !== trip) return;
        document.getElementById("tripModalAnalysis").innerHTML =
          `<p class="text-small text-muted-fms mb-0"><i class="bi bi-exclamation-triangle me-1"></i>Trip analysis is unavailable right now (${escapeHtml(error.message)}).</p>`;
      });
  }

  function wireTripDownload() {
    const button = document.getElementById("tripModalDownloadBtn");
    document.querySelectorAll("[data-trip-download]").forEach((item) => {
      item.addEventListener("click", () => {
        const trip = pendingRouteTrip;
        if (!trip) return;
        const type = item.dataset.tripDownload;
        const params = new URLSearchParams({ vehicle: trip.vehicle_uuid, start: trip.start_time, type });
        downloadReport(`${TRIP_SINGLE_EXPORT_URL}?${params.toString()}`, { button, fallbackName: `trip_${trip.trip_id || "report"}.${type}` });
      });
    });
  }

  function playbackControls() {
    return {
      play: document.getElementById("tripPlaybackPlay"),
      restart: document.getElementById("tripPlaybackRestart"),
      scrub: document.getElementById("tripPlaybackScrub"),
      speed: document.getElementById("tripPlaybackSpeed"),
      clock: document.getElementById("tripPlaybackClock"),
    };
  }

  function setPlaybackNote(text) {
    const note = document.getElementById("tripPlaybackNote");
    note.textContent = text || "";
    note.classList.toggle("d-none", !text);
  }

  // Stops any running playback and removes the popup map — called before every
  // render and when the popup closes, so one trip's animation can never keep
  // running (or drawing) after another trip is opened.
  function teardownModalMap() {
    if (routePlayer) {
      routePlayer.destroy();
      routePlayer = null;
    }
    if (routeModalMap) {
      routeModalMap.remove();
      routeModalMap = null;
    }
    Object.values(playbackControls()).forEach((el) => {
      if (el && "disabled" in el) el.disabled = true; // buttons/inputs only; the clock has no "disabled"
    });
    setPlaybackNote("");
  }

  function renderModalRoute(data, trip) {
    const container = document.getElementById("tripRouteModalMap");
    const points = (data && data.points) || [];
    teardownModalMap();
    if (typeof L === "undefined") {
      container.innerHTML = '<div class="p-4 text-center text-muted-fms">Map library failed to load. Reload the page and try again.</div>';
      return;
    }
    container.innerHTML = "";
    routeModalMap = L.map(container).setView([20, 0], 2);
    FmsMap.addTileLayer(routeModalMap);

    const start = trip.start_latitude !== null && trip.start_longitude !== null
      ? [parseFloat(trip.start_latitude), parseFloat(trip.start_longitude)]
      : null;

    if (points.length === 0) {
      if (start) {
        routeModalMap.setView(start, 14);
        L.marker(start).addTo(routeModalMap);
      }
      setPlaybackNote("No valid GPS fixes were recorded for this trip, so there is no route to play.");
      return;
    }

    const latlngs = points.map((p) => [parseFloat(p.lat), parseFloat(p.lon)]);
    const primary = cssVar("--fms-primary");
    // The full recorded route, lighter — the playback draws the travelled part solid on top.
    const polyline = L.polyline(latlngs, { color: primary, weight: 4, opacity: 0.35 }).addTo(routeModalMap);
    L.circleMarker(latlngs[0], { radius: 7, color: "#fff", weight: 2, fillColor: cssVar("--fms-success"), fillOpacity: 1 })
      .bindTooltip("Start", { permanent: true, direction: "top", offset: [0, -8], className: "trip-map-label" })
      .addTo(routeModalMap);
    L.circleMarker(latlngs[latlngs.length - 1], {
      radius: 7, color: "#fff", weight: 2, fillColor: cssVar("--fms-danger"), fillOpacity: 1,
    })
      .bindTooltip(trip.status === "ACTIVE" ? "Now" : "End", { permanent: true, direction: "top", offset: [0, -8], className: "trip-map-label" })
      .addTo(routeModalMap);
    routeModalMap.fitBounds(polyline.getBounds().pad(0.15));

    if (points.length < 2) {
      setPlaybackNote("Only one GPS fix was recorded for this trip — there is no movement to play.");
      return;
    }
    routePlayer = FmsRoutePlayback.create({
      map: routeModalMap,
      points,
      controls: playbackControls(),
      color: primary,
      // Time of day only — the trip's date is already in the popup header/footer.
      formatTime: (date) => trackTime(date.toISOString()).split(", ").pop(),
    });
    if (data.truncated) {
      setPlaybackNote(`Long trip: route and playback use ${points.length.toLocaleString("en-GB")} evenly spaced recorded fixes.`);
    }
  }

  function loadAndRenderModalRoute(trip) {
    const container = document.getElementById("tripRouteModalMap");
    teardownModalMap();
    container.innerHTML = '<div class="trip-route-modal-loading"><div class="map-loading-spinner"></div></div>';
    const key = `${routeCacheKey(trip)}|detail`;
    const cached = routeCache.get(key);
    if (cached && cached.status === "COMPLETED") {
      renderModalRoute(cached.data, trip);
      return;
    }
    fetchRoutePoints(trip, undefined, DETAIL_ROUTE_POINTS)
      .then((data) => {
        routeCache.set(key, { status: trip.status, data });
        if (pendingRouteTrip !== trip) return; // closed, or another trip opened meanwhile
        renderModalRoute(data, trip);
      })
      .catch(() => {
        if (pendingRouteTrip !== trip) return;
        container.innerHTML = '<div class="p-4 text-center text-muted-fms">Unable to load this trip’s route right now.</div>';
      });
  }

  function wireRouteModal() {
    const modalEl = document.getElementById("tripRouteModal");
    modalEl.addEventListener("shown.bs.modal", () => {
      if (pendingRouteTrip) {
        loadAndRenderModalRoute(pendingRouteTrip);
        loadTripAnalysis(pendingRouteTrip);
      }
    });
    modalEl.addEventListener("hidden.bs.modal", () => {
      pendingRouteTrip = null;
      teardownModalMap();
      document.getElementById("tripRouteModalMap").innerHTML = "";
    });
  }

  /* ---------------- Table ---------------- */

  function statusBadgeHtml(status) {
    const tone = status === "ACTIVE" ? "success" : "neutral";
    const label = status === "ACTIVE" ? "Active" : "Completed";
    return `<span class="status-pill tone-${tone}"><span class="dot"></span>${label}</span>`;
  }

  function routeThumbHtml(trip) {
    return `
      <button type="button" class="trip-route-thumb-btn" aria-label="View route for ${escapeHtml(trip.registration_number)}" title="View route">
        <canvas class="trip-route-thumb" width="88" height="50"></canvas>
      </button>`;
  }

  // Plain report cell — the Trip Report never navigates away from a row.
  function vehicleCellHtml(trip) {
    return `
      <div class="d-flex align-items-center gap-2">
        <span class="avatar-chip" style="width:30px;height:30px;font-size:11px;">${escapeHtml(initials(trip.driver_name))}</span>
        <span>
          <div class="fw-semibold" style="color:var(--fms-text-primary);">${escapeHtml(trip.registration_number)}</div>
          <div class="cell-secondary">${trip.driver_name ? escapeHtml(trip.driver_name) : "Unassigned"}</div>
        </span>
      </div>`;
  }

  function locationHtml(text, lat, lon) {
    const label = text || (lat !== null && lat !== undefined && lon !== null && lon !== undefined
      ? `${parseFloat(lat).toFixed(5)}, ${parseFloat(lon).toFixed(5)}`
      : "");
    if (!label) return '<span class="text-muted-fms">—</span>';
    return `<span class="trip-location" title="${escapeHtml(label)}">${escapeHtml(label)}</span>`;
  }

  function endTimeHtml(trip) {
    if (trip.end_time) return escapeHtml(trackTime(trip.end_time));
    if (trip.status === "ACTIVE") return '<span class="text-muted-fms">Ongoing</span>';
    return "—";
  }

  function matchesStatusFilter(trip) {
    return !state.filters.statusPill || trip.status === state.filters.statusPill;
  }

  function renderTable() {
    const tbody = document.getElementById("tripReportTableBody");
    const countEl = document.getElementById("tripTableCount");
    if (!tbody) return;

    const visible = state.trips.filter(matchesStatusFilter);
    state.visibleTrips = visible;
    countEl.textContent = `${visible.length} of ${state.trips.length} trip${state.trips.length === 1 ? "" : "s"} shown`;

    tbody.innerHTML = visible
      .map(
        (trip, index) => `
          <tr data-trip-index="${index}">
            <td class="trip-map-cell">${routeThumbHtml(trip)}</td>
            <td>${vehicleCellHtml(trip)}</td>
            <td>${statusBadgeHtml(trip.status)}</td>
            <td class="cell-secondary" title="${escapeHtml(timeAgo(trip.start_time))}">${escapeHtml(trackTime(trip.start_time))}</td>
            <td class="cell-secondary">${endTimeHtml(trip)}</td>
            <td>${escapeHtml(durationText(trip.duration_seconds))}</td>
            <td>${locationHtml(trip.start_location, trip.start_latitude, trip.start_longitude)}</td>
            <td>${locationHtml(trip.end_location, trip.end_latitude, trip.end_longitude)}</td>
            <td class="trip-distance">${trip.distance_km !== null && trip.distance_km !== undefined ? `${parseFloat(trip.distance_km).toFixed(1)} km` : "—"}</td>
          </tr>`
      )
      .join("");

    // Rows are plain report rows (no navigation); the Map thumbnail is the
    // row's one action and opens this trip's popup.
    tbody.querySelectorAll(".trip-route-thumb-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const row = btn.closest("[data-trip-index]");
        const trip = row && state.visibleTrips[Number(row.dataset.tripIndex)];
        if (trip) openRouteModal(trip);
      });
    });

    renderRouteThumbnails(visible);
  }

  /* ---------------- Controls ---------------- */

  function wireControls() {
    state.rangeControl = FmsReportKit.createRangeControl({
      prefix: "trip", storageKey: RANGE_STORAGE_KEY, maxDays: MAX_CUSTOM_DAYS, onChange: refreshNow,
    });
    state.refreshUi = FmsReportKit.createRefreshUi({ root, prefix: "trip", noun: "trip data" });

    const debouncedSearch = FmsReportKit.debounce(() => refreshNow(), SEARCH_DEBOUNCE_MS);
    document.getElementById("tripSearchInput").addEventListener("input", (event) => {
      state.filters.search = event.target.value;
      debouncedSearch();
    });
    document.getElementById("tripVehicleFilter").addEventListener("change", (event) => {
      state.filters.vehicle = event.target.value;
      refreshNow();
    });

    document.getElementById("tripRefreshBtn").addEventListener("click", refreshNow);
    document.getElementById("tripRefreshRetryBtn").addEventListener("click", refreshNow);

    wireRouteModal();
    FmsReportKit.wireDownloadMenu({
      prefix: "trip",
      exportUrl: TRIP_EXPORT_URL,
      rangeControl: state.rangeControl,
      buildParams: () => new URLSearchParams(buildQuery()),
      fallbackName: "trip-report",
    });
    wireTripDownload();

    state.refresher = FmsAutoRefresh.create({
      task: loadTrips,
      select: document.getElementById("tripRefreshInterval"),
      storageKey: REFRESH_STORAGE_KEY,
      defaultMs: DEFAULT_REFRESH_MS,
      onStateChange: (refreshState) => state.refreshUi.onStateChange(refreshState),
    });
  }

  /* ---------------- Boot ---------------- */

  renderStatusPills();
  wireControls();
  state.refresher.start();
})();
