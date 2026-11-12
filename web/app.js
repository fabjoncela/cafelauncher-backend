"use strict";

const serviceStatus = document.querySelector("#service-status");
const applyProfileButton = document.querySelector("#apply-profile-button");
const applyGraphicsButton = document.querySelector("#apply-graphics-button");
const playButton = document.querySelector("#play-button");
const profileSelect = document.querySelector("#profile-select");
const actionButtons = [applyProfileButton, applyGraphicsButton, playButton];
const resultPanel = document.querySelector("#result-panel");
const protocolFallback = document.querySelector("#protocol-fallback");
const protocolLink = document.querySelector("#protocol-link");
const unlockButton = document.querySelector("#unlock-button");
const confirmDialog = document.querySelector("#confirm-dialog");
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
}

const actionLabels = {
  "apply-profile": "Apply profile",
  "apply-graphics": "Apply game graphics",
  "play-exec": "Play + Exec",
};

function confirmAction(action, profile) {
  const label = actionLabels[action];
  document.querySelector("#confirm-title").textContent = `${label} · ${profile}`;
  const descriptions = {
    "apply-profile": `CafeLauncher will apply the verified ${profile} profile to this Windows session. This can change mouse and NVIDIA settings, install the profile CFG, and open the profile's configured apps. Live Logitech DPI remains in the Windows app.`,
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
    renderResult(state);
    if (["COMPLETED", "PARTIAL", "SKIPPED", "FAILED", "CANCELED", "EXPIRED", "INTERRUPTED"].includes(state.state)) {
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
initialize();
