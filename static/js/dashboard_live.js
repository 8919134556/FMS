/*
 * Dashboard — Fleet Overview (templates/dashboard/partials/_live_fleet.html).
 *
 * ONE refresh loop re-reads the SAME feed the Live Tracking page polls
 * (/api/v1/tracking/fleet/current/) and updates the KPI cards and the status
 * donut in place. Vehicles are classified with the map markers' own rule
 * (FmsVehicleIcons.statusFor), so the dashboard and Live Tracking can never
 * disagree.
 *
 * Every 30 s, one request at a time, paused while the tab is hidden (caught up
 * on return), stopped on pagehide. A failed refresh keeps the last good numbers
 * and says so in the "updated …" label.
 */
(() => {
  "use strict";

  const root = document.getElementById("dashboardLiveFleet");
  if (!root || !root.dataset.fleetApiUrl || !window.fetch) return;

  const FEED_URL = root.dataset.fleetApiUrl;
  const INTERVAL_MS = 30000;
  const KEYS = ["moving", "idle", "stopped", "offline"];
  const updated = document.getElementById("dashboardLiveUpdated");
  const liveState = document.getElementById("dashboardLiveState");
  const stale = document.getElementById("dashboardFeedStale");
  const state = { busy: false, lastAt: 0, failed: false, timer: null, tick: null };

  function statusKey(vehicle) {
    if (window.FmsVehicleIcons) return window.FmsVehicleIcons.statusFor(vehicle).key;
    if (vehicle.connection_status === "OFFLINE") return "offline";
    if (vehicle.movement_state === "MOVING") return "moving";
    return vehicle.ignition === false ? "stopped" : "idle";
  }

  function setText(selector, value) {
    document.querySelectorAll(selector).forEach((node) => {
      if (node.textContent === String(value)) return;
      node.textContent = value;
      node.classList.remove("ov-bump");
      void node.offsetWidth; // restart the "changed" highlight
      node.classList.add("ov-bump");
    });
  }

  function apply(vehicles) {
    const counts = { moving: 0, idle: 0, stopped: 0, offline: 0 };
    vehicles.forEach((v) => { counts[statusKey(v)] += 1; });
    const tracked = vehicles.length;
    setText('[data-live-count="tracked"]', tracked);
    setText('[data-live-count="immobilized"]', vehicles.filter((v) => v.immobilizer === true).length);
    KEYS.forEach((key) => {
      setText(`[data-live-count="${key}"]`, counts[key]);
      setText(`[data-live-pct="${key}"]`, tracked ? Math.round((counts[key] * 100) / tracked) : 0);
    });
    if (window.FmsDashboardCharts) window.FmsDashboardCharts.setLiveStatus(KEYS.map((k) => counts[k]));
  }

  function tickLabel() {
    if (!updated) return;
    if (!state.lastAt) { updated.textContent = state.failed ? "couldn't refresh live data" : "updates every 30 s"; return; }
    const secs = Math.round((Date.now() - state.lastAt) / 1000);
    const ago = secs < 5 ? "just now" : secs < 60 ? `${secs}s ago` : `${Math.floor(secs / 60)} min ago`;
    updated.textContent = state.failed ? `refresh failed · last update ${ago}` : `updated ${ago}`;
    liveState?.classList.toggle("is-error", state.failed);
  }

  function refresh() {
    if (state.busy) return;
    state.busy = true;
    fetch(FEED_URL, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then((body) => {
        apply(body.results || []);
        state.failed = false;
        state.lastAt = Date.now();
        const sync = body.sync;
        stale?.classList.toggle("d-none", !(sync && sync.enabled && !sync.healthy));
      })
      .catch(() => { state.failed = true; }) // keep the last good numbers; the next tick tries again
      .finally(() => {
        state.busy = false;
        tickLabel();
      });
  }

  state.timer = window.setInterval(() => { if (!document.hidden) refresh(); }, INTERVAL_MS);
  state.tick = window.setInterval(tickLabel, 5000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - state.lastAt > INTERVAL_MS) refresh();
  });
  window.addEventListener("pagehide", () => {
    window.clearInterval(state.timer);
    window.clearInterval(state.tick);
  });
  refresh(); // confirm the server-rendered numbers right away (and the feed-health banner)
})();
