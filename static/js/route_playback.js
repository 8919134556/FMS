/*
 * FmsRoutePlayback — animates a vehicle marker along a RECORDED route on an
 * existing Leaflet map (the Trip Report popup's map). No fake data: the input
 * is the ordered GPS fixes [{t, lat, lon}] from the existing route endpoint
 * (tracking:trip-report-route).
 *
 *  - The marker moves segment by segment through consecutive recorded fixes,
 *    interpolating only BETWEEN neighbours, so it never leaves the drawn line.
 *  - Timing follows the records' timestamps (the vehicle visibly slows/speeds
 *    up as it really did); standing still is compressed 100x and any single
 *    gap is capped (MAX_SEGMENT_MS), so a long stop doesn't stall playback.
 *    At 1x the whole trip plays in PLAY_MS; the clock always shows the REAL
 *    time at the marker's position.
 *  - Controls are existing DOM elements passed in; every listener is bound to
 *    an AbortController, and destroy() stops the frame loop and unbinds them,
 *    so an old animation can never keep running after switching trips.
 */
(function (global) {
  "use strict";

  const PLAY_MS = 30000; // whole trip at 1x
  const MAX_SEGMENT_MS = 60000; // a single gap longer than 1 min plays as 1 min
  const MIN_MOVING_SEGMENT_MS = 250; // same-timestamp fixes that moved still glide, never jump
  // Standing still plays 100x faster than driving, so a 1-hour stop doesn't
  // fill the playback while the drive keeps its recorded timing. A hop counts
  // as standing still when it implies < STATIONARY_KMH: parked GPS drift
  // (typically 10-20 m every 30-60 s, ~1-2 km/h) is not driving.
  const STATIONARY_KMH = 3;
  const STATIONARY_FACTOR = 0.01;

  function toRad(d) {
    return (d * Math.PI) / 180;
  }

  function metres(a, b) {
    const dLat = toRad(b[0] - a[0]);
    const dLon = toRad(b[1] - a[1]);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function bearing(a, b) {
    const y = Math.sin(toRad(b[1] - a[1])) * Math.cos(toRad(b[0]));
    const x = Math.cos(toRad(a[0])) * Math.sin(toRad(b[0]))
      - Math.sin(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.cos(toRad(b[1] - a[1]));
    return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  }

  function create({ map, points, controls, color, formatTime, autoplay = true }) {
    const latlngs = points.map((p) => [parseFloat(p.lat), parseFloat(p.lon)]);
    const times = points.map((p) => Date.parse(p.t));
    const n = latlngs.length;
    const abort = new AbortController();
    const on = (el, type, fn) => el && el.addEventListener(type, fn, { signal: abort.signal });

    // Cumulative timeline weight at each fix (index 0 = 0).
    const cumulative = [0];
    for (let i = 1; i < n; i += 1) {
      const hop = metres(latlngs[i - 1], latlngs[i]);
      const realMs = Number.isFinite(times[i] - times[i - 1]) ? Math.max(0, times[i] - times[i - 1]) : 0;
      const kmh = realMs > 0 ? (hop / 1000) / (realMs / 3600000) : hop > 0 ? Infinity : 0;
      const moved = hop > 0 && kmh >= STATIONARY_KMH;
      let w = Math.min(realMs, MAX_SEGMENT_MS);
      w = moved ? Math.max(w, MIN_MOVING_SEGMENT_MS) : w * STATIONARY_FACTOR;
      cumulative.push(cumulative[i - 1] + w);
    }
    const total = cumulative[n - 1] || 0;

    const trail = L.polyline([latlngs[0]], { color, weight: 5, opacity: 1, interactive: false }).addTo(map);
    const icon = L.divIcon({
      html: '<div class="trip-playback-marker"><i class="bi bi-caret-up-fill" aria-hidden="true"></i></div>',
      className: "trip-playback-icon",
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    });
    const marker = L.marker(latlngs[0], { icon, zIndexOffset: 1000, keyboard: false, interactive: false }).addTo(map);

    const ui = controls || {};
    const state = { progress: 0, playing: false, speed: parseFloat(ui.speed?.value) || 1, frame: null, last: null, segment: 0 };

    // Position at timeline progress p (0..1): segment by binary search, then
    // linear interpolation between those two recorded fixes only.
    function positionAt(p) {
      if (total === 0) return { latlng: latlngs[n - 1], segment: n - 2, time: times[n - 1], heading: null };
      const target = p * total;
      let lo = 0;
      let hi = n - 1;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (cumulative[mid] <= target) lo = mid;
        else hi = mid;
      }
      const span = cumulative[hi] - cumulative[lo];
      const f = span > 0 ? (target - cumulative[lo]) / span : 1;
      const a = latlngs[lo];
      const b = latlngs[hi];
      const moved = a[0] !== b[0] || a[1] !== b[1];
      return {
        latlng: [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f],
        segment: lo,
        time: Number.isFinite(times[lo]) && Number.isFinite(times[hi]) ? times[lo] + (times[hi] - times[lo]) * f : null,
        heading: moved ? bearing(a, b) : null,
      };
    }

    function render() {
      const pos = positionAt(state.progress);
      marker.setLatLng(pos.latlng);
      if (pos.heading !== null) {
        const el = marker.getElement()?.querySelector(".trip-playback-marker");
        if (el) el.style.transform = `rotate(${pos.heading}deg)`;
      }
      // Travelled part of the route: every fix passed so far + the marker.
      state.segment = pos.segment;
      trail.setLatLngs(latlngs.slice(0, state.segment + 1).concat([pos.latlng]));
      if (ui.scrub) ui.scrub.value = String(Math.round(state.progress * 1000));
      if (ui.clock) ui.clock.textContent = pos.time !== null && formatTime ? formatTime(new Date(pos.time)) : "";
      setPlayingUi();
    }

    function setPlayingUi() {
      if (!ui.play) return;
      const atEnd = state.progress >= 1;
      const icon = state.playing ? "bi-pause-fill" : atEnd ? "bi-arrow-repeat" : "bi-play-fill";
      const label = state.playing ? "Pause" : atEnd ? "Replay" : "Play";
      ui.play.innerHTML = `<i class="bi ${icon}" aria-hidden="true"></i>`;
      ui.play.setAttribute("aria-label", label);
      ui.play.title = label;
    }

    function tick(now) {
      if (!state.playing) return;
      if (state.last !== null) {
        state.progress = Math.min(1, state.progress + ((now - state.last) * state.speed) / PLAY_MS);
      }
      state.last = now;
      render();
      if (state.progress >= 1) {
        pause(); // stops exactly at the trip end
        return;
      }
      state.frame = requestAnimationFrame(tick);
    }

    function play() {
      if (n < 2 || state.playing) return;
      if (state.progress >= 1) state.progress = 0;
      state.playing = true;
      state.last = null;
      state.frame = requestAnimationFrame(tick);
      setPlayingUi();
    }

    function pause() {
      state.playing = false;
      if (state.frame) cancelAnimationFrame(state.frame);
      state.frame = null;
      setPlayingUi();
    }

    function restart() {
      pause();
      state.progress = 0;
      render();
      play();
    }

    on(ui.play, "click", () => (state.playing ? pause() : play()));
    on(ui.restart, "click", restart);
    on(ui.speed, "change", () => {
      state.speed = parseFloat(ui.speed.value) || 1;
    });
    on(ui.scrub, "input", () => {
      state.progress = Math.min(1, Math.max(0, parseInt(ui.scrub.value, 10) / 1000));
      render();
    });

    const disabled = n < 2;
    [ui.play, ui.restart, ui.scrub, ui.speed].forEach((el) => {
      if (el) el.disabled = disabled;
    });
    render();
    const reduceMotion = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (autoplay && !disabled && !reduceMotion) play();

    return {
      play,
      pause,
      restart,
      get playing() {
        return state.playing;
      },
      get progress() {
        return state.progress;
      },
      destroy() {
        pause();
        abort.abort(); // unbind every control listener of THIS player
        trail.remove();
        marker.remove();
        // The controls are shared page markup: leave them as a fresh trip expects.
        if (ui.scrub) ui.scrub.value = "0";
        if (ui.speed) ui.speed.value = "1";
        if (ui.clock) ui.clock.textContent = "";
      },
    };
  }

  global.FmsRoutePlayback = { create, PLAY_MS };
})(window);
