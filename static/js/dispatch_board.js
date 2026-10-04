(() => {
  "use strict";

  const refreshBtn = document.getElementById("dispatchRefreshBtn");
  refreshBtn?.addEventListener("click", () => {
    refreshBtn.classList.add("is-refreshing");
    refreshBtn.disabled = true;
    window.location.reload();
  });

  function wireResourceSearch(inputId, listId) {
    const input = document.getElementById(inputId);
    const list = document.getElementById(listId);
    if (!input || !list) return;
    input.addEventListener("input", () => {
      const term = input.value.trim().toLowerCase();
      list.querySelectorAll("[data-search]").forEach((row) => {
        row.style.display = row.dataset.search.includes(term) ? "" : "none";
      });
    });
  }

  wireResourceSearch("dispatchVehicleSearch", "dispatchVehicleList");
  wireResourceSearch("dispatchDriverSearch", "dispatchDriverList");

  // Mobile filter drawer — the filter form itself is untouched (still a
  // real GET form, still server-validated); this only toggles visibility.
  const filtersToggle = document.getElementById("dispatchFiltersToggle");
  const filtersPanel = document.getElementById("dispatchFiltersPanel");
  filtersToggle?.addEventListener("click", () => {
    const isOpen = filtersPanel.classList.toggle("is-open");
    filtersToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
  });

  // Mobile column switcher — one workflow column visible at a time. Purely
  // presentational: every column is still server-rendered with real data,
  // this only toggles which one is shown.
  const columnNavButtons = document.querySelectorAll(".dispatch-column-nav-btn");
  const columns = document.querySelectorAll(".dispatch-column[data-column]");
  columnNavButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      columnNavButtons.forEach((b) => b.classList.toggle("active", b === btn));
      columns.forEach((col) => {
        if (col.dataset.column === btn.dataset.columnTarget) {
          col.removeAttribute("data-mobile-hidden");
        } else {
          col.setAttribute("data-mobile-hidden", "");
        }
      });
    });
  });
})();
