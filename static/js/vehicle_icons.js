/*
 * FmsVehicleIcons — the ONE vehicle marker system for Live Tracking.
 *
 * Everything that draws a vehicle (main map markers, the Live Map popup's
 * marker and header, the vehicle panel, the Vehicle Status table) comes from
 * here, so every vehicle looks the same everywhere.
 *
 * Design (after the provided reference): ONE map pin + ONE vehicle graphic.
 *   - the graphic (white face, full-colour front-view vector illustration)
 *     says WHAT the vehicle is — it never changes colour;
 *   - the pin's colour says HOW it is: immobilizer ON black > active alert red >
 *     moving green > idle amber > ignition off grey (offline: grey, faded). No
 *     status dot. An alert on an immobilized vehicle keeps its red halo + tooltip.
 *
 *   VEHICLE_TYPES  type -> { label, illustration (40x40 SVG) }.
 *                  Add a type = one entry + its spellings in SYNONYMS.
 *   STATUSES       the states the data supports, on the app's status tokens —
 *                  the pin colour, tooltip and popup all come from statusFor().
 *
 * Marker anatomy (built once per vehicle, then patched in place):
 *
 *     .vm                 root: status (pin colour) / alert / selected / stale classes
 *       .vm-ring          selected highlight around the face
 *       .vm-pin           pin + white face (SVG, pin in the status colour)
 *       .vm-art           the vehicle illustration — always upright, never rotated
 *       .vm-dir           direction arrow on the face's rim: the ONLY part that rotates,
 *                         to the vehicle's direction (shown while moving)
 *       .vm-label         registration pill (hover / selected / close zoom)
 *
 * The type text is matched case-insensitively with synonyms and never written
 * back. Unknown / empty -> the "Other" marker. All inline vector SVG (no
 * gradients/ids to collide, no image requests): sharp at any zoom and never a
 * broken image.
 */
(function (global) {
  "use strict";

  const DARK = "#1e293b";
  const TYRE = "#0f172a";
  const GLARE = 'fill="#fff" fill-opacity=".22"';

  const VEHICLE_TYPES = {
    bus: {
      label: "Bus",
      art: `<rect x="5" y="10" width="2.6" height="6.5" rx="1.1" fill="#1e3a8a"/><rect x="32.4" y="10" width="2.6" height="6.5" rx="1.1" fill="#1e3a8a"/>
        <rect x="8" y="5" width="24" height="28.5" rx="4" fill="#2563eb"/>
        <rect x="10.4" y="7" width="19.2" height="2.8" rx="1" fill="#1e3a8a"/>
        <rect x="10" y="11" width="20" height="11.5" rx="2" fill="${DARK}"/>
        <path d="M11.5 21 18 12.5h4L14 21z" ${GLARE}/>
        <circle cx="12.6" cy="27.2" r="1.7" fill="#fde68a"/><circle cx="27.4" cy="27.2" r="1.7" fill="#fde68a"/>
        <rect x="16" y="25.7" width="8" height="3" rx="1" fill="#1d4ed8"/>
        <rect x="9" y="31" width="22" height="2.6" rx="1.2" fill="${DARK}"/>
        <rect x="10" y="33" width="4" height="3.2" rx="1" fill="${TYRE}"/><rect x="26" y="33" width="4" height="3.2" rx="1" fill="${TYRE}"/>`,
    },
    bike: {
      label: "Bike",
      art: `<circle cx="8.4" cy="10" r="2.1" fill="${TYRE}"/><circle cx="31.6" cy="10" r="2.1" fill="${TYRE}"/>
        <rect x="8" y="10.4" width="2" height="4" rx=".8" fill="${TYRE}"/><rect x="30" y="10.4" width="2" height="4" rx=".8" fill="${TYRE}"/>
        <rect x="7" y="13.4" width="26" height="2.2" rx="1.1" fill="${TYRE}"/>
        <rect x="17.4" y="25" width="5.2" height="11.5" rx="2.6" fill="${TYRE}"/>
        <rect x="16" y="21" width="8" height="6.4" rx="2" fill="#334155"/>
        <rect x="12.6" y="21.6" width="14.8" height="4.4" rx="2.2" fill="${DARK}"/>
        <path d="M12 12.4Q20 5.6 28 12.4L27 22.4Q20 26.4 13 22.4z" fill="#2563eb"/>
        <path d="M15 11.4Q20 7.4 25 11.4L24 14.2Q20 12.2 16 14.2z" fill="${DARK}"/>
        <ellipse cx="20" cy="17.6" rx="3.8" ry="2" fill="#e0f2fe"/>
        <path d="M14.4 19.6 16.6 15.2" stroke="#fff" stroke-opacity=".35" stroke-width="1" stroke-linecap="round"/>`,
    },
    van: {
      label: "Van",
      art: `<rect x="5.4" y="11" width="2.6" height="5.5" rx="1" fill="${DARK}"/><rect x="32" y="11" width="2.6" height="5.5" rx="1" fill="${DARK}"/>
        <path d="M8.5 8.5Q8.5 5 12 5h16q3.5 0 3.5 3.5V31h-23z" fill="#f8fafc" stroke="#cbd5e1" stroke-width=".9"/>
        <path d="M11 8.6h18l1.3 8.6H9.7z" fill="${DARK}"/>
        <path d="M12 16.4 16.6 9.4h3.6L15.4 16.4z" ${GLARE}/>
        <rect x="10.2" y="20" width="5.2" height="2.6" rx="1" fill="#e2e8f0" stroke="#94a3b8" stroke-width=".6"/>
        <rect x="24.6" y="20" width="5.2" height="2.6" rx="1" fill="#e2e8f0" stroke="#94a3b8" stroke-width=".6"/>
        <rect x="16.4" y="20" width="7.2" height="4.2" rx="1" fill="#334155"/>
        <rect x="8" y="27.2" width="24" height="3.2" rx="1.6" fill="#475569"/>
        <rect x="10" y="30.6" width="4.2" height="4.2" rx="1" fill="${TYRE}"/><rect x="25.8" y="30.6" width="4.2" height="4.2" rx="1" fill="${TYRE}"/>`,
    },
    car: {
      label: "Car",
      art: `<path d="M11.4 15.4 13 9.6Q13.6 8 15.6 8h8.8q2 0 2.6 1.6l1.6 5.8z" fill="${DARK}"/>
        <path d="M14 14.6 16.4 9.4h3.4L16.4 14.6z" ${GLARE}/>
        <rect x="5.4" y="14.4" width="3" height="2.4" rx="1" fill="${DARK}"/><rect x="31.6" y="14.4" width="3" height="2.4" rx="1" fill="${DARK}"/>
        <path d="M7 18.2Q7 15 10.2 15h19.6q3.2 0 3.2 3.2V27q0 2-2 2H9q-2 0-2-2z" fill="#f8fafc" stroke="#cbd5e1" stroke-width=".9"/>
        <path d="M8.6 18.4h6.2l-1 2.6H8.8z" fill="#e0f2fe" stroke="#94a3b8" stroke-width=".5"/>
        <path d="M31.4 18.4h-6.2l1 2.6h4.8z" fill="#e0f2fe" stroke="#94a3b8" stroke-width=".5"/>
        <rect x="16" y="19.2" width="8" height="3.2" rx="1" fill="#475569"/>
        <rect x="9.2" y="24.2" width="21.6" height="2" rx="1" fill="#cbd5e1"/>
        <rect x="9" y="28.4" width="4.2" height="4.2" rx="1" fill="${TYRE}"/><rect x="26.8" y="28.4" width="4.2" height="4.2" rx="1" fill="${TYRE}"/>`,
    },
    truck: {
      label: "Truck",
      art: `<rect x="4.4" y="10" width="2.6" height="7.4" rx="1" fill="${DARK}"/><rect x="33" y="10" width="2.6" height="7.4" rx="1" fill="${DARK}"/>
        <path d="M8 9q0-4 4-4h16q4 0 4 4v21H8z" fill="#dc2626"/>
        <rect x="13" y="6.2" width="14" height="1.6" rx=".8" fill="#fde68a"/>
        <rect x="10" y="9.4" width="20" height="9" rx="1.6" fill="${DARK}"/>
        <path d="M11.4 17.4 16.6 10.4h3.4L14.8 17.4z" ${GLARE}/>
        <rect x="13" y="20.4" width="14" height="7" rx="1" fill="#7f1d1d"/>
        <rect x="14" y="22" width="12" height=".8" fill="#fff" fill-opacity=".28"/><rect x="14" y="24" width="12" height=".8" fill="#fff" fill-opacity=".28"/>
        <rect x="14" y="26" width="12" height=".6" fill="#fff" fill-opacity=".28"/>
        <rect x="9.4" y="21" width="3" height="3" rx=".8" fill="#fef3c7"/><rect x="27.6" y="21" width="3" height="3" rx=".8" fill="#fef3c7"/>
        <rect x="7.4" y="28.2" width="25.2" height="3" rx="1" fill="${DARK}"/>
        <rect x="9" y="31" width="5" height="4" rx="1" fill="${TYRE}"/><rect x="26" y="31" width="5" height="4" rx="1" fill="${TYRE}"/>`,
    },
    auto: {
      label: "Auto",
      art: `<path d="M9 13.4Q9 6 20 6t11 7.4V15H9z" fill="#facc15"/>
        <path d="M11 12.6Q11.6 8.2 18 7.6" stroke="#fff" stroke-opacity=".5" stroke-width="1" fill="none" stroke-linecap="round"/>
        <path d="M10 15h20v12q0 3-3 3H13q-3 0-3-3z" fill="#111827"/>
        <rect x="12" y="15.6" width="16" height="7.4" rx="1.6" fill="#334155"/>
        <path d="M13.4 22.2 17.4 16.4h3.2L16.6 22.2z" ${GLARE}/>
        <circle cx="20" cy="26.4" r="2" fill="#fde68a"/>
        <rect x="8.6" y="27.6" width="3" height="5.6" rx="1.2" fill="${TYRE}"/><rect x="28.4" y="27.6" width="3" height="5.6" rx="1.2" fill="${TYRE}"/>
        <rect x="18" y="29.6" width="4" height="6.4" rx="1.6" fill="${TYRE}"/>`,
    },
    default: {
      label: "Other",
      art: `<rect x="5.4" y="11" width="2.6" height="5.5" rx="1" fill="${DARK}"/><rect x="32" y="11" width="2.6" height="5.5" rx="1" fill="${DARK}"/>
        <path d="M8.5 9Q8.5 5.5 12 5.5h16q3.5 0 3.5 3.5V30.6h-23z" fill="#e2e8f0" stroke="#94a3b8" stroke-width=".9"/>
        <path d="M11 9.4h18l1 7.4H10z" fill="#334155"/>
        <path d="M12 16 16.2 10.2h3.4L15.4 16z" ${GLARE}/>
        <rect x="10.2" y="19.8" width="5" height="2.4" rx="1" fill="#f8fafc" stroke="#94a3b8" stroke-width=".6"/>
        <rect x="24.8" y="19.8" width="5" height="2.4" rx="1" fill="#f8fafc" stroke="#94a3b8" stroke-width=".6"/>
        <rect x="16.6" y="19.8" width="6.8" height="3.6" rx="1" fill="#64748b"/>
        <rect x="8" y="26.8" width="24" height="3.2" rx="1.6" fill="#475569"/>
        <rect x="10" y="30.2" width="4.2" height="4.2" rx="1" fill="${TYRE}"/><rect x="25.8" y="30.2" width="4.2" height="4.2" rx="1" fill="${TYRE}"/>`,
    },
  };

  // Spellings seen in comms and in the FMS Vehicle Type master (normalised: lower case, single spaces).
  const SYNONYMS = {
    bus: ["bus", "mini bus", "minibus", "school bus", "coach", "staff bus"],
    bike: ["bike", "motorbike", "motor bike", "motorcycle", "motor cycle", "two wheeler", "2 wheeler", "2w",
      "scooter", "scooty", "moped"],
    van: ["van", "mini van", "minivan", "tempo traveller", "tempo traveler", "tempo", "omni", "cargo van"],
    car: ["car", "sedan", "suv", "hatchback", "jeep", "muv", "mpv", "electric vehicle", "ev", "cab", "taxi"],
    truck: ["truck", "mini truck", "lorry", "lcv", "hcv", "light commercial vehicle", "heavy commercial vehicle",
      "trailer", "tipper", "tanker", "container", "pickup", "pick up"],
    auto: ["auto", "auto rickshaw", "autorickshaw", "rickshaw", "three wheeler", "3 wheeler", "3w",
      "e rickshaw", "erickshaw", "e auto"],
  };

  // Marker states from the real data (no invented ones), on the app's status tokens.
  // Movement states (from the feed's movement_state / ignition / connection_status) + the alert overlay.
  const STATUSES = {
    moving: { label: "Moving", token: "--fms-success" },
    idle: { label: "Idle", token: "--fms-warning" },
    stopped: { label: "Ignition Off", token: "--fms-text-muted" },
    offline: { label: "Offline", token: "--fms-text-muted" },
    alert: { label: "Alert", token: "--fms-danger" },
  };

  const LOOKUP = new Map();
  Object.entries(SYNONYMS).forEach(([kind, names]) => names.forEach((name) => LOOKUP.set(name, kind)));

  function escapeText(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function normalise(value) {
    return String(value ?? "").toLowerCase().replace(/[-_./]+/g, " ").replace(/\s+/g, " ").trim();
  }

  // Exact spelling first, then a known word inside the text ("School Bus 42" -> bus).
  function kindFor(type) {
    const text = normalise(type);
    if (!text) return "default";
    if (LOOKUP.has(text)) return LOOKUP.get(text);
    for (const word of text.split(" ")) {
      if (LOOKUP.has(word)) return LOOKUP.get(word);
    }
    return "default";
  }

  // What to show as "Type": the canonical name when recognised, else the raw text.
  function labelFor(type) {
    const kind = kindFor(type);
    if (kind !== "default") return VEHICLE_TYPES[kind].label;
    const raw = String(type ?? "").trim();
    return raw || "Other";
  }

  function configFor(type) {
    return VEHICLE_TYPES[kindFor(type)];
  }

  // The vehicle illustration on its own (headers, sheets).
  function svg(type, size = 24) {
    return `<svg class="vehicle-type-svg" viewBox="0 0 40 40" width="${size}" height="${size}" aria-hidden="true" focusable="false">${configFor(type).art}</svg>`;
  }

  /* ---------------- Status ---------------- */

  /* key: the movement state (Status rows). alert: the feed's active_alert (an alert whose condition
   * is still going on, apps.tracking.services.active_alerts_by_vehicle) or null. pin: what the pin
   * shows — ALERT > MOVING > IDLE > IGNITION OFF; when the alert clears it falls back by itself. */
  function statusFor(vehicle) {
    let key;
    if (vehicle.connection_status === "OFFLINE") key = "offline";
    else if (vehicle.movement_state === "MOVING") key = "moving";
    else if (vehicle.ignition === false) key = "stopped";
    else key = "idle";
    const alert = vehicle.active_alert || null;
    // The ACTUAL immobilizer state (device data), never a command's progress.
    const immobilized = vehicle.immobilizer === true;
    return {
      key, ...STATUSES[key], stale: vehicle.connection_status === "STALE", alert, immobilized,
      pin: immobilized ? "immobilized" : alert ? "alert" : key,
    };
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#64748b";
  }

  // A chip for tables / panels / popup headers: the marker's face (white face + illustration). Status is
  // shown by the surrounding UI (status columns / rows), never on the icon.
  function badgeHtml(type, { size = 30, className = "" } = {}) {
    const kind = kindFor(type);
    return `<span class="vehicle-type-badge vehicle-type-${kind} ${className}" style="width:${size}px;height:${size}px;" title="${escapeText(labelFor(type))}">${svg(type, Math.round(size * 0.78))}</span>`;
  }

  /* ---------------- Map marker ---------------- */

  const PIN_SVG = `<svg class="vm-pin" viewBox="0 0 48 60" aria-hidden="true" focusable="false">
      <path class="vm-pin-shape" d="M24 58.5c-2.4-5.6-6.4-9.4-11-13.6C7.6 40 3 34.2 3 24 3 12.4 12.4 3 24 3s21 9.4 21 21c0 10.2-4.6 16-10 20.9-4.6 4.2-8.6 8-11 13.6z"/>
      <circle cx="24" cy="24" r="16.4" fill="#fff"/>
      <circle cx="24" cy="24" r="16.4" fill="none" stroke="#0f172a" stroke-opacity=".06" stroke-width="1"/>
    </svg>`;

  function validHeading(value) {
    const n = Number(value);
    return value !== null && value !== undefined && value !== "" && Number.isFinite(n) ? ((n % 360) + 360) % 360 : null;
  }

  /* Direction (fleet feed "direction", apps/tracking/direction.py): the device heading when the
   * device really reports one, else the bearing of its GPS track. { degrees, cardinal, label,
   * source, moving } — degrees null = unknown. Older feeds without it fall back to "heading". */
  function directionOf(vehicle) {
    const d = vehicle.direction;
    if (d && d.degrees !== null && d.degrees !== undefined) return d;
    return { degrees: null, cardinal: null, label: null, source: null, moving: !!(d && d.moving) };
  }

  // "North-East · 45°", or "" when unknown.
  function directionText(vehicle) {
    const d = directionOf(vehicle);
    return d.degrees === null ? "" : `${d.label} · ${d.degrees}°`;
  }

  // A small compass whose needle points along the vehicle's direction (popup / panel).
  function compassHtml(vehicle, { size = 32 } = {}) {
    const d = directionOf(vehicle);
    const needle = d.degrees === null ? "" :
      `<span class="vm-compass-needle" style="transform:rotate(${d.degrees}deg)"></span>`;
    return `<span class="vm-compass${d.degrees === null ? " is-unknown" : ""}${d.moving ? " is-moving" : ""}" style="width:${size}px;height:${size}px" aria-hidden="true">
      <span class="vm-compass-n">N</span>${needle}</span>`;
  }

  function markerHtml(vehicle) {
    const kind = kindFor(vehicle.vehicle_type);
    return `
      <div class="vm vm--${statusFor(vehicle).key}" data-kind="${kind}">
        <span class="vm-ring"></span>
        ${PIN_SVG}
        <span class="vm-art">${svg(vehicle.vehicle_type, 28)}</span>
        <span class="vm-dir" aria-hidden="true"><i></i></span>
        <span class="vm-label"><span class="vm-label-reg">${escapeText(vehicle.registration_number)}</span><span class="vm-label-extra"></span></span>
      </div>`;
  }

  // Anchored at the pin's tip, so the tip sits exactly on the GPS position.
  function markerIcon(vehicle) {
    return global.L.divIcon({ html: markerHtml(vehicle), className: "vm-icon", iconSize: [48, 60], iconAnchor: [24, 58] });
  }

  function tooltipHtml(vehicle) {
    const status = statusFor(vehicle);
    const speed = vehicle.speed !== null && vehicle.speed !== undefined && vehicle.speed !== ""
      ? `<div class="vm-tip-speed">Speed: ${Math.round(Number(vehicle.speed))} km/h</div>` : "";
    return `<div class="vm-tip">
      <div class="vm-tip-reg">${escapeText(vehicle.registration_number)}</div>
      <div class="vm-tip-type">${escapeText(labelFor(vehicle.vehicle_type))}</div>
      <div class="vm-tip-status"><span class="vm-tip-dot" style="background:${cssVar(status.token)}"></span>${status.label}${status.stale ? " (stale)" : ""}</div>
      ${status.immobilized ? '<div class="vm-tip-immo"><i class="bi bi-lock-fill"></i>Immobilizer ON</div>' : ""}
      ${status.alert ? `<div class="vm-tip-alert"><i class="bi bi-exclamation-triangle-fill"></i>${escapeText(status.alert.label)}</div>` : ""}
      ${speed}
      ${directionTipHtml(vehicle)}
    </div>`;
  }

  function directionTipHtml(vehicle) {
    const d = directionOf(vehicle);
    if (d.degrees === null) return d.moving ? "" : '<div class="vm-tip-dir"><i class="bi bi-compass"></i>Stationary</div>';
    const text = d.moving ? `Heading ${d.label} (${d.degrees}°)` : `Stationary · last heading ${d.cardinal}`;
    return `<div class="vm-tip-dir"><i class="bi bi-compass"></i>${escapeText(text)}</div>`;
  }

  /* Patch an existing marker in place — only what changed:
   *   type -> illustration, status / alert -> classes (pin colour), direction -> arrow rotation only,
   *   selection -> ring, speed / registration -> label + tooltip. */
  function updateMarker(marker, vehicle, { selected = false } = {}) {
    const root = marker.getElement() && marker.getElement().querySelector(".vm");
    if (!root) return;
    const status = statusFor(vehicle);
    const kind = kindFor(vehicle.vehicle_type);
    // State lives on the DOM node: if Leaflet ever rebuilds the icon, it is simply re-applied.
    const st = root.fmsState || (root.fmsState = { kind: root.dataset.kind });

    if (st.kind !== kind) {
      root.dataset.kind = kind;
      root.querySelector(".vm-art").innerHTML = svg(vehicle.vehicle_type, 28);
      st.kind = kind;
    }
    const direction = directionOf(vehicle);
    // The vehicle's direction (device heading or GPS track); the raw "heading" only as a fallback.
    const heading = direction.degrees !== null ? direction.degrees : validHeading(vehicle.heading);
    const showArrow = direction.moving && direction.degrees !== null; // arrow only while it is moving
    const statusClass = `vm vm--${status.key}${status.alert ? " vm--alert" : ""}${status.immobilized ? " vm--immobilized" : ""}${status.stale ? " vm--stale" : ""}${selected ? " is-selected" : ""}${showArrow ? " has-dir" : ""}`;
    if (st.statusClass !== statusClass) {
      root.className = statusClass; // pin colour comes from CSS tokens (theme-aware, transitions smoothly)
      st.statusClass = statusClass;
      marker.setZIndexOffset(selected ? 1000 : status.alert ? 500 : status.key === "moving" ? 200 : 0);
    }
    // Heading: only the direction arrow turns (picture and pin stay put), the short way round (350 -> 10 = +20).
    // No heading in the data -> the graphic stays upright.
    if (heading !== null) {
      const previous = st.rotation ?? heading;
      const delta = ((heading - (previous % 360)) + 540) % 360 - 180;
      const rotation = st.rotation === undefined ? heading : previous + delta;
      if (rotation !== st.rotation) {
        // Only the direction arrow turns; the vehicle picture always stays upright.
        const arrow = root.querySelector(".vm-dir"); // absent on markers built before this version
        if (arrow) arrow.style.transform = `rotate(${rotation}deg)`;
        st.rotation = rotation;
      }
    }
    const extra = status.key === "moving" && vehicle.speed !== null && vehicle.speed !== undefined
      ? ` · ${Math.round(Number(vehicle.speed))} km/h${showArrow ? ` · ${direction.cardinal}` : ""}` : "";
    if (st.extra !== extra) {
      root.querySelector(".vm-label-extra").textContent = extra;
      st.extra = extra;
    }
    if (st.reg !== vehicle.registration_number) {
      root.querySelector(".vm-label-reg").textContent = vehicle.registration_number ?? "";
      st.reg = vehicle.registration_number;
    }
    const tip = tooltipHtml(vehicle);
    if (marker.fmsTip !== tip) {
      if (marker.getTooltip()) marker.setTooltipContent(tip);
      else marker.bindTooltip(tip, { direction: "top", offset: [0, -56], className: "vm-tooltip", opacity: 1 });
      marker.fmsTip = tip;
    }
  }

  /* ---------------- Smooth movement ----------------
   * Glide a marker to its new position (requestAnimationFrame, linear, ~1 s).
   * Jumps instead when the tab is hidden, the hop is long (a new trip or a
   * data gap — not a drive to animate), or the map is zoomed far out. */
  const MAX_ANIMATED_METRES = 2000;

  function moveMarker(marker, latlng, { map, duration = 1000 } = {}) {
    const from = marker.getLatLng();
    const to = global.L.latLng(latlng);
    if (marker.fmsAnim) global.cancelAnimationFrame(marker.fmsAnim);
    marker.fmsAnim = null;
    if (from.equals(to)) return;
    const far = map && map.distance(from, to) > MAX_ANIMATED_METRES;
    if (document.hidden || far || (map && map.getZoom() < 9)) {
      marker.setLatLng(to);
      return;
    }
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      marker.setLatLng([from.lat + (to.lat - from.lat) * t, from.lng + (to.lng - from.lng) * t]);
      marker.fmsAnim = t < 1 ? global.requestAnimationFrame(step) : null;
    };
    marker.fmsAnim = global.requestAnimationFrame(step);
  }

  /* ---------------- Zoom-dependent detail ----------------
   * far (< 12): compact pins, no labels. normal (12-15): full pins, labels on
   * hover / selection. near (>= 16): every label shown, with speed when moving. */
  function applyZoomClass(map) {
    const zoom = map.getZoom();
    const el = map.getContainer();
    el.classList.toggle("vm-zoom-far", zoom < 12);
    el.classList.toggle("vm-zoom-near", zoom >= 16);
  }

  global.FmsVehicleIcons = {
    VEHICLE_TYPES, SYNONYMS, STATUSES, kindFor, labelFor, svg, badgeHtml, statusFor, markerIcon, updateMarker,
    tooltipHtml, moveMarker, applyZoomClass, directionOf, directionText, compassHtml,
  };
})(window);
