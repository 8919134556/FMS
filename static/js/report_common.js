/*
 * FmsReportKit — the pieces every page under Reports shares (Trip Report,
 * Odometer Report, …): the date-range bar, custom-range validation, toasts,
 * the PDF/Excel download menu, the auto-refresh indicator and the JSON loader.
 *
 * Each page renders the same markup (templates/tracking/_report_header.html,
 * _report_range_bar.html) with its own element-id ``prefix`` ("trip", "odo",
 * …), so one implementation drives all of them and a new report only writes
 * its own table/summary rendering.
 */
(function (global) {
  "use strict";

  const { escapeHtml, timeAgo } = global.FmsFormat;

  const RANGE_LABELS = {
    today: "Today", yesterday: "Yesterday", last3: "Last 3 Days", last5: "Last 5 Days", last7: "Last 7 Days", custom: "Custom Range",
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function todayIsoDate() {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  }

  // Mirrors the server-side checks in trip_analytics.validate_export_range, so
  // the user hears about a bad range immediately instead of after a request.
  function customRangeProblem(from, to, maxDays) {
    if (!from || !to) return "Select both a start date and an end date.";
    if (from > to) return "The start date must be on or before the end date.";
    if (from > todayIsoDate()) return "The start date is in the future — there is no history to show yet.";
    const days = Math.round((Date.parse(to) - Date.parse(from)) / 86400000) + 1;
    if (days > maxDays) return `A custom range can cover at most ${maxDays} days.`;
    return "";
  }

  /* ---------------- Toasts (same markup as components/_toast_item.html) ---------------- */

  const TOAST_ICONS = { danger: "x-circle-fill", warning: "exclamation-triangle-fill", success: "check-circle-fill", info: "info-circle-fill" };

  function showToast(message, tone = "info") {
    let container = byId("reportToastContainer");
    if (!container) {
      container = document.createElement("div");
      container.id = "reportToastContainer";
      container.className = "toast-container position-fixed top-0 end-0 p-3";
      container.style.zIndex = "var(--fms-z-toast, 1090)";
      document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = "toast fms-toast show align-items-center border-0 mb-2";
    toast.setAttribute("role", tone === "danger" || tone === "warning" ? "alert" : "status");
    toast.innerHTML = `
      <div class="d-flex">
        <div class="toast-body d-flex align-items-start gap-2">
          <i class="bi bi-${TOAST_ICONS[tone] || TOAST_ICONS.info}" style="color: var(--fms-${tone}); font-size: 1rem; margin-top: 1px;"></i>
          <span style="overflow-wrap:anywhere; min-width:0;">${escapeHtml(message)}</span>
        </div>
        <button type="button" class="btn-close me-2 m-auto" aria-label="Close"></button>
      </div>`;
    const remove = () => toast.remove();
    toast.querySelector(".btn-close").addEventListener("click", remove);
    container.appendChild(toast);
    window.setTimeout(remove, tone === "danger" ? 9000 : 6000);
  }

  /* ---------------- Downloads (PDF / Excel) ---------------- */

  function filenameFrom(response, fallback) {
    const header = response.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(header);
    return match ? match[1] : fallback;
  }

  // Fetch (not a plain link) so a "no data" / "range too large" answer shows up
  // as a readable message on the page instead of a downloaded error file.
  async function downloadReport(url, { button, fallbackName }) {
    if (button?.dataset.busy === "1") return;
    const originalHtml = button ? button.innerHTML : "";
    if (button) {
      button.dataset.busy = "1";
      button.disabled = true;
      button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Preparing report…';
    }
    try {
      let response;
      try {
        response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      } catch (networkError) {
        throw new Error("Cannot reach the server. Check your connection and try again.");
      }
      if (!response.ok) {
        let detail = "";
        try {
          detail = (await response.json()).detail || "";
        } catch (notJson) {
          /* fall through to the generic message */
        }
        if (response.status === 429) detail = "Too many downloads in a short time. Please wait a minute and try again.";
        if (response.status === 401 || response.status === 403) detail = "Your session expired or you no longer have access. Reload the page and sign in again.";
        const tone = response.status === 404 ? "info" : "danger";
        showToast(detail || `The report could not be generated (HTTP ${response.status}).`, tone);
        return;
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filenameFrom(response, fallbackName);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(link.href), 30000);
      showToast(`Downloaded ${link.download}`, "success");
    } catch (error) {
      showToast(error.message || "The report could not be generated.", "danger");
    } finally {
      if (button) {
        button.innerHTML = originalHtml;
        button.disabled = false;
        delete button.dataset.busy;
      }
    }
  }

  /* ---------------- Date-range bar ---------------- */

  // Owns the range pills, the custom From/To inputs + Apply, and persistence.
  // ``onChange`` fires whenever a new range is committed (pill or Apply).
  function createRangeControl({ prefix, storageKey, maxDays, onChange }) {
    let range = { key: "today", from: "", to: "" };
    const pills = () => document.querySelectorAll(`#${prefix}RangeBar .trip-range-pill`);

    function applyToUi() {
      pills().forEach((btn) => btn.classList.toggle("active", btn.dataset.range === range.key));
      byId(`${prefix}CustomRange`).classList.toggle("d-none", range.key !== "custom");
    }

    function persist() {
      try {
        localStorage.setItem(storageKey, JSON.stringify(range));
      } catch (unavailable) {
        /* private mode / storage disabled — the choice just won't persist */
      }
    }

    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (stored && stored.key && RANGE_LABELS[stored.key]) range = stored;
    } catch (unavailable) {
      /* corrupt/blocked storage — keep the "today" default */
    }
    if (range.key === "custom") {
      byId(`${prefix}RangeFrom`).value = range.from || "";
      byId(`${prefix}RangeTo`).value = range.to || "";
    }
    applyToUi();

    pills().forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.range;
        if (key === "custom") {
          range = { ...range, key: "custom" };
          const fromInput = byId(`${prefix}RangeFrom`);
          const toInput = byId(`${prefix}RangeTo`);
          if (!fromInput.value) fromInput.value = todayIsoDate();
          if (!toInput.value) toInput.value = todayIsoDate();
          applyToUi();
          return; // waits for "Apply" — picking dates isn't a commitment yet
        }
        range = { key, from: "", to: "" };
        persist();
        applyToUi();
        onChange();
      });
    });

    // Pages that apply filters with their own button (Location Data Report)
    // render no custom-range Apply and call commitCustomInputs() instead.
    byId(`${prefix}CustomApply`)?.addEventListener("click", () => {
      const from = byId(`${prefix}RangeFrom`).value;
      const to = byId(`${prefix}RangeTo`).value;
      const problem = customRangeProblem(from, to, maxDays);
      if (problem) {
        showToast(problem, "warning");
        return;
      }
      range = { key: "custom", from, to };
      persist();
      onChange();
    });

    return {
      get range() {
        return range;
      },
      appendTo(params) {
        params.set("range", range.key);
        if (range.key === "custom") {
          params.set("from", range.from);
          params.set("to", range.to);
        }
        return params;
      },
      // The download menu header: "Last 7 Days · 21 Sep 2026 – 27 Sep 2026"
      // (``apiRange`` is the server's resolved {start, end}).
      showPeriod(apiRange) {
        const el = byId(`${prefix}DownloadPeriod`);
        if (!el || !apiRange) return;
        const fmt = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
        const span = apiRange.start === apiRange.end ? fmt(apiRange.start) : `${fmt(apiRange.start)} – ${fmt(apiRange.end)}`;
        el.textContent = `${RANGE_LABELS[range.key] || "Selected period"} · ${span}`;
      },
      // Reads the From/To inputs into the range when Custom is selected;
      // returns a problem message (and changes nothing) if they're invalid.
      commitCustomInputs() {
        if (range.key !== "custom") return "";
        const from = byId(`${prefix}RangeFrom`).value;
        const to = byId(`${prefix}RangeTo`).value;
        const problem = customRangeProblem(from, to, maxDays);
        if (problem) return problem;
        range = { key: "custom", from, to };
        persist();
        return "";
      },
      // For downloads: a custom range must be valid and applied first.
      problemForDownload() {
        if (range.key !== "custom") return "";
        const problem = customRangeProblem(range.from, range.to, maxDays);
        return problem ? `${problem} Then click Apply before downloading.` : "";
      },
    };
  }

  // The range bar's "Download Report → PDF | Excel" menu.
  // ``precheck`` (optional) returns a message when a download isn't possible yet.
  function wireDownloadMenu({ prefix, exportUrl, rangeControl, buildParams, fallbackName, precheck }) {
    const button = byId(`${prefix}DownloadBtn`);
    document.querySelectorAll(`#${prefix}RangeBar [data-report-download]`).forEach((item) => {
      item.addEventListener("click", () => {
        const problem = (precheck && precheck()) || rangeControl.problemForDownload();
        if (problem) {
          showToast(problem, "warning");
          return;
        }
        const params = buildParams();
        params.set("type", item.dataset.reportDownload);
        downloadReport(`${exportUrl}?${params.toString()}`, { button, fallbackName: `${fallbackName}.${item.dataset.reportDownload}` });
      });
    });
  }

  /* ---------------- Auto-refresh indicator + error banner ---------------- */

  const REFRESH_INDICATOR_MIN_MS = 500;

  function createRefreshUi({ root, prefix, noun }) {
    let since = 0;
    let clearTimer = null;
    let lastUpdatedAt = null;

    function setLoading(isLoading) {
      const apply = (on) => {
        byId(`${prefix}RefreshBtn`)?.classList.toggle("is-refreshing", on);
        byId(`${prefix}RefreshDot`)?.classList.toggle("is-active", on);
        root.setAttribute("aria-busy", on ? "true" : "false");
      };
      if (isLoading) {
        window.clearTimeout(clearTimer);
        since = Date.now();
        apply(true);
      } else {
        clearTimer = window.setTimeout(() => apply(false), Math.max(0, REFRESH_INDICATOR_MIN_MS - (Date.now() - since)));
      }
    }

    function showError(reason) {
      const text = byId(`${prefix}RefreshErrorText`);
      if (text) text.textContent = reason ? `Unable to refresh ${noun} — ${reason}` : `Unable to refresh ${noun}`;
      byId(`${prefix}RefreshError`).classList.remove("d-none");
    }

    function hideError() {
      byId(`${prefix}RefreshError`).classList.add("d-none");
    }

    function tick() {
      const el = byId(`${prefix}LastUpdated`);
      if (el) el.textContent = lastUpdatedAt ? `Updated ${timeAgo(lastUpdatedAt.toISOString())}` : "Not yet loaded";
    }

    const tickTimerId = window.setInterval(tick, 5000);
    window.addEventListener("pagehide", () => window.clearInterval(tickTimerId));

    return {
      onStateChange(refreshState) {
        setLoading(refreshState.loading);
        if (!refreshState.loading && refreshState.error) showError(refreshState.error.reason || "");
      },
      markUpdated() {
        hideError();
        lastUpdatedAt = new Date();
        tick();
      },
    };
  }

  /* ---------------- JSON loading with readable failure reasons ---------------- */

  class ReportLoadError extends Error {
    constructor(reason, retryAfterMs) {
      super(reason);
      this.name = "ReportLoadError";
      this.reason = reason;
      this.retryAfterMs = retryAfterMs || 0; // honoured by FmsAutoRefresh
    }
  }

  function retryAfterFrom(response) {
    const seconds = parseInt(response.headers.get("Retry-After"), 10);
    return Number.isFinite(seconds) ? Math.min(seconds, 300) * 1000 : 0;
  }

  function errorReason(status) {
    if (status === 401 || status === 403) return "your session expired or you no longer have access. Reload the page or sign in again.";
    if (status === 429) return "too many requests. It will retry automatically.";
    if (status >= 500) return `the server returned an error (HTTP ${status}).`;
    return `unexpected response (HTTP ${status}).`;
  }

  // GET ``url`` as JSON and hand it to ``render``; every failure becomes a
  // ReportLoadError whose ``reason`` the refresh banner can show as-is.
  async function loadJson(url, { signal, noun, render }) {
    let response;
    try {
      response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal });
    } catch (networkError) {
      if (networkError && networkError.name === "AbortError") throw networkError;
      throw new ReportLoadError("cannot reach the server. Check that it is running and your connection is up.");
    }
    if (!response.ok) {
      throw new ReportLoadError(errorReason(response.status), response.status === 429 ? retryAfterFrom(response) : 0);
    }
    let data;
    try {
      data = await response.json();
    } catch (parseError) {
      throw new ReportLoadError(`the server did not return ${noun}. Reload the page or sign in again.`);
    }
    try {
      render(data);
    } catch (renderError) {
      console.error(`Rendering ${noun} failed`, renderError);
      throw new ReportLoadError("the data arrived but could not be displayed (see browser console).");
    }
  }

  function debounce(fn, ms) {
    let timerId = null;
    return (...args) => {
      window.clearTimeout(timerId);
      timerId = window.setTimeout(() => fn(...args), ms);
    };
  }

  global.FmsReportKit = {
    RANGE_LABELS,
    todayIsoDate,
    customRangeProblem,
    showToast,
    downloadReport,
    createRangeControl,
    wireDownloadMenu,
    createRefreshUi,
    loadJson,
    ReportLoadError,
    debounce,
  };
})(window);
