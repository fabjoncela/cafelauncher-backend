"use strict";

const copyButton = document.querySelector("#copy-diagnostics");
const copyStatus = document.querySelector("#copy-diagnostics-status");

async function copyDiagnostics() {
  const statusCards = [...document.querySelectorAll(".status-card")]
    .map((card) => card.textContent.replace(/\s+/g, " ").trim());
  const deployment = document.querySelector(".deployment-info")?.textContent.replace(/\s+/g, " ").trim();
  const report = [
    "CafeLauncher Backend diagnostics",
    `Page: ${location.origin}`,
    `Checked: ${new Date().toISOString()}`,
    ...statusCards,
    deployment,
  ].filter(Boolean).join("\n");

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(report);
    } else {
      const input = document.createElement("textarea");
      input.value = report;
      input.setAttribute("readonly", "");
      input.className = "clipboard-fallback";
      document.body.append(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) throw new Error("Clipboard access is unavailable.");
    }
    copyStatus.textContent = "Diagnostics copied.";
  } catch (error) {
    copyStatus.textContent = `Copy failed: ${error.message}`;
  }
}

copyButton.addEventListener("click", copyDiagnostics);
