/*
 * FmsAlertCenter — the emergency side of the notification bell.
 *
 * The bell (app.js) polls the notification feed; the feed's ``alerts`` list
 * holds the user's UNREAD notifications for vehicle alert events (a new panic
 * episode creates exactly one per user — apps.alerts.events). For each one not
 * announced before, this shows a popup and plays a short alarm ONCE:
 *
 *   - "once" is remembered in localStorage per notification, so a page reload,
 *     another page or a second tab never repeats it;
 *   - only fresh notifications (≤ ANNOUNCE_WINDOW_MS old) pop up — an old unread
 *     one stays in the bell without a siren;
 *   - browsers block audio until the user has interacted with the page, so the
 *     audio context is unlocked on the first click/keypress; an alert that
 *     arrives while sound is still blocked shows "Click to enable sound" and
 *     plays as soon as the user interacts (if still recent).
 *
 * Nothing here decides what is an alert: it only presents what the server sent.
 * Fires ``fms:alert`` on window so open pages (Alert Report) can refresh at once.
 */
(function (global) {
  "use strict";

  const STORAGE_KEY = "fms.alerts.announced";
  const MAX_REMEMBERED = 300;
  const ANNOUNCE_WINDOW_MS = 30 * 60 * 1000;
  const PENDING_SOUND_MS = 2 * 60 * 1000;

  let audioCtx = null;
  let pendingSoundAt = 0;
  let pendingUrgent = false;
  let container = null;
  let options = { timezone: undefined, onOpen: null };

  /* ---------------- Announced registry (per browser, across tabs) ---------------- */

  function readAnnounced() {
    try {
      const list = JSON.parse(global.localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(list) ? list : [];
    } catch (unavailable) {
      return [];
    }
  }

  const memoryAnnounced = new Set(); // fallback when storage is blocked

  function wasAnnounced(id) {
    return memoryAnnounced.has(id) || readAnnounced().includes(id);
  }

  function markAnnounced(id) {
    memoryAnnounced.add(id);
    try {
      const list = readAnnounced().filter((x) => x !== id);
      list.push(id);
      global.localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(-MAX_REMEMBERED)));
    } catch (unavailable) {
      /* private mode — the in-memory set still prevents repeats on this page */
    }
  }

  /* ---------------- Sound (Web Audio, no asset to load) ---------------- */

  function context() {
    if (audioCtx) return audioCtx;
    const Ctor = global.AudioContext || global.webkitAudioContext;
    if (!Ctor) return null;
    try {
      audioCtx = new Ctor();
    } catch (unsupported) {
      audioCtx = null;
    }
    return audioCtx;
  }

  function soundReady() {
    const ctx = context();
    return !!ctx && ctx.state === "running";
  }

  // urgent (critical, e.g. Panic): two-tone emergency alarm, ~1.6 s, 960 / 720 Hz square.
  // attention (medium/low, e.g. Idle): three soft rising chimes, ~0.9 s.
  const SOUNDS = {
    urgent: { wave: "square", volume: 0.22, notes: [960, 720, 960, 720, 960, 720, 960, 720], step: 0.2, length: 0.19 },
    attention: { wave: "sine", volume: 0.3, notes: [660, 880, 1046], step: 0.28, length: 0.26 },
  };

  function playAlarm(kind = "urgent") {
    const ctx = context();
    if (!ctx || ctx.state !== "running") return false;
    const sound = SOUNDS[kind] || SOUNDS.urgent;
    const start = ctx.currentTime + 0.02;
    const master = ctx.createGain();
    master.gain.value = sound.volume;
    master.connect(ctx.destination);
    sound.notes.forEach((frequency, i) => {
      const t = start + i * sound.step;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = sound.wave;
      osc.frequency.setValueAtTime(frequency, t);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(1, t + 0.015);
      gain.gain.setValueAtTime(1, t + sound.length - 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + sound.length);
      osc.connect(gain).connect(master);
      osc.start(t);
      osc.stop(t + sound.step);
    });
    return true;
  }

  function isUrgent(alert) {
    return alert.severity === "CRITICAL" || alert.severity === "HIGH";
  }

  function unlock() {
    const ctx = context();
    if (!ctx) return;
    const after = () => {
      if (ctx.state !== "running") return;
      document.querySelectorAll(".fms-alert-popup-sound").forEach((el) => el.classList.add("d-none"));
      if (pendingSoundAt && Date.now() - pendingSoundAt < PENDING_SOUND_MS) playAlarm(pendingUrgent ? "urgent" : "attention");
      pendingSoundAt = 0;
      pendingUrgent = false;
    };
    if (ctx.state === "suspended") ctx.resume().then(after).catch(() => {});
    else after();
  }

  ["pointerdown", "keydown", "touchstart"].forEach((type) => {
    global.addEventListener(type, unlock, { capture: true, passive: true });
  });

  /* ---------------- Popup ---------------- */

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function formatTime(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString("en-GB", {
        timeZone: options.timezone || undefined, day: "2-digit", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    } catch (badZone) {
      return new Date(iso).toLocaleString("en-GB");
    }
  }

  function ensureContainer() {
    if (container && document.body.contains(container)) return container;
    container = document.createElement("div");
    container.className = "fms-alert-stack";
    container.setAttribute("aria-live", "assertive");
    document.body.appendChild(container);
    return container;
  }

  function row(label, value) {
    return value ? `<div class="fms-alert-popup-row"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>` : "";
  }

  function showPopup(alert, { soundBlocked }) {
    const location = alert.location
      || (alert.latitude !== null && alert.latitude !== undefined ? `${alert.latitude.toFixed(5)}, ${alert.longitude.toFixed(5)}` : "");
    const urgent = isUrgent(alert);
    const card = document.createElement("div");
    card.className = `fms-alert-popup${urgent ? "" : " is-medium"}`;
    card.setAttribute("role", "alertdialog");
    card.setAttribute("aria-label", `${alert.type_label} alert, vehicle ${alert.vehicle}`);
    card.innerHTML = `
      <div class="fms-alert-popup-head">
        <span class="fms-alert-popup-icon" aria-hidden="true"><i class="bi ${urgent ? "bi-exclamation-octagon-fill" : "bi-exclamation-triangle-fill"}"></i></span>
        <span class="fms-alert-popup-title">${escapeHtml(alert.type_label)} Alert</span>
        <button type="button" class="btn-close btn-close-white" aria-label="Dismiss"></button>
      </div>
      <div class="fms-alert-popup-body">
        ${alert.message ? `<p class="fms-alert-popup-message">${escapeHtml(alert.message)}</p>` : ""}
        ${row("Vehicle", alert.vehicle)}
        ${row("Level", alert.level_label)}
        ${row("Driver", alert.driver)}
        ${row("Time", formatTime(alert.occurred_at))}
        ${row("Location", location)}
      </div>
      <div class="fms-alert-popup-sound${soundBlocked ? "" : " d-none"}">
        <i class="bi bi-volume-mute me-1" aria-hidden="true"></i>Sound is blocked by the browser — click anywhere to enable it.
      </div>
      <div class="fms-alert-popup-actions">
        <a class="btn btn-sm btn-light fw-semibold" href="${escapeHtml(alert.link_url || "#")}">View alert</a>
        <button type="button" class="btn btn-sm btn-outline-light" data-dismiss>Dismiss</button>
      </div>`;
    const close = () => card.remove();
    card.querySelector(".btn-close").addEventListener("click", close);
    card.querySelector("[data-dismiss]").addEventListener("click", close);
    card.querySelector("a").addEventListener("click", () => {
      if (options.onOpen) options.onOpen(alert);
    });
    ensureContainer().prepend(card);
  }

  /* ---------------- Entry point (called by the bell after each poll) ---------------- */

  function announce(alerts) {
    if (!Array.isArray(alerts) || !alerts.length) return;
    const now = Date.now();
    const fresh = alerts
      .filter((a) => !wasAnnounced(a.notification_uuid))
      .filter((a) => now - new Date(a.created_at).getTime() <= ANNOUNCE_WINDOW_MS)
      .reverse(); // oldest first, so the newest ends up on top
    if (!fresh.length) return;
    fresh.forEach((a) => markAnnounced(a.notification_uuid));
    const blocked = !soundReady();
    fresh.forEach((a) => showPopup(a, { soundBlocked: blocked }));
    // One sound per poll, however many new events arrived in it — the most urgent one's.
    const urgent = fresh.some(isUrgent);
    if (!playAlarm(urgent ? "urgent" : "attention")) {
      pendingSoundAt = now;
      pendingUrgent = pendingUrgent || urgent;
    }
    global.dispatchEvent(new CustomEvent("fms:alert", { detail: { alerts: fresh } }));
  }

  global.FmsAlertCenter = {
    configure(next) {
      options = { ...options, ...next };
    },
    announce,
    playAlarm,
  };
})(window);
