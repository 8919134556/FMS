# Assets needed

Place these two files in this folder (`design/login-preview/assets/`):

- **fleet-login-bg.jpg** — the full highway/truck/plane/skyline/GPS-map photo.
  Recommended: 1920×1080 or larger, landscape, JPG (or WebP with a JPG
  fallback). `background-size: cover` in `style.css` crops it to fill the
  viewport, so keep the truck on the left and the map/skyline detail toward
  the right/center of the source photo if you can, since that's where the
  card's transparent left panel and the visible strip right of the card sit.
- **zentora-logo.png** — the actual Zentora Info Global Solutions logo (the
  full lockup: globe mark + "ZENTORA" + "Info Global Solutions"), PNG with a
  transparent background. Recommended height: at least 140px so it stays
  sharp when scaled down to the ~34px display height used in the logo chip.

Until these are added, the page will show a plain navy background and a
broken image icon where the logo goes — that's expected, not a bug.

## v2 changes from the first prototype

- Background filename changed from `background.jpg` → **`fleet-login-bg.jpg`**
  to match this version's more premium composition (see `style.css`,
  `.page { background-image: ... url("assets/fleet-login-bg.jpg") }`).
- Logo chip is now a compact, elegant inline badge (not a full-width white
  bar) — see `.logo-chip` in `style.css`.
