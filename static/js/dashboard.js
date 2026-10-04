(() => {
  "use strict";

  const dateRangeSelect = document.getElementById("dashboardDateRange");
  const autoRefreshSelect = document.getElementById("dashboardAutoRefresh");
  const refreshBtn = document.getElementById("dashboardRefreshBtn");
  const AUTO_REFRESH_KEY = "fms.dashboard.autoRefreshMs";

  function currentUrlWithParam(key, value) {
    const url = new URL(window.location.href);
    url.searchParams.set(key, value);
    return url.toString();
  }

  /* ---------------- Date range selector ---------------- */
  dateRangeSelect?.addEventListener("change", () => {
    window.location.href = currentUrlWithParam("date_range", dateRangeSelect.value);
  });

  /* ---------------- Manual refresh (full reload — no polling infra yet) ---------------- */
  refreshBtn?.addEventListener("click", () => {
    refreshBtn.classList.add("is-refreshing");
    refreshBtn.disabled = true;
    window.location.reload();
  });

  /* ---------------- Optional auto-refresh (default Off, persisted) ---------------- */
  if (autoRefreshSelect) {
    const stored = localStorage.getItem(AUTO_REFRESH_KEY) || "0";
    autoRefreshSelect.value = stored;
    const ms = parseInt(stored, 10);
    if (ms > 0) {
      window.setTimeout(() => window.location.reload(), ms);
    }
    autoRefreshSelect.addEventListener("change", () => {
      localStorage.setItem(AUTO_REFRESH_KEY, autoRefreshSelect.value);
      const nextMs = parseInt(autoRefreshSelect.value, 10);
      if (nextMs > 0) {
        window.setTimeout(() => window.location.reload(), nextMs);
      }
    });
  }
})();
