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


const repositoryActivityGrid = document.querySelector("#repository-activity-grid");
const repositoryActivityChecked = document.querySelector("#repository-activity-checked");

function repositoryRelativeAge(date) {
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "just now";
  const units = [[86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [size, label] of units) {
    if (seconds >= size) {
      const amount = Math.floor(seconds / size);
      return `${amount} ${label}${amount === 1 ? "" : "s"} ago`;
    }
  }
  return "just now";
}

function renderRepositoryActivity(payload) {
  const repositories = Array.isArray(payload?.repositories) ? payload.repositories : [];
  repositoryActivityGrid.replaceChildren();
  for (const repository of repositories) {
    const card = document.createElement("article");
    card.className = "status-card";
    const label = document.createElement("p");
    label.className = "card-label";
    label.textContent = repository.name || repository.repository || "Repository";
    const value = document.createElement("p");
    value.className = "card-value release-value";
    const detail = document.createElement("p");
    detail.className = "card-detail";
    detail.textContent = repository.role || "";
    const link = document.createElement("a");
    link.href = repository.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "View on GitHub";
    link.className = "repository-link";
    if (repository.status === "available" && repository.pushedAt) {
      const pushedAt = new Date(repository.pushedAt);
      if (Number.isNaN(pushedAt.getTime())) {
        value.textContent = "Time unavailable";
      } else {
        const time = document.createElement("time");
        time.dateTime = pushedAt.toISOString();
        time.textContent = repositoryRelativeAge(pushedAt);
        time.title = pushedAt.toLocaleString();
        value.append(time);
        detail.textContent = `${repository.role || ""} · ${pushedAt.toLocaleString()}`;
      }
    } else {
      value.textContent = "Unavailable";
      detail.textContent = repository.error || "GitHub metadata could not be loaded.";
    }
    card.append(label, value, detail, link);
    repositoryActivityGrid.append(card);
  }
  if (!repositories.length) {
    const message = document.createElement("p");
    message.className = "card-detail";
    message.textContent = "Repository activity is temporarily unavailable.";
    repositoryActivityGrid.append(message);
  }
  const checkedAt = new Date(payload?.checkedAt || "");
  repositoryActivityChecked.textContent = Number.isNaN(checkedAt.getTime())
    ? "Last checked: unavailable"
    : `Checked ${checkedAt.toLocaleTimeString()}`;
}

async function refreshRepositoryActivity() {
  try {
    const response = await fetch("/v1/repository-status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderRepositoryActivity(await response.json());
  } catch {
    repositoryActivityChecked.textContent = "GitHub status unavailable";
    if (!repositoryActivityGrid.children.length) {
      repositoryActivityGrid.textContent = "Could not load repository activity.";
    }
  }
}

refreshRepositoryActivity();
window.setInterval(refreshRepositoryActivity, 60 * 1000);
