(() => {
  "use strict";

  const form = document.getElementById("documentForm");
  if (!form) return;

  /* ---------------- Upload dropzone ---------------- */
  const dropzone = document.getElementById("uploadDropzone");
  const fileInput = form.querySelector("input[name='file']");
  const idleState = document.getElementById("uploadDropzoneIdle");
  const fileState = document.getElementById("uploadDropzoneFile");
  const fileNameEl = document.getElementById("uploadFileName");
  const fileMetaEl = document.getElementById("uploadFileMeta");
  const removeBtn = document.getElementById("uploadRemoveBtn");

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function showFile(file) {
    if (!file) return;
    fileNameEl.textContent = file.name;
    const ext = (file.name.split(".").pop() || "").toUpperCase();
    fileMetaEl.textContent = `${formatSize(file.size)} · ${ext}`;
    idleState.classList.add("d-none");
    fileState.classList.remove("d-none");
    dropzone.classList.add("has-file");
  }

  function clearFile() {
    fileInput.value = "";
    idleState.classList.remove("d-none");
    fileState.classList.add("d-none");
    dropzone.classList.remove("has-file");
  }

  fileInput?.addEventListener("change", () => {
    if (fileInput.files.length) showFile(fileInput.files[0]);
  });

  dropzone?.addEventListener("click", (event) => {
    if (event.target === removeBtn) return;
    fileInput.click();
  });

  removeBtn?.addEventListener("click", (event) => {
    event.stopPropagation();
    clearFile();
  });

  ["dragenter", "dragover"].forEach((evt) => {
    dropzone?.addEventListener(evt, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone?.addEventListener(evt, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });
  dropzone?.addEventListener("drop", (event) => {
    const dropped = event.dataTransfer?.files;
    if (dropped && dropped.length) {
      fileInput.files = dropped;
      showFile(dropped[0]);
    }
  });

  /* ---------------- Related record picker ---------------- */
  const entityTypeSelect = document.getElementById("entityTypeSelect");
  const relatedSearch = document.getElementById("relatedRecordSearch");
  const relatedSelect = document.getElementById("relatedRecordSelect");

  entityTypeSelect?.addEventListener("change", () => {
    const hasType = Boolean(entityTypeSelect.value);
    relatedSearch.disabled = !hasType;
    relatedSelect.disabled = !hasType;
    if (!hasType) relatedSelect.innerHTML = '<option value="">Select an entity type first…</option>';
  });
})();
