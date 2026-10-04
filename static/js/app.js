(() => {
  "use strict";

  const shell = document.getElementById("fmsShell");
  const sidebar = document.getElementById("fmsSidebar");
  const backdrop = document.getElementById("fmsSidebarBackdrop");
  const mobileToggle = document.getElementById("fmsMobileSidebarToggle");
  const collapseToggle = document.getElementById("fmsSidebarCollapseToggle");

  /* ---------------- Mobile drawer ---------------- */
  function closeMobileDrawer() {
    shell?.classList.remove("is-mobile-open");
  }
  mobileToggle?.addEventListener("click", () => {
    shell?.classList.toggle("is-mobile-open");
  });
  backdrop?.addEventListener("click", closeMobileDrawer);
  window.addEventListener("resize", () => {
    if (window.innerWidth >= 1200) closeMobileDrawer();
  });

  /* ---------------- Desktop collapse (persisted) ---------------- */
  const COLLAPSE_KEY = "fms.sidebar.collapsed";
  if (shell && localStorage.getItem(COLLAPSE_KEY) === "1") {
    shell.classList.add("is-collapsed");
  }
  collapseToggle?.addEventListener("click", () => {
    const collapsed = shell.classList.toggle("is-collapsed");
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  });

  /* ---------------- Nav group expand/collapse (persisted per group) ---------------- */
  document.querySelectorAll("[data-nav-group]").forEach((group) => {
    const key = `fms.navgroup.${group.dataset.navGroup}`;
    const hasActiveLink = group.querySelector(".nav-link-fms.active") !== null;
    const stored = localStorage.getItem(key);
    if (stored === "collapsed" && !hasActiveLink) {
      group.classList.add("is-collapsed");
    }
    const toggle = group.querySelector("[data-nav-group-toggle]");
    toggle?.addEventListener("click", () => {
      const collapsed = group.classList.toggle("is-collapsed");
      localStorage.setItem(key, collapsed ? "collapsed" : "expanded");
    });
  });

  /* ---------------- Toasts ---------------- */
  document.querySelectorAll(".fms-toast").forEach((toast, index) => {
    setTimeout(() => {
      toast.classList.remove("show");
      toast.addEventListener("transitionend", () => toast.remove());
      setTimeout(() => toast.remove(), 400);
    }, 6000 + index * 300);
  });

  /* ---------------- Fullscreen toggle ---------------- */
  document.getElementById("fmsFullscreenToggle")?.addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen?.();
    } else {
      document.exitFullscreen?.();
    }
  });

  /* ---------------- Command palette (Ctrl/Cmd+K) ---------------- */
  const paletteEl = document.getElementById("fmsCommandPalette");
  if (paletteEl && window.bootstrap) {
    const palette = new bootstrap.Modal(paletteEl);
    document.addEventListener("keydown", (event) => {
      const isShortcut = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k";
      if (isShortcut) {
        event.preventDefault();
        palette.show();
      }
    });
    paletteEl.addEventListener("shown.bs.modal", () => {
      document.getElementById("fmsCommandPaletteInput")?.focus();
    });
  }

  /* ---------------- Confirm dialog: intercepts form[data-confirm] ---------------- */
  const confirmModalEl = document.getElementById("fmsConfirmModal");
  if (confirmModalEl && window.bootstrap) {
    const confirmModal = new bootstrap.Modal(confirmModalEl);
    const titleEl = document.getElementById("fmsConfirmModalTitle");
    const bodyEl = document.getElementById("fmsConfirmModalBody");
    const acceptBtn = document.getElementById("fmsConfirmModalAccept");
    let pendingForm = null;

    document.querySelectorAll("form[data-confirm]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        if (form.dataset.confirmed === "1") return;
        event.preventDefault();
        pendingForm = form;
        titleEl.textContent = form.dataset.confirmTitle || "Are you sure?";
        bodyEl.textContent = form.getAttribute("data-confirm");
        acceptBtn.classList.toggle("btn-danger", form.dataset.confirmTone !== "primary");
        acceptBtn.classList.toggle("btn-primary", form.dataset.confirmTone === "primary");
        confirmModal.show();
      });
    });

    acceptBtn?.addEventListener("click", () => {
      confirmModal.hide();
      if (pendingForm) {
        pendingForm.dataset.confirmed = "1";
        pendingForm.requestSubmit();
        pendingForm = null;
      }
    });
  }

  /* ---------------- Submit-button loading state (form[data-submit-loading] target) ---------------- */
  document.querySelectorAll("button[data-submit-loading]").forEach((button) => {
    const form = button.closest("form");
    if (!form) return;
    form.addEventListener("submit", () => {
      if (form.dataset.confirm && form.dataset.confirmed !== "1") return;
      if (button.dataset.locked === "1") return;
      button.dataset.locked = "1";
      button.disabled = true;
      button.innerHTML = `<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>${button.dataset.submitLoading}`;
    });
  });

  /* ---------------- Filter chip removal (?key= param stripped, page reloads) ---------------- */
  document.querySelectorAll("[data-remove-param]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const url = new URL(window.location.href);
      url.searchParams.delete(chip.dataset.removeParam);
      window.location.href = url.toString();
    });
  });

  /* ---------------- Notification bell (poll-based — no WebSockets/Celery) ---------------- */
  const bell = document.getElementById("fmsNotificationBell");
  if (bell) {
    const FEED_URL = bell.dataset.feedUrl;
    const MARK_READ_TEMPLATE = bell.dataset.markReadUrlTemplate;
    const MARK_ALL_READ_URL = bell.dataset.markAllReadUrl;
    const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
    const badge = document.getElementById("fmsNotificationBadge");
    const listEl = document.getElementById("fmsNotificationList");
    const csrfToken = document.cookie.match(/csrftoken=([^;]+)/)?.[1];

    function escapeHtml(value) {
      const div = document.createElement("div");
      div.textContent = value == null ? "" : String(value);
      return div.innerHTML;
    }

    function levelIcon(level) {
      return { INFO: "info-circle", SUCCESS: "check-circle", WARNING: "exclamation-triangle", CRITICAL: "exclamation-octagon" }[level] || "bell";
    }

    function renderList(items) {
      if (!items.length) {
        listEl.innerHTML = '<div class="text-small text-muted-fms text-center py-3">You\'re all caught up.</div>';
        return;
      }
      listEl.innerHTML = items
        .map(
          (n) => `
        <a class="notification-item${n.is_read ? "" : " is-unread"}${n.is_alert ? " is-alert" : ""}" href="${n.link_url ? escapeHtml(n.link_url) : "#"}" data-uuid="${n.uuid}">
          <i class="bi bi-${levelIcon(n.level)} notification-item-icon notification-level-${n.level.toLowerCase()}"></i>
          <span class="notification-item-body">
            <span class="notification-item-title">${escapeHtml(n.title)}</span>
            ${n.body ? `<span class="notification-item-text">${escapeHtml(n.body)}</span>` : ""}
          </span>
        </a>`
        )
        .join("");
      listEl.querySelectorAll(".notification-item").forEach((el) => {
        el.addEventListener("click", () => markRead(el.dataset.uuid));
      });
    }

    function updateBadge(count) {
      if (count > 0) {
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.classList.remove("d-none");
      } else {
        badge.classList.add("d-none");
      }
    }

    function markRead(uuid) {
      return fetch(MARK_READ_TEMPLATE.replace(ZERO_UUID, uuid), {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken, Accept: "application/json" },
      }).catch(() => {});
    }

    // Vehicle alert events (panic, ...): popup + one alarm per new event.
    window.FmsAlertCenter?.configure({
      timezone: bell.dataset.timezone || undefined,
      onOpen: (alert) => markRead(alert.notification_uuid),
    });

    function refresh() {
      fetch(FEED_URL, { headers: { Accept: "application/json" }, cache: "no-store" })
        .then((res) => (res.ok ? res.json() : Promise.reject()))
        .then((data) => {
          updateBadge(data.unread_count);
          renderList(data.results);
          window.FmsAlertCenter?.announce(data.alerts || []);
        })
        .catch(() => {
          listEl.innerHTML = '<div class="text-small text-muted-fms text-center py-3">Unable to load notifications.</div>';
        });
    }

    document.getElementById("fmsMarkAllReadBtn")?.addEventListener("click", () => {
      fetch(MARK_ALL_READ_URL, { method: "POST", headers: { "X-CSRFToken": csrfToken, Accept: "application/json" } })
        .then(() => refresh())
        .catch(() => {});
    });

    // Real time without WebSockets (this deployment has none): a tiny "pulse"
    // request every 2 s while the tab is visible (10 s in a background tab —
    // browsers throttle hidden timers anyway) returns only the newest unread
    // alert notification + unread count. The full feed — and with it the alert
    // popup and sound — is fetched the moment either changes, plus a full
    // refresh every 60 s for read/archived changes made elsewhere.
    const PULSE_URL = bell.dataset.pulseUrl;
    const VISIBLE_PULSE_MS = 2000;
    const HIDDEN_PULSE_MS = 10000;
    const FULL_REFRESH_MS = 60000;
    let lastPulse = 0;
    let lastFull = 0;
    let lastSeen = null; // { latest_alert, unread_count }
    let pulseBusy = false;

    function fullRefresh() {
      lastFull = Date.now();
      refresh();
    }

    function pulse() {
      if (pulseBusy) return;
      pulseBusy = true;
      lastPulse = Date.now();
      fetch(PULSE_URL, { headers: { Accept: "application/json" }, cache: "no-store" })
        .then((res) => (res.ok ? res.json() : Promise.reject()))
        .then((data) => {
          const changed = !lastSeen || data.latest_alert !== lastSeen.latest_alert
            || data.unread_count !== lastSeen.unread_count;
          lastSeen = data;
          if (changed || Date.now() - lastFull >= FULL_REFRESH_MS) fullRefresh();
        })
        .catch(() => {})
        .finally(() => { pulseBusy = false; });
    }

    fullRefresh();
    window.setInterval(() => {
      const due = document.hidden ? HIDDEN_PULSE_MS : VISIBLE_PULSE_MS;
      if (Date.now() - lastPulse >= due - 100) pulse();
    }, VISIBLE_PULSE_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pulse();
    });
  }

  /* ---------------- Responsive tables ----------------
   * On phones responsive.css turns each row into a card, using the column's
   * header text as the cell label. Here we copy that header text onto every
   * cell (data-label) and flag the table `.is-stacked`. It is redone whenever
   * a table body changes (Live Tracking re-renders its rows every refresh;
   * HTMX swaps in fragments). Opt out with class "no-stack" on the table.
   */
  function labelTable(table) {
    if (table.classList.contains("no-stack")) return;
    const headRow = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
    if (!headRow) return;
    const labels = [];
    Array.from(headRow.cells).forEach((th) => {
      const span = th.colSpan || 1;
      for (let i = 0; i < span; i += 1) labels.push((th.textContent || "").replace(/\s+/g, " ").trim());
    });
    let stackable = false;
    Array.from(table.tBodies).forEach((body) => {
      Array.from(body.rows).forEach((row) => {
        const cells = Array.from(row.cells);
        if (cells.some((cell) => cell.colSpan > 1) || cells.length !== labels.length) {
          row.classList.add("stack-skip"); // empty-state / spanning rows stay as a normal row
          return;
        }
        row.classList.remove("stack-skip");
        cells.forEach((cell, index) => {
          if (cell.dataset.label !== labels[index]) cell.dataset.label = labels[index];
          // Row-action cells: a lone kebab menu floats to the card's corner; a set
          // of buttons ("Acknowledge", "Resolve", ...) becomes a full-width button row.
          const isActions = /^actions?$/i.test(labels[index]);
          const isKebab = isActions && cell.querySelector(".kebab-btn") !== null && cell.querySelector(".btn") === null;
          cell.classList.toggle("cell-actions", isKebab);
          cell.classList.toggle("cell-actions-inline", isActions && !isKebab);
        });
        stackable = true;
      });
    });
    table.classList.toggle("is-stacked", stackable);
  }

  const labelObservers = new WeakSet();
  function labelAllTables(root) {
    (root || document).querySelectorAll("table.table").forEach((table) => {
      labelTable(table);
      Array.from(table.tBodies).forEach((body) => {
        if (labelObservers.has(body)) return;
        labelObservers.add(body);
        let queued = false;
        new MutationObserver(() => {
          if (queued) return;
          queued = true;
          window.requestAnimationFrame(() => {
            queued = false;
            labelTable(table);
          });
        }).observe(body, { childList: true });
      });
    });
  }
  labelAllTables(document);
  document.body.addEventListener("htmx:afterSwap", (event) => labelAllTables(event.target));

  /* ---------------- Collapsible filter bar (phones) ---------------- */
  document.querySelectorAll("[data-filter-bar]").forEach((bar) => {
    const toggle = bar.querySelector("[data-filter-bar-toggle]");
    const countEl = bar.querySelector("[data-filter-bar-count]");
    if (!toggle) return;
    const fields = Array.from(bar.querySelectorAll("form input:not([type=hidden]), form select"));
    const active = fields.filter((field) => field.value && field.value.trim() !== "").length;
    if (countEl && active > 0) {
      countEl.textContent = String(active);
      countEl.hidden = false;
    }
    const setOpen = (open) => {
      bar.classList.toggle("is-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    setOpen(active > 0); // a filtered list opens with its filters visible
    toggle.addEventListener("click", () => setOpen(!bar.classList.contains("is-open")));
  });
})();
