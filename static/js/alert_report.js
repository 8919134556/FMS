/*
 * Alert Report — vehicle alert events (apps.alerts.report). Everything is
 * filtered, counted, sorted and paged on the server; the browser holds one
 * page. Range bar, downloads, auto-refresh and error banner come from the
 * shared report kit (report_common.js / auto_refresh.js), like the other
 * reports. A new alert announced by the bell (``fms:alert``) refreshes the
 * table at once. ``?alert=<uuid>`` (the notification link) opens that alert.
 */
(() => {
  "use strict";

  const root = document.getElementById("alertReportRoot");
  if (!root) return;

  const REPORT_URL = root.dataset.reportUrl;
  const EXPORT_URL = root.dataset.exportUrl;
  const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
  const MAX_CUSTOM_DAYS = parseInt(root.dataset.maxCustomDays, 10) || 31;
  const DISPLAY_TZ = root.dataset.timezone || undefined;
  const { escapeHtml, durationText } = FmsFormat;
  const csrfToken = document.cookie.match(/csrftoken=([^;]+)/)?.[1];
  const DASH = '<span class="text-muted-fms">—</span>';

  const COLUMNS = [
    { key: "time", label: "Time", sort: "time" },
    { key: "vehicle", label: "Vehicle / Driver", sort: "vehicle" },
    { key: "type", label: "Alert / Severity", sort: "severity" },
    { key: "duration", label: "Duration", num: true },
    { key: "location", label: "Location" },
    { key: "speed", label: "Speed", sort: "speed", num: true },
    { key: "voltage", label: "Voltage", sort: "voltage", num: true },
    { key: "status", label: "Status", sort: "status" },
    { key: "action", label: "", narrow: true },
  ];

  const STATUS_PILLS = [
    { key: "", label: "All", icon: "bi-collection", count: "total" },
    { key: "OPEN", label: "New", icon: "bi-exclamation-octagon", count: "open" },
    { key: "ACKNOWLEDGED", label: "Acknowledged", icon: "bi-eye", count: "acknowledged" },
    { key: "RESOLVED", label: "Resolved", icon: "bi-check-circle", count: "resolved" },
  ];
  const STATUS_TONES = { OPEN: "tone-danger", ACKNOWLEDGED: "tone-warning", RESOLVED: "tone-success" };
  // Per alert type: pill tone + icon. A new type falls back to the severity's tone.
  const TYPE_STYLES = {
    PANIC: { tone: "tone-danger", icon: "bi-exclamation-octagon-fill" },
    IDLE: { tone: "tone-warning", icon: "bi-hourglass-split" },
  };
  const SEVERITY_TONES = { CRITICAL: "tone-danger", HIGH: "tone-danger", MEDIUM: "tone-warning", LOW: "tone-info" };

  const state = {
    rangeControl: null,
    refreshUi: null,
    refresher: null,
    filters: { vehicle: "", type: "", severity: "", status: "" },
    page: 1,
    pageSize: parseInt(document.getElementById("alrPageSize").value, 10) || 50,
    sort: "time",
    dir: "desc",
    rows: [],
    summary: null,
    canUpdate: false,
    detail: null, // alert shown in the modal
    map: null,
  };

  /* ---------------- Formatting ---------------- */

  function num(value, places) {
    return Number(value).toLocaleString("en-GB", { minimumFractionDigits: places, maximumFractionDigits: places });
  }

  function trackTime(iso) {
    return FmsFormat.trackTime(iso, DISPLAY_TZ);
  }

  function statusPill(row) {
    return `<span class="status-pill ${STATUS_TONES[row.status] || "tone-neutral"}"><span class="dot"></span>${escapeHtml(row.status_label)}</span>`;
  }

  function locationText(row) {
    if (row.location) return row.location;
    if (row.latitude !== null) return `${num(row.latitude, 5)}, ${num(row.longitude, 5)}`;
    return "";
  }

  function cellHtml(col, row) {
    switch (col.key) {
      case "time": {
        // Date over time keeps the column narrow enough for the whole table to fit.
        const when = row.occurred_at ? new Date(row.occurred_at) : null;
        if (!when) return DASH;
        const date = when.toLocaleDateString("en-GB", { timeZone: DISPLAY_TZ, day: "2-digit", month: "short", year: "numeric" });
        const time = when.toLocaleTimeString("en-GB", { timeZone: DISPLAY_TZ, hour12: false });
        return `<span class="text-nowrap" title="${escapeHtml(trackTime(row.occurred_at))}">${escapeHtml(date)}</span>`
          + `<span class="d-block text-small text-muted-fms">${escapeHtml(time)}</span>`;
      }
      case "vehicle":
        return `<span class="fw-semibold">${escapeHtml(row.registration_number)}</span>`
          + `<span class="d-block text-small text-muted-fms">${row.driver ? escapeHtml(row.driver) : "No driver"}</span>`;
      case "type": {
        const style = TYPE_STYLES[row.type] || { tone: SEVERITY_TONES[row.severity] || "tone-neutral", icon: "bi-bell-fill" };
        const severityTone = (SEVERITY_TONES[row.severity] || "tone-neutral").replace("tone-", "text-");
        return `<span class="status-pill ${style.tone}"><i class="bi ${style.icon} me-1" aria-hidden="true"></i>${escapeHtml(row.type_label)}</span>`
          + `<span class="d-block text-small mt-1"><span class="fw-semibold ${severityTone}">${escapeHtml(row.severity_label)}</span>`
          + `${row.signal_active ? ` · <span class="fw-semibold ${row.type === "PANIC" ? "text-danger" : "text-warning"}">active</span>` : ""}</span>`;
      }
      case "duration": return row.duration_text ? `<span class="text-nowrap">${escapeHtml(row.duration_text)}</span>` : DASH;
      case "location": {
        const text = locationText(row);
        return text ? `<span class="trip-location" title="${escapeHtml(text)}">${escapeHtml(text)}</span>` : DASH;
      }
      case "speed": return row.speed === null ? DASH : `${num(row.speed, 1)} km/h`;
      case "voltage": return row.voltage === null ? DASH : `${num(row.voltage, 2)} V`;
      case "status": return statusPill(row);
      case "action":
        return `<button type="button" class="btn btn-sm btn-outline-primary alert-detail-btn" data-detail="${row.uuid}" aria-label="Details of the ${escapeHtml(row.type_label)} alert on ${escapeHtml(row.registration_number)}" title="Details"><i class="bi bi-chevron-right" aria-hidden="true"></i></button>`;
      default: return DASH;
    }
  }

  /* ---------------- Data ---------------- */

  function buildQuery() {
    const params = state.rangeControl.appendTo(new URLSearchParams());
    if (state.filters.vehicle) params.set("vehicle", state.filters.vehicle);
    if (state.filters.type) params.set("alert_type", state.filters.type);
    if (state.filters.severity) params.set("severity", state.filters.severity);
    if (state.filters.status) params.set("status", state.filters.status);
    return params;
  }

  function loadAlerts({ signal } = {}) {
    const params = buildQuery();
    params.set("page", state.page);
    params.set("page_size", state.pageSize);
    params.set("sort", state.sort);
    params.set("dir", state.dir);
    return FmsReportKit.loadJson(`${REPORT_URL}?${params.toString()}`, { signal, noun: "alerts", render: applyData });
  }

  function refreshNow() {
    return state.refresher ? state.refresher.refreshNow() : Promise.resolve();
  }

  function reloadFromFirstPage() {
    state.page = 1;
    refreshNow();
  }

  function applyData(data) {
    state.summary = data.summary;
    state.canUpdate = data.can_update;
    state.rows = data.alerts.rows;
    state.refreshUi.markUpdated();
    state.rangeControl.showPeriod(data.selection.range);
    renderSummary(data.summary);
    renderStatusPills();
    renderTable(data.alerts);
  }

  function renderSummary(summary) {
    document.getElementById("alrKpiTotal").textContent = summary.total.toLocaleString("en-GB");
    document.getElementById("alrKpiOpen").textContent = summary.open.toLocaleString("en-GB");
    document.getElementById("alrKpiAck").textContent = summary.acknowledged.toLocaleString("en-GB");
    document.getElementById("alrKpiResolved").textContent = summary.resolved.toLocaleString("en-GB");
  }

  function renderStatusPills() {
    const container = document.getElementById("alrStatusPills");
    const s = state.summary || {};
    container.innerHTML = STATUS_PILLS.map((pill) => {
      const active = state.filters.status === pill.key;
      return `
        <button type="button" class="live-status-pill${active ? " active" : ""}" data-status="${pill.key}" role="tab" aria-selected="${active}">
          <i class="bi ${pill.icon}"></i><span>${pill.label}</span>
          <span class="live-status-pill-count">${(s[pill.count] ?? 0).toLocaleString("en-GB")}</span>
        </button>`;
    }).join("");
    container.querySelectorAll("[data-status]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.filters.status = btn.dataset.status;
        reloadFromFirstPage();
      });
    });
  }

  function renderHeader() {
    document.getElementById("alrHeadRow").innerHTML = COLUMNS.map((c) => {
      const cls = c.num ? ' class="text-end"' : "";
      if (c.narrow) return '<th class="alert-action-col"><span class="visually-hidden">Details</span></th>';
      if (!c.sort) return `<th${cls}>${c.label}</th>`;
      const active = state.sort === c.sort;
      const aria = active ? (state.dir === "asc" ? "ascending" : "descending") : "none";
      const icon = active ? (state.dir === "asc" ? "bi-sort-up" : "bi-sort-down") : "bi-arrow-down-up";
      return `<th aria-sort="${aria}"${cls}><button type="button" class="loc-sort${active ? " is-active" : ""}" data-sort="${c.sort}">${c.label}<i class="bi ${icon}" aria-hidden="true"></i></button></th>`;
    }).join("");
    document.querySelectorAll("#alrHeadRow [data-sort]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.sort;
        state.dir = state.sort === key && state.dir === "desc" ? "asc" : "desc";
        state.sort = key;
        reloadFromFirstPage();
      });
    });
  }

  function renderTable(alerts) {
    renderHeader();
    const { total, page, pages, page_size: size } = alerts;
    state.page = page;
    const first = total ? (page - 1) * size + 1 : 0;
    const last = Math.min(page * size, total);
    document.getElementById("alrTableCount").textContent = total
      ? `Showing ${first.toLocaleString("en-GB")}–${last.toLocaleString("en-GB")} of ${total.toLocaleString("en-GB")}`
      : "0 alerts";
    const tbody = document.getElementById("alrTableBody");
    tbody.innerHTML = alerts.rows
      .map((row) => `<tr class="alert-report-row${row.status === "OPEN" ? " is-new" : ""}" data-row="${row.uuid}">${COLUMNS
        .map((c) => `<td class="${c.num ? "text-end text-nowrap" : ""}${c.narrow ? " alert-action-col" : ""}">${cellHtml(c, row)}</td>`).join("")}</tr>`)
      .join("");
    document.getElementById("alrEmpty").classList.toggle("d-none", total !== 0);
    document.getElementById("alrTable").closest(".table-responsive-fms").classList.toggle("d-none", total === 0);
    tbody.querySelectorAll("tr[data-row]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const row = state.rows.find((r) => r.uuid === tr.dataset.row);
        if (row) openDetail(row);
      });
    });
    renderPagination(page, pages);
  }

  function renderPagination(page, pages) {
    const nav = document.getElementById("alrPagination");
    if (pages <= 1) {
      nav.innerHTML = "";
      return;
    }
    const sorted = [...new Set([1, pages, page - 1, page, page + 1].filter((p) => p >= 1 && p <= pages))].sort((x, y) => x - y);
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
        refreshNow();
      });
    });
  }

  /* ---------------- Details modal ---------------- */

  const modalEl = document.getElementById("alrDetailModal");
  const modal = window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalEl) : null;

  function detailRow(label, value) {
    return `<dt>${label}</dt><dd>${value}</dd>`;
  }

  // Panic signals often last seconds, which "0m" would hide.
  function signalDuration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    return seconds < 60 ? `${seconds}s` : durationText(seconds);
  }

  function ignitionText(value) {
    if (value === true) return '<span class="ignition-indicator is-on"><span class="dot"></span>ON</span>';
    if (value === false) return '<span class="ignition-indicator is-off"><span class="dot"></span>OFF</span>';
    return DASH;
  }

  function openDetail(row) {
    state.detail = row;
    const time = row.occurred_at ? new Date(row.occurred_at) : null;
    const dateOnly = time ? time.toLocaleDateString("en-GB", { timeZone: DISPLAY_TZ, day: "2-digit", month: "short", year: "numeric" }) : "";
    const timeOnly = time ? time.toLocaleTimeString("en-GB", { timeZone: DISPLAY_TZ, hour12: false }) : "";
    document.getElementById("alrDetailTitle").textContent = `${row.type_label} Alert — ${row.registration_number}`;
    document.getElementById("alrDetailSubtitle").textContent = trackTime(row.occurred_at);
    const isIdle = row.type === "IDLE";
    const ongoing = `<span class="fw-semibold ${isIdle ? "text-warning" : "text-danger"}">${isIdle ? "Still idling" : "Still active"}</span>`;
    const lifecycle = isIdle
      ? [
        detailRow("Idle start", escapeHtml(trackTime(row.occurred_at))),
        detailRow("Alert generated", escapeHtml(trackTime(row.triggered_at))),
        detailRow("Idle end", row.signal_active ? ongoing : escapeHtml(trackTime(row.signal_cleared_at))),
        detailRow("Idle duration", `${escapeHtml(row.duration_text || "—")}${row.signal_active ? " so far" : ""}`),
      ]
      : [
        detailRow("Date", escapeHtml(dateOnly) || DASH),
        detailRow("Time", escapeHtml(timeOnly) || DASH),
        detailRow("Signal", row.signal_active
          ? ongoing
          : `Cleared ${escapeHtml(trackTime(row.signal_cleared_at))}${row.duration_seconds !== null ? ` (after ${escapeHtml(signalDuration(row.duration_seconds))})` : ""}`),
      ];
    document.getElementById("alrDetailList").innerHTML = [
      detailRow("Alert type", escapeHtml(row.type_label)),
      detailRow("Severity", `<span class="status-pill ${SEVERITY_TONES[row.severity] || "tone-neutral"}"><span class="dot"></span>${escapeHtml(row.severity_label)}</span>`),
      detailRow("Status", statusPill(row)),
      detailRow("Vehicle", `<strong>${escapeHtml(row.registration_number)}</strong>`),
      detailRow("Driver", row.driver ? escapeHtml(row.driver) : DASH),
      row.client ? detailRow("Client", escapeHtml(row.client)) : "",
      ...lifecycle,
      detailRow("Location", row.location ? escapeHtml(row.location) : DASH),
      detailRow("Latitude", row.latitude === null ? DASH : num(row.latitude, 6)),
      detailRow("Longitude", row.longitude === null ? DASH : num(row.longitude, 6)),
      detailRow("Speed", row.speed === null ? DASH : `${num(row.speed, 1)} km/h`),
      isIdle ? "" : detailRow("Voltage", row.voltage === null ? DASH : `${num(row.voltage, 2)} V`),
      detailRow("Ignition", ignitionText(row.ignition)),
      detailRow("Odometer", row.odometer === null ? DASH : `${num(row.odometer, 1)} km`),
    ].join("");
    const workflow = [];
    if (row.acknowledged_at) workflow.push(`Acknowledged ${trackTime(row.acknowledged_at)}${row.acknowledged_by ? ` by ${row.acknowledged_by}` : ""}`);
    if (row.resolved_at) workflow.push(`Resolved ${trackTime(row.resolved_at)}${row.resolved_by ? ` by ${row.resolved_by}` : ""}`);
    document.getElementById("alrDetailWorkflow").textContent = workflow.join(" · ");
    const ack = document.getElementById("alrAckBtn");
    const resolve = document.getElementById("alrResolveBtn");
    if (ack) ack.classList.toggle("d-none", !state.canUpdate || row.status !== "OPEN");
    if (resolve) resolve.classList.toggle("d-none", !state.canUpdate || row.status === "RESOLVED");
    modal?.show();
  }

  function renderDetailMap() {
    const row = state.detail;
    const note = document.getElementById("alrDetailMapNote");
    const mapEl = document.getElementById("alrDetailMap");
    if (state.map) {
      state.map.remove();
      state.map = null;
    }
    if (!row || row.latitude === null || typeof L === "undefined") {
      mapEl.classList.add("d-none");
      note.textContent = row && row.latitude === null ? "The device had no GPS fix when the alert was raised." : "";
      return;
    }
    mapEl.classList.remove("d-none");
    note.textContent = row.type === "IDLE" ? "Where the vehicle stood idling." : "Position of the reading that raised the alert.";
    state.map = L.map(mapEl, { zoomControl: true }).setView([row.latitude, row.longitude], 16);
    FmsMap.addTileLayer(state.map);
    const color = getComputedStyle(document.documentElement)
      .getPropertyValue(row.type === "IDLE" ? "--fms-warning" : "--fms-danger").trim() || "#dc2626";
    L.circleMarker([row.latitude, row.longitude], { radius: 10, color: "#fff", weight: 3, fillColor: color, fillOpacity: 1 })
      .bindTooltip(`${row.type_label} · ${row.registration_number}`, { permanent: true, direction: "top", offset: [0, -10] })
      .addTo(state.map);
  }

  modalEl.addEventListener("shown.bs.modal", renderDetailMap);
  modalEl.addEventListener("hidden.bs.modal", () => {
    if (state.map) {
      state.map.remove();
      state.map = null;
    }
  });

  async function changeStatus(templateAttr, okMessage) {
    const row = state.detail;
    if (!row) return;
    const url = root.dataset[templateAttr].replace(ZERO_UUID, row.uuid);
    try {
      const response = await fetch(url, { method: "POST", headers: { "X-CSRFToken": csrfToken, Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      FmsReportKit.showToast(okMessage, "success");
      modal?.hide();
      refreshNow();
    } catch (failed) {
      FmsReportKit.showToast("The alert could not be updated. Reload the page and try again.", "danger");
    }
  }

  document.getElementById("alrAckBtn")?.addEventListener("click", () => changeStatus("ackUrlTemplate", "Alert acknowledged."));
  document.getElementById("alrResolveBtn")?.addEventListener("click", () => changeStatus("resolveUrlTemplate", "Alert resolved."));

  // Opened from a notification: show that alert even if it is outside the current filters.
  async function openFromQuery() {
    const uuid = new URLSearchParams(window.location.search).get("alert");
    if (!uuid) return;
    try {
      const response = await fetch(root.dataset.detailUrlTemplate.replace(ZERO_UUID, encodeURIComponent(uuid)), {
        headers: { Accept: "application/json" }, cache: "no-store",
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        FmsReportKit.showToast(body.detail || "This alert could not be opened.", "warning");
        return;
      }
      state.canUpdate = body.can_update;
      openDetail(body.alert);
    } catch (networkError) {
      FmsReportKit.showToast("Cannot reach the server to open the alert.", "danger");
    }
  }

  /* ---------------- Boot ---------------- */

  state.rangeControl = FmsReportKit.createRangeControl({
    prefix: "alr", storageKey: "fms.alertReport.range", maxDays: MAX_CUSTOM_DAYS, onChange: reloadFromFirstPage,
  });
  state.refreshUi = FmsReportKit.createRefreshUi({ root, prefix: "alr", noun: "alerts" });
  document.getElementById("alrVehicleFilter").addEventListener("change", (event) => {
    state.filters.vehicle = event.target.value;
    reloadFromFirstPage();
  });
  document.getElementById("alrSeverityFilter").addEventListener("change", (event) => {
    state.filters.severity = event.target.value;
    reloadFromFirstPage();
  });
  document.getElementById("alrTypeFilter").addEventListener("change", (event) => {
    state.filters.type = event.target.value;
    reloadFromFirstPage();
  });
  document.getElementById("alrPageSize").addEventListener("change", (event) => {
    state.pageSize = parseInt(event.target.value, 10) || 50;
    reloadFromFirstPage();
  });
  document.getElementById("alrRefreshBtn").addEventListener("click", refreshNow);
  document.getElementById("alrRefreshRetryBtn").addEventListener("click", refreshNow);
  window.addEventListener("fms:alert", () => refreshNow());

  // Downloads = the applied range + vehicle / alert type / status — every matching alert.
  FmsReportKit.wireDownloadMenu({
    prefix: "alr",
    exportUrl: EXPORT_URL,
    rangeControl: state.rangeControl,
    buildParams: buildQuery,
    fallbackName: "alert-report",
  });

  state.refresher = FmsAutoRefresh.create({
    task: loadAlerts,
    select: document.getElementById("alrRefreshInterval"),
    storageKey: "fms.alertReport.refreshMs",
    defaultMs: 30000,
    onStateChange: (refreshState) => state.refreshUi.onStateChange(refreshState),
  });
  renderStatusPills();
  state.refresher.start();
  openFromQuery();
})();
