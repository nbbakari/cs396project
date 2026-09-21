(function () {
  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("data_file");
  const fileNameEl = document.getElementById("selected-file-name");
  const submitBtn = document.getElementById("upload-submit");
  if (!dropzone || !input) return;

  function showFileName() {
    if (input.files && input.files.length) {
      fileNameEl.textContent = input.files[0].name;
      dropzone.classList.add("has-file");
      if (submitBtn) submitBtn.disabled = false;
    } else {
      fileNameEl.textContent = "No file selected";
      dropzone.classList.remove("has-file");
      if (submitBtn) submitBtn.disabled = true;
    }
  }

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("dragover");
    })
  );

  ["dragleave", "dragend", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("dragover");
    })
  );

  dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) {
      input.files = files;
      showFileName();
    }
  });

  dropzone.addEventListener("click", (e) => {
    if (e.target !== input) input.click();
  });

  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      input.click();
    }
  });

  input.addEventListener("change", showFileName);

  showFileName();
})();