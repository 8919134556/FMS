/*
 * FmsAutoRefresh — reusable, polling-based auto-refresh controller.
 *
 * Any "live" page hands it a `task` (an async function that fetches + renders
 * fresh data) and, optionally, a <select> of intervals. It guarantees:
 *
 *   - exactly ONE timer at a time (a chained setTimeout, never setInterval),
 *     so a slow response can never stack up overlapping requests;
 *   - at most ONE request in flight — a manual refresh while one is running
 *     simply joins it instead of firing a duplicate;
 *   - the cadence is measured from when a run STARTS (5 s means every 5 s,
 *     not "5 s after the previous one finished");
 *   - polling pauses while the tab is hidden and resumes (with an immediate
 *     catch-up refresh if the data went stale) when it is visible again;
 *   - failures never break the page: the interval keeps running, backing off
 *     (up to 30 s) while the server is failing, and honouring `retryAfterMs`
 *     if the task's error carries one (HTTP 429 Retry-After);
 *   - the chosen interval persists (localStorage) and is validated against
 *     the <select>'s own options, so stale/invalid stored values are ignored;
 *   - everything is torn down on `pagehide`.
 *
 * Usage:
 *   const refresher = FmsAutoRefresh.create({
 *     task: ({ signal }) => loadAndRender(signal),   // throw to signal failure
 *     select: document.getElementById("refreshInterval"),
 *     storageKey: "fms.somePage.refreshMs",
 *     defaultMs: 30000,
 *     onStateChange: (s) => { ... },  // { loading, error, paused, intervalMs, lastSuccessAt, failures }
 *   });
 *   refresher.start();           // runs once immediately, then on the interval
 *   refresher.refreshNow();      // manual refresh (resets the countdown)
 *   refresher.setIntervalMs(ms); // programmatic change (the <select> does this itself)
 *   refresher.destroy();
 */
(function (global) {
  "use strict";

  const MIN_INTERVAL_MS = 1000;
  const MAX_BACKOFF_MS = 30000;

  function readStored(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (unavailable) {
      return null;
    }
  }

  function writeStored(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (unavailable) {
      /* private mode / storage disabled — the choice just won't persist */
    }
  }

  function create(options) {
    const task = options.task;
    const select = options.select || null;
    const storageKey = options.storageKey || null;
    const pauseWhenHidden = options.pauseWhenHidden !== false;
    const onStateChange = options.onStateChange || function () {};

    const allowed = select
      ? Array.from(select.options).map((o) => parseInt(o.value, 10)).filter((n) => n >= MIN_INTERVAL_MS)
      : null;

    function isAllowed(ms) {
      return Number.isFinite(ms) && ms >= MIN_INTERVAL_MS && (!allowed || allowed.includes(ms));
    }

    let intervalMs = isAllowed(options.defaultMs) ? options.defaultMs : allowed ? allowed[0] : 30000;
    if (storageKey) {
      const stored = parseInt(readStored(storageKey), 10);
      if (isAllowed(stored)) intervalMs = stored;
    }

    let timerId = null;
    let inFlight = null;
    let abortController = null;
    let rerunRequested = false; // see refreshNow()
    let lastStartedAt = 0;
    let lastSuccessAt = null;
    let failures = 0;
    let retryAfterMs = 0;
    let started = false;
    let destroyed = false;
    let lastError = null;

    function snapshot() {
      return {
        loading: inFlight !== null,
        error: lastError,
        paused: pauseWhenHidden && document.hidden,
        intervalMs: intervalMs,
        lastSuccessAt: lastSuccessAt,
        failures: failures,
      };
    }

    function emit() {
      try {
        onStateChange(snapshot());
      } catch (uiError) {
        console.error("FmsAutoRefresh onStateChange failed", uiError);
      }
    }

    function clearTimer() {
      if (timerId !== null) {
        window.clearTimeout(timerId);
        timerId = null;
      }
    }

    function nextDelay() {
      if (failures > 0) {
        const backoff = Math.min(MAX_BACKOFF_MS, intervalMs * Math.pow(2, Math.min(failures, 6)));
        return Math.max(retryAfterMs, backoff, intervalMs);
      }
      const elapsed = Date.now() - lastStartedAt;
      return Math.max(500, intervalMs - elapsed);
    }

    function schedule(delay) {
      clearTimer(); // the single-timer guarantee lives here
      if (!started || destroyed) return;
      if (pauseWhenHidden && document.hidden) return; // resumed by visibilitychange
      timerId = window.setTimeout(run, delay === undefined ? nextDelay() : delay);
    }

    function run() {
      timerId = null;
      if (destroyed) return Promise.resolve();
      if (inFlight) return inFlight; // never a second concurrent request

      lastStartedAt = Date.now();
      abortController = typeof AbortController === "function" ? new AbortController() : null;
      lastError = null;
      inFlight = Promise.resolve()
        .then(() => task({ signal: abortController ? abortController.signal : undefined }))
        .then(() => {
          failures = 0;
          retryAfterMs = 0;
          lastSuccessAt = new Date();
        })
        .catch((error) => {
          if (error && error.name === "AbortError") return; // torn down on purpose
          failures += 1;
          retryAfterMs = (error && error.retryAfterMs) || 0;
          lastError = error || new Error("Refresh failed");
        })
        .then(() => {
          inFlight = null;
          abortController = null;
          if (rerunRequested && !destroyed) {
            // refreshNow() arrived mid-request (e.g. the user changed a filter):
            // that request was for the old filters, so load again right away.
            rerunRequested = false;
            return run();
          }
          emit();
          schedule();
          return undefined;
        });
      emit();
      return inFlight;
    }

    // A manual refresh always reflects the page's CURRENT filters. If a request
    // is already running it was built from the previous ones: cancel it and run
    // again as soon as it settles (still never two requests at once).
    function refreshNow() {
      if (destroyed) return Promise.resolve();
      clearTimer();
      if (inFlight) {
        rerunRequested = true;
        if (abortController) abortController.abort();
        return inFlight;
      }
      return run();
    }

    function setIntervalMs(ms) {
      if (!isAllowed(ms)) return;
      intervalMs = ms;
      if (select && String(select.value) !== String(ms)) select.value = String(ms);
      if (storageKey) writeStored(storageKey, String(ms));
      failures = 0; // a deliberate change is a fresh start
      retryAfterMs = 0;
      emit();
      // Re-arm from the last run start, so picking "5 s" after 3 s of waiting
      // refreshes in 2 s, not 5 s — but never sooner than 500 ms.
      if (!inFlight) schedule();
    }

    function onVisibilityChange() {
      if (destroyed || !started) return;
      if (document.hidden) {
        clearTimer();
        emit();
        return;
      }
      emit();
      if (Date.now() - lastStartedAt >= intervalMs) {
        refreshNow(); // data went stale while the tab was in the background
      } else {
        schedule();
      }
    }

    function onOnline() {
      if (started && !destroyed && failures > 0) refreshNow();
    }

    function onSelectChange() {
      setIntervalMs(parseInt(select.value, 10));
    }

    function destroy() {
      destroyed = true;
      clearTimer();
      if (abortController) abortController.abort();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("pagehide", destroy);
      if (select) select.removeEventListener("change", onSelectChange);
    }

    function start() {
      if (started || destroyed) return;
      started = true;
      if (select) {
        select.value = String(intervalMs);
        select.addEventListener("change", onSelectChange);
      }
      document.addEventListener("visibilitychange", onVisibilityChange);
      window.addEventListener("online", onOnline);
      window.addEventListener("pagehide", destroy);
      if (pauseWhenHidden && document.hidden) {
        emit(); // opened in a background tab: load when it becomes visible
        return;
      }
      run();
    }

    return {
      start: start,
      refreshNow: refreshNow,
      setIntervalMs: setIntervalMs,
      destroy: destroy,
      get intervalMs() {
        return intervalMs;
      },
      get state() {
        return snapshot();
      },
    };
  }

  global.FmsAutoRefresh = { create: create };
})(window);
