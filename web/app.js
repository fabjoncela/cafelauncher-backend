"use strict";

const serviceStatus = document.querySelector("#service-status");
const applyProfileButton = document.querySelector("#apply-profile-button");
const applyGraphicsButton = document.querySelector("#apply-graphics-button");
const playButton = document.querySelector("#play-button");
const checkPcButton = document.querySelector("#check-pc-button");
const profileSelect = document.querySelector("#profile-select");
const actionButtons = [applyProfileButton, applyGraphicsButton, playButton, checkPcButton];
const resultPanel = document.querySelector("#result-panel");
const protocolFallback = document.querySelector("#protocol-fallback");
const protocolLink = document.querySelector("#protocol-link");
const unlockButton = document.querySelector("#unlock-button");
const confirmDialog = document.querySelector("#confirm-dialog");
const recentStorageKey = "cafelauncher-recent-actions";
const terminalStates = new Set(["COMPLETED", "PARTIAL", "SKIPPED", "FAILED", "CANCELED", "EXPIRED", "INTERRUPTED"]);
let csrfToken = "";
let activeRequest = false;

function setServiceStatus(message, kind = "") {
  serviceStatus.textContent = message;
  serviceStatus.dataset.kind = kind;
}

function getPendingRequest() {
  try { return sessionStorage.getItem("cafelauncher-action-request") || ""; }
  catch { return ""; }
}

function setPendingRequest(requestId) {
  try { sessionStorage.setItem("cafelauncher-action-request", requestId); }
  catch { /* The current page can still track this action in memory. */ }
}

function clearPendingRequest() {
  try { sessionStorage.removeItem("cafelauncher-action-request"); }
  catch { /* Nothing else is needed if browser storage is disabled. */ }
}

function loadRecentActions() {
  try {
    const entries = JSON.parse(sessionStorage.getItem(recentStorageKey) || "[]");
    if (!Array.isArray(entries)) return [];
    return entries.filter((entry) => entry &&
      typeof entry.requestId === "string" && /^[A-Za-z0-9_-]{43}$/.test(entry.requestId) &&
      Object.prototype.hasOwnProperty.call(actionLabels, entry.action) &&
      typeof entry.state === "string" && terminalStates.has(entry.state) &&
      typeof entry.profile === "string" && entry.profile.length <= 64 &&
      typeof entry.executionOutcomeKnown === "boolean" &&
      typeof entry.completedUtc === "string");
  } catch {
    return [];
  }
}

function canRetryRecent(entry) {
  if (entry.action === "detect-pc") return entry.state !== "INTERRUPTED";
  if (entry.state === "EXPIRED") return true;
  return entry.state === "SKIPPED" && entry.executionOutcomeKnown === true;
}

function renderRecentActions() {
  const list = document.querySelector("#recent-list");
  const empty = document.querySelector("#recent-empty");
  list.replaceChildren();
  const entries = loadRecentActions().slice(0, 6);
  empty.hidden = entries.length > 0;
  for (const entry of entries) {
    const item = document.createElement("li");
    item.className = "recent-entry";
    const main = document.createElement("div");
    main.className = "recent-entry-main";
    const title = document.createElement("strong");
    title.className = "recent-entry-title";
    title.textContent = `${actionLabels[entry.action]}${entry.profile ? ` · ${entry.profile}` : ""}`;
    const meta = document.createElement("span");
    meta.className = "recent-entry-meta";
    const date = new Date(entry.completedUtc);
    meta.textContent = `${entry.state} · ${Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString()}`;
    main.append(title, meta);
    item.append(main);
    if (canRetryRecent(entry)) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "secondary-button";
      retry.textContent = "Retry";
      retry.addEventListener("click", () => startAction(entry.action, entry.profile || null));
      item.append(retry);
    }
    list.append(item);
  }
}

function recordRecentAction(state) {
  if (!terminalStates.has(state.state) || !Object.prototype.hasOwnProperty.call(actionLabels, state.action)) return;
  const entries = loadRecentActions().filter((entry) => entry.requestId !== state.requestId);
  entries.unshift({
    requestId: state.requestId,
    action: state.action,
    profile: typeof state.profile === "string" ? state.profile : "",
    state: state.state,
    executionOutcomeKnown: state.result?.data?.executionOutcomeKnown === true,
    completedUtc: new Date().toISOString(),
  });
  try { sessionStorage.setItem(recentStorageKey, JSON.stringify(entries.slice(0, 6))); }
  catch { /* Action history is optional when browser storage is unavailable. */ }
  renderRecentActions();
}

function setReadiness(id, value, kind = "unknown") {
  const element = document.querySelector(`#${id}`);
  element.textContent = value;
  element.dataset.state = kind;
}

function setReadinessOverall(value, kind = "unknown") {
  setReadiness("readiness-overall", value, kind);
}

function updateReadinessFromPcCheck(state) {
  const data = state.result?.data;
  if (!data?.steam || !data?.cs2) {
    setReadiness("readiness-runner", "No confirmed response", "error");
    setReadinessOverall("Could not check", "error");
    document.querySelector("#readiness-note").textContent = "The runner did not return PC details. Check the action result before relying on this readiness check.";
    return;
  }
  const version = typeof state.runnerVersion === "string" && state.runnerVersion ? ` · v${state.runnerVersion}` : "";
  setReadiness("readiness-runner", `Connected${version}`, "ready");
  setReadiness("readiness-steam", data.steam.status === "DETECTED" ? "Detected" : "Not detected", data.steam.status === "DETECTED" ? "ready" : "warning");
  setReadiness("readiness-session", data.steam.activeSessionPresent ? "Signed in" : "Not signed in", data.steam.activeSessionPresent ? "ready" : "warning");
  setReadiness("readiness-cs2", data.cs2.status === "DETECTED" ? "One install found" : "No unique install", data.cs2.status === "DETECTED" ? "ready" : "warning");
  const isReady = data.steam.status === "DETECTED" && data.steam.activeSessionPresent && data.cs2.status === "DETECTED";
  setReadinessOverall(isReady ? "Checks passed" : "Needs attention", isReady ? "ready" : "warning");
  document.querySelector("#readiness-note").textContent = "This read-only check does not apply settings. Each action runs its own required checks, and CafeLauncher checks for runner updates when it starts.";
}

function updateReadinessFromRunner(state) {
  if (typeof state.runnerVersion !== "string" || !state.runnerVersion) return;
  setReadiness("readiness-runner", `Connected · v${state.runnerVersion}`, "ready");
  if (document.querySelector("#readiness-overall").dataset.state === "unknown") {
    setReadinessOverall("Runner connected", "ready");
  }
}

function updateReadinessFromProfileApply(state) {
  const dpi = state.result?.steps?.find((step) => step.name === "Logitech live DPI");
  if (!dpi) return;
  if (dpi.status === "SUCCESS") setReadiness("readiness-logitech", "Applied and verified", "ready");
  else if (dpi.status === "SKIPPED") setReadiness("readiness-logitech", "Skipped · see action result", "warning");
  else if (["FAILED", "PARTIAL"].includes(dpi.status)) setReadiness("readiness-logitech", "Needs attention · see action result", "error");
  else setReadiness("readiness-logitech", `${dpi.status} · see action result`, "warning");
}

async function readJson(response) {
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error(`CafeLauncher returned HTTP ${response.status}.`);
  }
  if (!response.ok) {
    const error = new Error(body?.message || `CafeLauncher returned HTTP ${response.status}.`);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function initialize() {
  try {
    const session = await readJson(await fetch("/v1/actions/session", {
      credentials: "same-origin",
      cache: "no-store",
    }));
    csrfToken = session.csrfToken;
    const schema = await readJson(await fetch("/v1/viewer-schema", { cache: "no-store" }));
    profileSelect.replaceChildren();
    for (const profile of schema.profiles || []) {
      const option = document.createElement("option");
      option.value = profile.name;
      option.textContent = profile.name;
      profileSelect.append(option);
    }
    if (!profileSelect.options.length) throw new Error("No CafeLauncher profiles are available.");
    profileSelect.disabled = false;
    setActionsDisabled(false);
    setServiceStatus("Ready on this PC. Select an action to continue.", "ready");
    const pendingRequest = getPendingRequest();
    if (pendingRequest && /^[A-Za-z0-9_-]{43}$/.test(pendingRequest)) {
      activeRequest = true;
      setActionsDisabled(true);
      profileSelect.disabled = true;
      setServiceStatus("Reconnecting to the pending CafeLauncher action…", "working");
      const finished = await watchRequest(pendingRequest);
      if (finished) {
        activeRequest = false;
        setActionsDisabled(false);
        profileSelect.disabled = false;
      }
    }
  } catch (error) {
    setServiceStatus(error.message, "error");
  }
}

function newRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function setActionsDisabled(disabled) {
  for (const button of actionButtons) button.disabled = disabled;
  for (const button of document.querySelectorAll("#recent-list button")) button.disabled = disabled;
}

const actionLabels = {
  "detect-pc": "Check this PC",
  "apply-profile": "Apply profile",
  "apply-graphics": "Apply game graphics",
  "play-exec": "Play + Exec",
};

function confirmAction(action, profile) {
  const label = actionLabels[action];
  document.querySelector("#confirm-title").textContent = `${label} · ${profile}`;
  const descriptions = {
    "apply-profile": `CafeLauncher will apply the verified ${profile} profile to this Windows session. This can change mouse and NVIDIA settings, install the profile CFG, open the profile's configured apps, and set supported Logitech live DPI when the mouse is connected.`,
    "apply-graphics": `CafeLauncher will apply the verified ${profile} CS2 video settings and supported NVIDIA scaling. CS2 must be closed.`,
    "play-exec": `CafeLauncher will verify the current ${profile} profile and CFG, install the reset baseline and profile CFG, then ask Steam to start CS2.`,
  };
  document.querySelector("#confirm-description").textContent = descriptions[action];
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "confirm"), { once: true });
  });
}

document.querySelector("#confirm-cancel").addEventListener("click", () => confirmDialog.close("cancel"));
document.querySelector("#confirm-accept").addEventListener("click", () => confirmDialog.close("confirm"));

function renderResult(state) {
  resultPanel.hidden = false;
  protocolFallback.hidden = state.state !== "REQUESTED";
  if (state.state === "REQUESTED") {
    protocolLink.href = `cafelauncher://v1/run/${encodeURIComponent(state.requestId)}`;
  }
  const statePill = document.querySelector("#result-state");
  statePill.textContent = state.state;
  statePill.className = "state-pill";
  if (["COMPLETED", "SUCCESS"].includes(state.state)) statePill.classList.add("success");
  else if (["FAILED", "INTERRUPTED"].includes(state.state)) statePill.classList.add("failure");
  else statePill.classList.add("warning");

  const title = document.querySelector("#result-heading");
  title.textContent = `${actionLabels[state.action] || "CafeLauncher action"}${state.profile ? ` · ${state.profile}` : ""}`;
  const result = state.result;
  const fallbackSummary = state.state === "EXPIRED"
    ? "The runner did not claim this request before it expired. Check that CafeLauncher is installed on this PC, then retry."
    : state.state === "INTERRUPTED"
      ? "The runner stopped without a final result. Check this PC before retrying."
      : "The runner has not reported a final result yet.";
  document.querySelector("#result-summary").textContent = result?.summary || fallbackSummary;

  const list = document.querySelector("#result-steps");
  list.replaceChildren();
  const steps = result?.steps || state.events?.filter((event) => event.step).map((event) => event.step) || [];
  for (const step of steps) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.className = "step-status";
    label.textContent = step.status;
    const name = document.createElement("strong");
    name.textContent = step.name;
    const detail = document.createElement("span");
    detail.className = "step-detail";
    detail.textContent = step.detail || "";
    item.append(label, name, detail);
    list.append(item);
  }

  const data = document.querySelector("#result-data");
  if (result?.data) {
    data.hidden = false;
    data.textContent = JSON.stringify(result.data, null, 2);
  } else {
    data.hidden = true;
    data.textContent = "";
  }
}

async function watchRequest(requestId) {
  let delay = 700;
  while (true) {
    let state;
    try {
      state = await readJson(await fetch(`/v1/actions/${encodeURIComponent(requestId)}`, {
        credentials: "same-origin",
        cache: "no-store",
      }));
    } catch (error) {
      if (error.status && error.status < 500) {
        resultPanel.hidden = false;
        document.querySelector("#result-heading").textContent = "Action status unavailable";
        document.querySelector("#result-state").textContent = "CHECK PC";
        document.querySelector("#result-summary").textContent = `${error.message} The action may already have started. Check this PC before trying again.`;
        unlockButton.hidden = false;
        setServiceStatus("The request state is uncertain; action buttons stay disabled to prevent a duplicate run.", "error");
        return false;
      }
      setServiceStatus(`Connection lost while checking the action result. Retrying automatically: ${error.message}`, "notice");
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(3000, delay + 400);
      continue;
    }
    updateReadinessFromRunner(state);
    renderResult(state);
    if (["COMPLETED", "PARTIAL", "SKIPPED", "FAILED", "CANCELED", "EXPIRED", "INTERRUPTED"].includes(state.state)) {
      if (state.action === "detect-pc") updateReadinessFromPcCheck(state);
      if (state.action === "apply-profile") updateReadinessFromProfileApply(state);
      recordRecentAction(state);
      if (state.state === "INTERRUPTED" || state.result?.data?.executionOutcomeKnown === false) {
        setServiceStatus("The action may have changed this PC without a confirmed result. Check it before allowing another action.", "error");
        unlockButton.hidden = false;
        return false;
      }
      const statusText = `Action finished: ${state.state}.`;
      setServiceStatus(statusText, state.state === "COMPLETED" ? "ready" : "notice");
      clearPendingRequest();
      return true;
    }
    const events = state.events || [];
    const current = (events.length ? events[events.length - 1].state : "") || state.state;
    setServiceStatus(current === "AWAITING_LOCAL_INPUT" ? "Waiting for a Windows or device prompt on this PC…" : "Action running on this PC…", "working");
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(3000, delay + 400);
  }
}

unlockButton.addEventListener("click", () => {
  clearPendingRequest();
  activeRequest = false;
  unlockButton.hidden = true;
  setActionsDisabled(false);
  profileSelect.disabled = false;
  setServiceStatus("You confirmed this PC is ready for another action.", "ready");
});

async function startAction(action, profile = null) {
  if (activeRequest || !csrfToken) return;
  activeRequest = true;
  setActionsDisabled(true);
  profileSelect.disabled = true;
  if (action !== "detect-pc") {
    try {
      if (!(await confirmAction(action, profile))) {
        activeRequest = false;
        setActionsDisabled(false);
        profileSelect.disabled = false;
        setServiceStatus("Action canceled before it started.", "notice");
        return;
      }
    } catch (error) {
      activeRequest = false;
      setActionsDisabled(false);
      profileSelect.disabled = false;
      setServiceStatus(error.message, "error");
      return;
    }
  }
  resultPanel.hidden = true;
  setServiceStatus("Preparing a short-lived action request…", "working");
  let requestResolved = false;
  let finished = false;
  let ticket = null;
  try {
    const requestId = newRequestId();
    setPendingRequest(requestId);
    let retryDelay = 700;
    while (!ticket) {
      try {
        ticket = await readJson(await fetch("/v1/actions", {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: { "Content-Type": "application/json", "X-Cafe-CSRF": csrfToken },
          body: JSON.stringify({ action, profile, clientRequestId: requestId }),
        }));
      } catch (error) {
        if (error.status && error.status < 500) {
          clearPendingRequest();
          requestResolved = true;
          throw error;
        }
        setServiceStatus(`Ticket response is uncertain. Retrying the same request safely: ${error.message}`, "notice");
        await new Promise((resolve) => setTimeout(resolve, retryDelay));
        retryDelay = Math.min(3000, retryDelay + 400);
      }
    }
    requestResolved = true;
    setPendingRequest(ticket.requestId);
    setServiceStatus("Opening CafeLauncher on this PC…", "working");
    window.location.href = `cafelauncher://v1/run/${encodeURIComponent(ticket.requestId)}`;
    finished = await watchRequest(ticket.requestId);
  } catch (error) {
    setServiceStatus(error.message, "error");
  } finally {
    if (requestResolved && (!ticket || finished)) {
      activeRequest = false;
      setActionsDisabled(false);
      profileSelect.disabled = false;
    }
  }
}

applyProfileButton.addEventListener("click", () => startAction("apply-profile", profileSelect.value));
applyGraphicsButton.addEventListener("click", () => startAction("apply-graphics", profileSelect.value));
playButton.addEventListener("click", () => startAction("play-exec", profileSelect.value));
checkPcButton.addEventListener("click", () => startAction("detect-pc"));
document.querySelector("#clear-recent-button").addEventListener("click", () => {
  try { sessionStorage.removeItem(recentStorageKey); }
  catch { /* The visible history can still be cleared for this page. */ }
  renderRecentActions();
});
renderRecentActions();
initialize();
