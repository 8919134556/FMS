/*
 * FmsAlertCenter — the emergency side of the notification bell.
 *
 * The bell (app.js) polls the notification feed; the feed's ``alerts`` list
 * holds the user's UNREAD notifications for vehicle alert events (a new panic
 * episode creates exactly one per user — apps.alerts.events). For each one not
 * announced before, this shows a compact toast (top-right) and plays a short alarm ONCE:
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
 * Toasts: newest on top, at most ``maxVisible`` at once (the oldest leaves when
 * another arrives — every alert stays in the bell and the Alert Report), and
 * each fades out after ``toastDurationMs`` (paused while hovered / focused).
 * Closing one only hides it: the alert and its notification are untouched.
 * They sit under the topbar, or under a visible ``[data-fms-toast-anchor]``
 * (e.g. the Live Tracking map's top-right controls) so they never cover them.
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
  // Configurable via FmsAlertCenter.configure({...}).
  let options = { timezone: undefined, onOpen: null, toastDurationMs: 7000, maxVisible: 3 };

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
      document.querySelectorAll(".fms-alert-toast-sound").forEach((el) => el.classList.add("d-none"));
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

  function formatClock(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleTimeString("en-GB", { timeZone: options.timezone || undefined, hour: "2-digit", minute: "2-digit" });
    } catch (badZone) {
      return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    }
  }

  /* Stack position: under the topbar, or under the lowest visible [data-fms-toast-anchor] (page
   * controls in the top-right that must stay clickable) — and, while a dialog is open, under its
   * header so its close button stays clear. */
  function placeStack() {
    if (!container) return;
    const css = global.getComputedStyle(document.documentElement);
    const base = (parseFloat(css.getPropertyValue("--fms-topbar-height")) || 64) + 12;
    const modal = document.querySelector(".modal.show .modal-header");
    // Phones: page anchors sit mid-screen, so stay under the topbar there.
    const pageAnchors = global.innerWidth >= 768 ? Array.from(document.querySelectorAll("[data-fms-toast-anchor]")) : [];
    const anchors = modal ? [modal] : pageAnchors;
    let top = base;
    anchors.forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.height && r.bottom > 0 && r.top < global.innerHeight * 0.5) top = Math.max(top, r.bottom + 8);
    });
    container.style.top = `${Math.round(top)}px`;
  }
  // Dialogs opening / closing move the stack too.
  document.addEventListener("shown.bs.modal", () => queuePlace());
  document.addEventListener("hidden.bs.modal", () => queuePlace());

  let placeQueued = false;
  function queuePlace() {
    if (placeQueued) return;
    placeQueued = true;
    global.requestAnimationFrame(() => { placeQueued = false; placeStack(); });
  }
  global.addEventListener("scroll", queuePlace, { passive: true, capture: true });
  global.addEventListener("resize", queuePlace, { passive: true });

  function ensureContainer() {
    if (container && document.body.contains(container)) return container;
    container = document.createElement("div");
    container.className = "fms-alert-stack";
    container.setAttribute("aria-live", "assertive");
    container.setAttribute("aria-label", "Vehicle alerts");
    document.body.appendChild(container);
    return container;
  }

  // Level -> accent, from the alert's own severity (the Alert Report's Level).
  function toneFor(alert) {
    if (alert.severity === "CRITICAL" || alert.severity === "HIGH") return { cls: "is-high", icon: "bi-exclamation-octagon-fill" };
    if (alert.severity === "MEDIUM") return { cls: "is-medium", icon: "bi-exclamation-triangle-fill" };
    return { cls: "is-low", icon: "bi-info-circle-fill" };
  }

  function dismiss(card) {
    if (!card || card.dataset.leaving) return;
    card.dataset.leaving = "1";
    global.clearTimeout(card.fmsTimer);
    card.classList.add("is-leaving");
    const gone = () => card.remove();
    card.addEventListener("animationend", gone, { once: true });
    global.setTimeout(gone, 400); // reduced motion / no animation
  }

  // Auto-dismiss that pauses while the pointer or keyboard focus is on the toast.
  function armTimer(card) {
    let remaining = options.toastDurationMs;
    let startedAt = 0;
    const start = () => {
      if (!remaining || remaining < 0 || card.dataset.leaving) return;
      startedAt = Date.now();
      card.fmsTimer = global.setTimeout(() => dismiss(card), remaining);
    };
    const pause = () => {
      global.clearTimeout(card.fmsTimer);
      remaining = Math.max(1200, remaining - (Date.now() - startedAt));
    };
    card.addEventListener("mouseenter", pause);
    card.addEventListener("mouseleave", start);
    card.addEventListener("focusin", pause);
    card.addEventListener("focusout", start);
    start();
  }

  function showToast(alert, { soundBlocked }) {
    const tone = toneFor(alert);
    const detail = alert.detail || alert.message || "";
    const time = formatClock(alert.occurred_at);
    const card = document.createElement("div");
    card.className = `fms-alert-toast ${tone.cls}`;
    card.setAttribute("role", "alert");
    card.innerHTML = `
      <span class="fms-alert-toast-icon" aria-hidden="true"><i class="bi ${tone.icon}"></i></span>
      <div class="fms-alert-toast-body">
        <div class="fms-alert-toast-head">
          <a class="fms-alert-toast-title" href="${escapeHtml(alert.link_url || "#")}" title="Open in Alert Report">${escapeHtml(alert.type_label)}</a>
          <span class="fms-alert-toast-level">${escapeHtml(alert.level_label || "")}</span>
        </div>
        <div class="fms-alert-toast-vehicle">${escapeHtml(alert.vehicle || "—")}${time ? `<span class="fms-alert-toast-time">${escapeHtml(time)}</span>` : ""}</div>
        ${detail ? `<div class="fms-alert-toast-detail">${escapeHtml(detail)}</div>` : ""}
        <div class="fms-alert-toast-sound${soundBlocked ? "" : " d-none"}"><i class="bi bi-volume-mute" aria-hidden="true"></i>Click anywhere to enable sound</div>
      </div>
      <button type="button" class="fms-alert-toast-close" aria-label="Dismiss notification"><i class="bi bi-x-lg" aria-hidden="true"></i></button>`;
    card.querySelector(".fms-alert-toast-close").addEventListener("click", () => dismiss(card));
    card.querySelector(".fms-alert-toast-title").addEventListener("click", () => {
      if (options.onOpen) options.onOpen(alert);
    });
    const stack = ensureContainer();
    // The "enable sound" hint once, on the newest toast only.
    if (soundBlocked) stack.querySelectorAll(".fms-alert-toast-sound").forEach((el) => el.classList.add("d-none"));
    stack.prepend(card); // newest on top
    placeStack();
    // Never more than maxVisible: the oldest goes (it is still in the bell / Alert Report).
    const live = Array.from(stack.querySelectorAll(".fms-alert-toast:not(.is-leaving)"));
    live.slice(Math.max(1, options.maxVisible)).forEach(dismiss);
    armTimer(card);
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
    fresh.forEach((a) => showToast(a, { soundBlocked: blocked }));
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
