/*
 * FMS Authentication Experience — interactions
 * Loaded on every page extending templates/base_auth.html.
 *
 * Django owns the actual authentication (form POST, validation, redirects);
 * this file only handles client-side presentation: password visibility,
 * and a loading state on submit so double-clicking Sign In can't fire two
 * requests. Nothing here blocks or replaces the normal form submission.
 */

(() => {
  "use strict";

  document.addEventListener("DOMContentLoaded", () => {
    initPasswordToggles();
    initSubmitLoadingState();
  });

  function initPasswordToggles() {
    document.querySelectorAll("[data-toggle-password]").forEach((button) => {
      const input = document.getElementById(button.dataset.togglePassword);
      if (!input) return;

      button.addEventListener("click", () => {
        const isHidden = input.type === "password";
        input.type = isHidden ? "text" : "password";

        button.setAttribute("aria-pressed", String(isHidden));
        button.setAttribute("aria-label", isHidden ? "Hide password" : "Show password");

        const icon = button.querySelector("i");
        if (icon) {
          icon.className = isHidden ? "bi bi-eye-slash" : "bi bi-eye";
        }
      });
    });
  }

  function initSubmitLoadingState() {
    document.querySelectorAll("form.auth-form").forEach((form) => {
      const submitButton = form.querySelector(".auth-submit");
      if (!submitButton) return;

      form.addEventListener("submit", (event) => {
        // Guard against a double-click firing two submissions.
        if (submitButton.dataset.submitting === "1") {
          event.preventDefault();
          return;
        }
        submitButton.dataset.submitting = "1";
        submitButton.classList.add("is-loading");
        submitButton.setAttribute("aria-busy", "true");

        const label = submitButton.querySelector(".btn-label");
        if (label) label.textContent = submitButton.dataset.loadingLabel || "Signing in…";

        // Deferred so the click/submit that triggered this still goes through
        // with the button's own value, if the browser needs it.
        window.setTimeout(() => {
          submitButton.disabled = true;
        }, 0);
      });
    });
  }
})();
