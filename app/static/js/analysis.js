/**
 * Analytical dataset builder: "copy link" and loading a saved configuration.
 */
(function () {
  const copyButton = document.getElementById("copy-link");
  if (copyButton) {
    copyButton.addEventListener("click", () => {
      const done = () => {
        copyButton.textContent = "Link copied";
        setTimeout(() => (copyButton.textContent = "Copy link"), 2000);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(window.location.href).then(done, () => window.prompt("Copy this link:", window.location.href));
      } else {
        window.prompt("Copy this link:", window.location.href);
      }
    });
  }

  // A configuration file holds {"kind": "analysis-configuration", "parameters": {name: [values]}};
  // loading one rebuilds the same selection from its parameters.
  const input = document.getElementById("load-config");
  const status = document.getElementById("config-status");
  const form = document.getElementById("builder-form");
  if (!input || !form) return;

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) return;
    file.text().then((text) => {
      let config;
      try {
        config = JSON.parse(text);
      } catch (error) {
        throw new Error("That file is not valid JSON.");
      }
      if (!config || config.kind !== "analysis-configuration" || typeof config.parameters !== "object") {
        throw new Error("That file is not an epaData analysis configuration.");
      }
      const query = new URLSearchParams();
      Object.entries(config.parameters).forEach(([name, values]) => {
        (Array.isArray(values) ? values : [values]).forEach((v) => {
          if (typeof v === "string" || typeof v === "number") query.append(name, String(v));
        });
      });
      window.location.assign(form.getAttribute("action") + (query.toString() ? "?" + query : ""));
    }).catch((error) => {
      status.textContent = error.message;
      status.classList.add("text-danger");
      input.value = "";
    });
  });
})();
