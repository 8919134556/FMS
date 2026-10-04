/*
 * Geofence form: type-dependent fields, the boundary map editor (circle or
 * polygon, on Leaflet + FmsMap tiles — no extra drawing library), and the
 * assigned-vehicle checklist. The server validates everything again
 * (apps.geofences.forms.GeofenceForm); this only makes it easy to fill in.
 */
(() => {
  "use strict";

  const form = document.getElementById("geofenceForm");
  if (!form) return;

  const $ = (id) => document.getElementById(id);
  const typeRadios = Array.from(document.querySelectorAll('input[name="geofence_type"]'));
  const shapeSelect = $("id_shape");
  const latInput = $("id_center_latitude");
  const lonInput = $("id_center_longitude");
  const radiusInput = $("id_radius_meters");
  const polygonInput = $("id_polygon_points");

  /* ---------------- Type: show the speed limit only for Speed Limit ---------------- */

  function selectedType() {
    const checked = typeRadios.find((radio) => radio.checked);
    return checked ? checked.value : "";
  }

  function syncType() {
    const type = selectedType();
    const isSpeed = type === "SPEED_LIMIT";
    $("gfSpeedLimitField").classList.toggle("d-none", !isSpeed);
    $("id_speed_limit_kmh").required = isSpeed;
    const help = $("gfTypeHelp");
    help.textContent = {
      ENTRY: help.dataset.entry, EXIT: help.dataset.exit, SPEED_LIMIT: help.dataset.speed, ENTRY_AND_EXIT: help.dataset.both,
    }[type] || "";
  }
  typeRadios.forEach((radio) => radio.addEventListener("change", syncType));
  syncType();

  /* ---------------- Map editor ---------------- */

  const DEFAULT_CENTER = [12.9716, 77.5946]; // Bengaluru until something is placed
  const color = getComputedStyle(document.documentElement).getPropertyValue("--fms-primary").trim() || "#2563eb";
  const map = L.map("gfMap", { zoomControl: true }).setView(DEFAULT_CENTER, 12);
  FmsMap.addTileLayer(map);

  let circle = null;
  let polygon = null;
  let vertexMarkers = [];
  let points = [];
  try {
    points = JSON.parse(polygonInput.value || "[]");
  } catch (badJson) {
    points = [];
  }

  function num(input) {
    const value = parseFloat(input.value);
    return Number.isFinite(value) ? value : null;
  }

  function drawCircle(fit) {
    if (polygon) { polygon.remove(); polygon = null; }
    vertexMarkers.forEach((m) => m.remove());
    vertexMarkers = [];
    const lat = num(latInput);
    const lon = num(lonInput);
    const radius = num(radiusInput) || 500;
    if (lat === null || lon === null) {
      if (circle) { circle.remove(); circle = null; }
      return;
    }
    if (!circle) {
      circle = L.circle([lat, lon], { radius, color, weight: 2, fillOpacity: 0.12 }).addTo(map);
    } else {
      circle.setLatLng([lat, lon]);
      circle.setRadius(radius);
    }
    if (fit) map.fitBounds(circle.getBounds().pad(0.2));
  }

  function drawPolygon(fit) {
    if (circle) { circle.remove(); circle = null; }
    vertexMarkers.forEach((m) => m.remove());
    vertexMarkers = points.map((p, index) => {
      const marker = L.marker(p, {
        draggable: true,
        icon: L.divIcon({ className: "gf-vertex", iconSize: [12, 12] }),
        title: `Corner ${index + 1}`,
      }).addTo(map);
      marker.on("drag", (event) => {
        const ll = event.target.getLatLng();
        points[index] = [round6(ll.lat), round6(ll.lng)];
        updatePolygonShape();
      });
      marker.on("dragend", savePolygon);
      return marker;
    });
    updatePolygonShape();
    if (fit && points.length) map.fitBounds(L.latLngBounds(points).pad(0.2));
    savePolygon();
  }

  function updatePolygonShape() {
    if (polygon) polygon.remove();
    polygon = points.length >= 2
      ? L.polygon(points, { color, weight: 2, fillOpacity: points.length >= 3 ? 0.12 : 0 }).addTo(map)
      : null;
  }

  function savePolygon() {
    polygonInput.value = JSON.stringify(points);
    $("gfPointCount").textContent = `${points.length} point${points.length === 1 ? "" : "s"}${points.length < 3 ? " (need at least 3)" : ""}`;
  }

  function round6(value) {
    return Math.round(value * 1e6) / 1e6;
  }

  function isPolygon() {
    return shapeSelect.value === "POLYGON";
  }

  function syncShape(fit) {
    const polygonMode = isPolygon();
    document.querySelector(".gf-circle-fields").classList.toggle("d-none", polygonMode);
    document.querySelector(".gf-polygon-fields").classList.toggle("d-none", !polygonMode);
    const help = $("gfMapHelp");
    help.textContent = polygonMode ? help.dataset.polygon : help.dataset.circle;
    if (polygonMode) drawPolygon(fit);
    else drawCircle(fit);
  }

  map.on("click", (event) => {
    const { lat, lng } = event.latlng;
    if (isPolygon()) {
      points.push([round6(lat), round6(lng)]);
      drawPolygon(false);
    } else {
      latInput.value = round6(lat).toFixed(6);
      lonInput.value = round6(lng).toFixed(6);
      if (!radiusInput.value) radiusInput.value = 500;
      drawCircle(false);
    }
  });

  [latInput, lonInput, radiusInput].forEach((input) => input.addEventListener("input", () => drawCircle(false)));
  shapeSelect.addEventListener("change", () => syncShape(true));
  $("gfUndoPoint").addEventListener("click", () => { points.pop(); drawPolygon(false); });
  $("gfClearPolygon").addEventListener("click", () => { points = []; drawPolygon(false); });
  syncShape(true);
  window.setTimeout(() => map.invalidateSize(), 100);

  /* ---------------- Assigned vehicles ---------------- */

  const items = Array.from(document.querySelectorAll("#gfVehicleList .geofence-vehicle-item"));
  function updateCount() {
    const selected = items.filter((item) => item.querySelector("input").checked).length;
    $("gfVehicleCount").textContent = `${selected} of ${items.length} selected`;
  }
  $("gfVehicleSearch").addEventListener("input", (event) => {
    const q = event.target.value.trim().toLowerCase();
    items.forEach((item) => item.classList.toggle("d-none", q !== "" && !item.dataset.name.includes(q)));
  });
  $("gfSelectAll").addEventListener("click", () => {
    items.filter((item) => !item.classList.contains("d-none")).forEach((item) => { item.querySelector("input").checked = true; });
    updateCount();
  });
  $("gfSelectNone").addEventListener("click", () => {
    items.forEach((item) => { item.querySelector("input").checked = false; });
    updateCount();
  });
  items.forEach((item) => item.querySelector("input").addEventListener("change", updateCount));
  updateCount();
})();
