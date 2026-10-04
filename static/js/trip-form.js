(() => {
  "use strict";

  const form = document.getElementById("tripForm");
  if (!form) return;

  const clientSelect = form.querySelector("[name='client']");
  const originSelect = form.querySelector("[name='origin_site']");
  const destinationSelect = form.querySelector("[name='destination_site']");
  const vehicleSelect = form.querySelector("[name='vehicle']");
  const driverSelect = form.querySelector("[name='driver']");

  function cacheOptions(select) {
    if (!select || select.dataset.cached) return;
    select._allOptions = Array.from(select.options);
    select.dataset.cached = "1";
  }
  [originSelect, destinationSelect, vehicleSelect, driverSelect].forEach(cacheOptions);

  // Sites: HARD filter — only the selected client's sites may be chosen.
  function filterSites(select) {
    if (!select || !clientSelect) return;
    const clientId = clientSelect.value;
    const current = select.value;
    select.innerHTML = "";
    select._allOptions.forEach((opt) => {
      const matches = !opt.value || !clientId || opt.dataset.client === clientId;
      if (matches) select.appendChild(opt);
    });
    if (Array.from(select.options).some((o) => o.value === current)) {
      select.value = current;
    } else {
      select.value = "";
    }
  }

  // Vehicles/Drivers: SOFT preference — this client's fleet first, then the rest.
  function preferClientOptions(select) {
    if (!select || !clientSelect) return;
    const clientId = clientSelect.value;
    const current = select.value;
    const blank = select._allOptions.filter((o) => !o.value);
    const preferred = select._allOptions.filter((o) => o.value && clientId && o.dataset.client === clientId);
    const rest = select._allOptions.filter((o) => o.value && !(clientId && o.dataset.client === clientId));
    select.innerHTML = "";
    [...blank, ...preferred, ...rest].forEach((opt) => select.appendChild(opt));
    select.value = current;
  }

  function applyClientScope() {
    filterSites(originSelect);
    filterSites(destinationSelect);
    preferClientOptions(vehicleSelect);
    preferClientOptions(driverSelect);
  }

  clientSelect?.addEventListener("change", applyClientScope);
  if (clientSelect?.value) applyClientScope();

  // Estimated duration display
  const startInput = form.querySelector("[name='scheduled_start']");
  const endInput = form.querySelector("[name='scheduled_end']");
  const durationDisplay = document.getElementById("tripDurationDisplay");

  function updateDuration() {
    if (!durationDisplay || !startInput?.value || !endInput?.value) {
      if (durationDisplay) durationDisplay.value = "—";
      return;
    }
    const start = new Date(startInput.value);
    const end = new Date(endInput.value);
    const diffMs = end - start;
    if (isNaN(diffMs) || diffMs <= 0) {
      durationDisplay.value = "Invalid range";
      return;
    }
    const hours = Math.floor(diffMs / 3600000);
    const minutes = Math.round((diffMs % 3600000) / 60000);
    durationDisplay.value = `${hours}h ${minutes}m`;
  }

  startInput?.addEventListener("change", updateDuration);
  endInput?.addEventListener("change", updateDuration);
  updateDuration();
})();
