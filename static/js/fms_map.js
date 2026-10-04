/*
 * FmsMap — the one shared Leaflet tile-layer setup, so every page that shows
 * a real map (Live Tracking, Trip Report's route modal, …) uses the same
 * provider/attribution/referrer settings instead of each redefining them.
 */
(function (global) {
  "use strict";

  const TILE_CONFIG = {
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  };
  // PRODUCTION NOTE: tile.openstreetmap.org is the OSM Foundation's demo
  // tile server (see https://operations.osmfoundation.org/policies/tiles/)
  // — it disallows heavy/commercial production traffic without prior
  // arrangement. Swap this for a paid tile provider (MapTiler/Stadia/
  // Mapbox) or a self-hosted tile server before real production load.
  // Used here only because it needs no API key for dev/demo.

  // Adds the shared tile layer to an already-created Leaflet map and returns it.
  function addTileLayer(map) {
    // OSM's tile policy requires a Referer. The site-wide Referrer-Policy is
    // Django's default "same-origin", which strips it on cross-origin
    // requests and gets tiles blocked with 403 — so only tile requests opt
    // in to sending the origin (scheme+host, no path/query).
    return L.tileLayer(TILE_CONFIG.url, {
      attribution: TILE_CONFIG.attribution,
      maxZoom: 19,
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(map);
  }

  global.FmsMap = { TILE_CONFIG, addTileLayer };
})(window);
