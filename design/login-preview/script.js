/* ==========================================================================
   Zentora FMS — Premium Login Page interactions
   Vanilla JS, no framework. Responsibilities:
     1. Render Lucide icons
     2. Password show/hide toggle
     3. Remember-me persistence (localStorage, prototype-only)
     4. Client-side validation + inline field errors
     5. Submit handling: loading state + error component, ready for a real API
     6. Forgot password / Terms placeholder handlers
   ========================================================================== */

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) {
    window.lucide.createIcons();
  }

  initPasswordToggle();
  initRememberMe();
  initLoginForm();
  initPlaceholderLinks();
});

/* ---------------------------------------------------------------------- */
/* 1. Password show/hide                                                  */
/* ---------------------------------------------------------------------- */

function initPasswordToggle() {
  const toggleButton = document.getElementById("passwordToggle");
  const passwordInput = document.getElementById("password");
  const eyeIcon = document.getElementById("eyeIcon");
  const eyeOffIcon = document.getElementById("eyeOffIcon");

  if (!toggleButton || !passwordInput) return;

  toggleButton.addEventListener("click", () => {
    const isHidden = passwordInput.type === "password";

    passwordInput.type = isHidden ? "text" : "password";
    toggleButton.setAttribute("aria-pressed", String(isHidden));
    toggleButton.setAttribute("aria-label", isHidden ? "Hide password" : "Show password");

    if (eyeIcon && eyeOffIcon) {
      eyeIcon.hidden = isHidden;
      eyeOffIcon.hidden = !isHidden;
    }
  });
}

/* ---------------------------------------------------------------------- */
/* 2. Remember me — persisted locally so returning to the prototype keeps  */
/*    the last choice (a real backend would instead extend session length) */
/* ---------------------------------------------------------------------- */

const REMEMBER_ME_KEY = "zentora.fms.rememberMe";

function initRememberMe() {
  const checkbox = document.getElementById("rememberMe");
  if (!checkbox) return;

  checkbox.checked = localStorage.getItem(REMEMBER_ME_KEY) === "1";

  checkbox.addEventListener("change", () => {
    localStorage.setItem(REMEMBER_ME_KEY, checkbox.checked ? "1" : "0");
  });
}

/* ---------------------------------------------------------------------- */
/* 3 & 4. Form validation + submit                                        */
/* ---------------------------------------------------------------------- */

function initLoginForm() {
  const form = document.getElementById("loginForm");
  const usernameInput = document.getElementById("username");
  const passwordInput = document.getElementById("password");
  const submitButton = document.getElementById("signinButton");

  if (!form) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault(); // remove once a real endpoint is wired up

    hideFormAlert();
    // Both fields are validated (not short-circuited) so both errors show at once.
    const isUsernameValid = validateField(usernameInput, "Enter your email or username.");
    const isPasswordValid = validateField(passwordInput, "Enter your password.");
    if (!isUsernameValid || !isPasswordValid) return;

    setLoading(submitButton, true);

    try {
      await submitLogin({
        username: usernameInput.value.trim(),
        password: passwordInput.value,
        rememberMe: document.getElementById("rememberMe").checked,
      });

      // On success a real integration would redirect, e.g.:
      // window.location.href = "/dashboard/";
      showFormAlert("Sign-in isn't connected to a backend in this preview — wire up submitLogin() in script.js.");
    } catch (error) {
      showFormAlert(error.message || "Sign in failed. Please check your credentials and try again.");
    } finally {
      setLoading(submitButton, false);
    }
  });

  // Clear a field's error as soon as the user starts correcting it.
  [usernameInput, passwordInput].forEach((input) => {
    input.addEventListener("input", () => clearFieldError(input));
  });
}

function validateField(input, message) {
  if (!input.value.trim()) {
    showFieldError(input, message);
    return false;
  }
  clearFieldError(input);
  return true;
}

function showFieldError(input, message) {
  input.classList.add("is-invalid");
  input.setAttribute("aria-invalid", "true");
  const errorEl = document.getElementById(`${input.id}Error`);
  if (errorEl) errorEl.textContent = message;
}

function clearFieldError(input) {
  input.classList.remove("is-invalid");
  input.removeAttribute("aria-invalid");
  const errorEl = document.getElementById(`${input.id}Error`);
  if (errorEl) errorEl.textContent = "";
}

function showFormAlert(message) {
  const alertBox = document.getElementById("formAlert");
  const alertText = document.getElementById("formAlertText");
  if (!alertBox || !alertText) return;
  alertText.textContent = message;
  alertBox.hidden = false;
}

function hideFormAlert() {
  const alertBox = document.getElementById("formAlert");
  if (alertBox) alertBox.hidden = true;
}

function setLoading(button, isLoading) {
  if (!button) return;
  button.disabled = isLoading;
  button.classList.toggle("is-loading", isLoading);
  button.setAttribute("aria-busy", String(isLoading));
  const label = button.querySelector(".btn-label");
  if (label) label.textContent = isLoading ? "Signing In…" : "Sign In";
}

/**
 * Replace this with a real call to your authentication API, e.g. the
 * Django FMS backend:
 *
 *   const response = await fetch("/api/login/", {
 *     method: "POST",
 *     headers: { "Content-Type": "application/json" },
 *     body: JSON.stringify({ username, password, remember_me: rememberMe }),
 *   });
 *   if (!response.ok) {
 *     const data = await response.json().catch(() => ({}));
 *     throw new Error(data.detail || "Invalid credentials");
 *   }
 *   return response.json();
 */
async function submitLogin({ username, password, rememberMe }) {
  await simulateNetworkDelay();
  return { username, password, rememberMe }; // placeholder result
}

function simulateNetworkDelay(ms = 600) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ---------------------------------------------------------------------- */
/* 5. Forgot password / Terms — placeholders                              */
/* ---------------------------------------------------------------------- */

function initPlaceholderLinks() {
  document.querySelectorAll(".placeholder-link").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      // TODO: point each of these at real routes once they exist, e.g.
      // "/accounts/password/reset/", "/legal/terms/", "/legal/privacy/".
    });
  });
}
