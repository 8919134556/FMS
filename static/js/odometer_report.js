/*
 * Odometer Report — daily Device Odometer vs GPS Odometer per vehicle.
 * Everything shared with the Trip Report (date range, filters, downloads,
 * refresh, errors) comes from FmsReportKit (report_common.js); this file only
 * renders this report's KPIs, status pills and rows. The server sends the
 * already-aggregated rows (apps.tracking.odometer_report) — no GPS points.
 */
(() => {
  "use strict";

  const root = document.getElementById("odometerReportRoot");
  if (!root) return;

  const REPORT_URL = root.dataset.reportUrl;
  const EXPORT_URL = root.dataset.exportUrl;
  const MAX_CUSTOM_DAYS = parseInt(root.dataset.maxCustomDays, 10) || 31;
  const SEARCH_DEBOUNCE_MS = 400;
  // History aggregates don't change second by second: refresh less often by default.
  const DEFAULT_REFRESH_MS = 300000;

  const STATUS_PILLS = [
    { key: "", label: "All", icon: "bi-collection" },
    { key: "ok", label: "OK", icon: "bi-check-circle" },
    { key: "issue", label: "Data issues", icon: "bi-exclamation-triangle" },
    { key: "no_data", label: "No data", icon: "bi-slash-circle" },
  ];

  const { escapeHtml } = FmsFormat;

  const state = {
    rows: [],
    filters: { statusPill: "", search: "", vehicle: "" },
    rangeControl: null,
    refreshUi: null,
    refresher: null,
  };

  function refreshNow() {
    return state.refresher ? state.refresher.refreshNow() : Promise.resolve();
  }

  /* ---------------- Formatting ---------------- */

  const NA = '<span class="text-muted-fms">N/A</span>';

  function km(value, places = 1) {
    if (value === null || value === undefined) return NA;
    return `${Number(value).toLocaleString("en-GB", { minimumFractionDigits: places, maximumFractionDigits: places })} km`;
  }

  function signedKm(value) {
    if (value === null || value === undefined) return "N/A";
    const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
    return `${sign}${Math.abs(value).toLocaleString("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} km`;
  }

  function signedPct(value) {
    if (value === null || value === undefined) return "";
    const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
    return `${sign}${Math.abs(value).toFixed(1)}%`;
  }

  function dateText(iso) {
    return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  }

  /* ---------------- Status pills ---------------- */

  function renderStatusPills() {
    const container = document.getElementById("odoStatusPills");
    const counts = { "": state.rows.length };
    STATUS_PILLS.slice(1).forEach((pill) => {
      counts[pill.key] = state.rows.filter((r) => r.status === pill.key).length;
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

  /* ---------------- Table ---------------- */

  // Plain report cell, like the Trip Report — rows never navigate away.
  function vehicleCellHtml(row) {
    return `
      <div class="fw-semibold" style="color:var(--fms-text-primary);">${escapeHtml(row.registration_number)}</div>
      <div class="cell-secondary">${row.driver_name ? escapeHtml(row.driver_name) : "Unassigned"}</div>`;
  }

  function deviceStartHtml(row) {
    if (row.device_start_km === null) return NA;
    const hint = row.device_start_carried ? "Last reading before this day (value at midnight)" : "First reading of this day";
    return `<span title="${hint}">${km(row.device_start_km)}</span>`;
  }

  function differenceHtml(row) {
    if (row.difference_km === null) return NA;
    const pct = row.difference_pct !== null ? `<div class="cell-secondary">${signedPct(row.difference_pct)}</div>` : "";
    return `<span>${escapeHtml(signedKm(row.difference_km))}</span>${pct}`;
  }

  function notesHtml(row) {
    if (!row.notes.length) return '<span class="status-pill tone-success"><span class="dot"></span>OK</span>';
    return `<ul class="odo-notes">${row.notes
      .map((n) => `<li class="is-${n.level}"><i class="bi ${n.level === "warning" ? "bi-exclamation-triangle" : "bi-info-circle"}" aria-hidden="true"></i>${escapeHtml(n.text)}</li>`)
      .join("")}</ul>`;
  }

  function matchesStatus(row) {
    return !state.filters.statusPill || row.status === state.filters.statusPill;
  }

  function renderTable() {
    const tbody = document.getElementById("odometerReportTableBody");
    const visible = state.rows.filter(matchesStatus);
    document.getElementById("odoTableCount").textContent =
      `${visible.length} of ${state.rows.length} vehicle-day${state.rows.length === 1 ? "" : "s"} shown`;
    tbody.innerHTML = visible
      .map(
        (row) => `
          <tr class="odo-row is-${row.status}">
            <td class="cell-secondary text-nowrap">${escapeHtml(dateText(row.date))}</td>
            <td>${vehicleCellHtml(row)}</td>
            <td class="odo-num">${deviceStartHtml(row)}</td>
            <td class="odo-num">${km(row.device_end_km)}</td>
            <td class="odo-num fw-semibold">${km(row.device_distance_km)}</td>
            <td class="odo-num">${km(row.gps_start_km)}</td>
            <td class="odo-num">${km(row.gps_end_km)}</td>
            <td class="odo-num fw-semibold">${km(row.gps_distance_km)}</td>
            <td class="odo-num">${differenceHtml(row)}</td>
            <td>${notesHtml(row)}</td>
          </tr>`
      )
      .join("");
  }

  function toggleEmptyState(isEmpty) {
    document.getElementById("odometerReportEmpty").classList.toggle("d-none", !isEmpty);
    document.getElementById("odometerReportTable").closest(".table-responsive-fms").classList.toggle("d-none", isEmpty);
  }

  /* ---------------- Data ---------------- */

  function buildQuery() {
    const params = state.rangeControl.appendTo(new URLSearchParams());
    if (state.filters.vehicle) params.set("vehicle", state.filters.vehicle);
    if (state.filters.search) params.set("q", state.filters.search);
    return params;
  }

  function applyData(data) {
    state.rows = data.results;
    state.refreshUi.markUpdated();
    state.rangeControl.showPeriod(data.range);
    renderStatusPills();
    renderTable();
    toggleEmptyState(data.count === 0);
  }

  function loadReport({ signal } = {}) {
    return FmsReportKit.loadJson(`${REPORT_URL}?${buildQuery().toString()}`, { signal, noun: "odometer data", render: applyData });
  }

  /* ---------------- Boot ---------------- */

  state.rangeControl = FmsReportKit.createRangeControl({
    prefix: "odo", storageKey: "fms.odometerReport.range", maxDays: MAX_CUSTOM_DAYS, onChange: refreshNow,
  });
  state.refreshUi = FmsReportKit.createRefreshUi({ root, prefix: "odo", noun: "odometer data" });

  const debouncedSearch = FmsReportKit.debounce(() => refreshNow(), SEARCH_DEBOUNCE_MS);
  document.getElementById("odoSearchInput").addEventListener("input", (event) => {
    state.filters.search = event.target.value;
    debouncedSearch();
  });
  document.getElementById("odoVehicleFilter").addEventListener("change", (event) => {
    state.filters.vehicle = event.target.value;
    refreshNow();
  });
  document.getElementById("odoRefreshBtn").addEventListener("click", refreshNow);
  document.getElementById("odoRefreshRetryBtn").addEventListener("click", refreshNow);

  FmsReportKit.wireDownloadMenu({
    prefix: "odo", exportUrl: EXPORT_URL, rangeControl: state.rangeControl, buildParams: buildQuery, fallbackName: "odometer-report",
  });

  state.refresher = FmsAutoRefresh.create({
    task: loadReport,
    select: document.getElementById("odoRefreshInterval"),
    storageKey: "fms.odometerReport.refreshMs",
    defaultMs: DEFAULT_REFRESH_MS,
    onStateChange: (refreshState) => state.refreshUi.onStateChange(refreshState),
  });

  renderStatusPills();
  state.refresher.start();
})();
