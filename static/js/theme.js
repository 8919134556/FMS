/*
 * FMS Theme Switching
 * The actual initial (pre-paint) theme stamp happens in an inline <script>
 * at the top of base.html's <head> — this file handles everything after
 * first paint: the topbar quick-toggle, the "Appearance" radio controls in
 * the profile dropdown, persistence, and keeping every open control in sync
 * with each other and with the OS if the user is on "System".
 *
 * Storage contract: localStorage["fms-theme"] is "light", "dark", or absent
 * (absent = "system", i.e. follow prefers-color-scheme — see design-tokens.css).
 */

(() => {
  "use strict";

  const STORAGE_KEY = "fms-theme";
  const root = document.documentElement;
  const mediaDark = window.matchMedia("(prefers-color-scheme: dark)");

  function getStoredChoice() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function getEffectiveTheme() {
    const stored = getStoredChoice();
    if (stored === "light" || stored === "dark") return stored;
    return mediaDark.matches ? "dark" : "light";
  }

  function applyTheme(choice) {
    // Briefly enable transitions on themed surfaces so the switch feels like
    // a cross-fade rather than an instant flip, then remove the class so
    // normal interactions (hover, etc.) aren't slowed down by it.
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!prefersReducedMotion) {
      root.classList.add("theme-transitioning");
      window.setTimeout(() => root.classList.remove("theme-transitioning"), 260);
    }

    if (choice === "light" || choice === "dark") {
      try {
        localStorage.setItem(STORAGE_KEY, choice);
      } catch (e) { /* ignore — theme just won't persist across reloads */ }
      root.setAttribute("data-theme", choice);
    } else {
      // "system"
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch (e) { /* ignore */ }
      root.removeAttribute("data-theme");
    }

    syncControls();
  }

  function syncControls() {
    const effective = getEffectiveTheme();
    const storedChoice = getStoredChoice() || "system";

    // Quick toggle button in the topbar: shows the icon for the theme you'd
    // switch TO, and reflects the current effective (not just stored) theme.
    document.querySelectorAll("[data-theme-quick-toggle]").forEach((btn) => {
      const icon = btn.querySelector("i");
      if (icon) {
        icon.className = effective === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
      }
      btn.setAttribute("aria-label", effective === "dark" ? "Switch to light theme" : "Switch to dark theme");
    });

    // Appearance radio group in the profile dropdown (Light / Dark / System).
    document.querySelectorAll("[data-theme-option]").forEach((el) => {
      const isActive = el.dataset.themeOption === storedChoice;
      el.classList.toggle("is-active", isActive);
      el.setAttribute("aria-checked", String(isActive));
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    syncControls();

    document.querySelectorAll("[data-theme-quick-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyTheme(getEffectiveTheme() === "dark" ? "light" : "dark");
      });
    });

    document.querySelectorAll("[data-theme-option]").forEach((el) => {
      el.addEventListener("click", (event) => {
        event.preventDefault();
        applyTheme(el.dataset.themeOption);
      });
    });

    // If the user is on "System" and the OS theme changes live, follow it
    // without needing a click or a reload.
    mediaDark.addEventListener("change", () => {
      if (!getStoredChoice()) syncControls();
    });
  });
})();
