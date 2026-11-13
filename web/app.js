"use strict";

const serviceStatus = document.querySelector("#service-status");
const applyProfileButton = document.querySelector("#apply-profile-button");
const applyGraphicsButton = document.querySelector("#apply-graphics-button");
const playButton = document.querySelector("#play-button");
const checkPcButton = document.querySelector("#check-pc-button");
const profileSelect = document.querySelector("#profile-select");
const profileDpi = document.querySelector("#profile-dpi");
const profilePointer = document.querySelector("#profile-pointer");
const profileCfg = document.querySelector("#profile-cfg");
const profileDisplay = document.querySelector("#profile-display");
const profileCs2Display = document.querySelector("#profile-cs2-display");
const profileCfgFiles = new Map();
const pointerSpeedLevels = [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20];
const actionButtons = [applyProfileButton, applyGraphicsButton, playButton, checkPcButton];
const resultPanel = document.querySelector("#result-panel");
const protocolFallback = document.querySelector("#protocol-fallback");
const protocolLink = document.querySelector("#protocol-link");
const unlockButton = document.querySelector("#unlock-button");
const cancelActionButton = document.querySelector("#cancel-action-button");
const downloadActionLogButton = document.querySelector("#download-action-log-button");
const updateRunnerLink = document.querySelector("#update-runner-link");
const updateRunnerNote = document.querySelector("#update-runner-note");
const cancelNote = document.querySelector("#cancel-note");
const currentStepTiming = document.querySelector("#current-step-timing");
const confirmDialog = document.querySelector("#confirm-dialog");
const recentStorageKey = "cafelauncher-recent-actions";
const terminalStates = new Set(["COMPLETED", "PARTIAL", "SKIPPED", "FAILED", "CANCELED", "EXPIRED", "INTERRUPTED"]);
let csrfToken = "";
let activeRequest = false;
let latestActionState = null;
let displayedProgressEventId = "";
let displayedProgressStartedAt = 0;
let profileSummaryRequest = 0;

const siteUpdatedTime = document.querySelector("#site-updated");
if (siteUpdatedTime?.dateTime) {
  const updatedAt = new Date(siteUpdatedTime.dateTime);
  if (!Number.isNaN(updatedAt.getTime())) {
    siteUpdatedTime.textContent = new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
    }).format(updatedAt);
  }
}

function setServiceStatus(message, kind = "") {
  serviceStatus.textContent = message;
  serviceStatus.dataset.kind = kind;
}

function formatPointerSpeedLevel(value) {
  if (!Number.isInteger(value)) return "Unavailable";
  let index = pointerSpeedLevels.indexOf(value);
  if (index < 0) {
    index = 0;
    let nearestDistance = Number.POSITIVE_INFINITY;
    for (let candidate = 0; candidate < pointerSpeedLevels.length; candidate += 1) {
      const distance = Math.abs(pointerSpeedLevels[candidate] - value);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        index = candidate;
      }
    }
  }
  return `${index + 1}/11`;
}

async function refreshSelectedProfileSummary() {
  const profileName = profileSelect.value;
  const requestNumber = ++profileSummaryRequest;
  if (!profileName) return;
  profileDpi.textContent = "Loading…";
  profilePointer.textContent = "Loading…";
  profileCfg.textContent = profileCfgFiles.get(profileName) || "Unavailable";
  profileDisplay.textContent = "Loading…";
  profileCs2Display.textContent = "Loading…";
  try {
    const bundle = await readJson(await fetch(`/v1/profiles/${encodeURIComponent(profileName)}/bundle`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    }));
    if (requestNumber !== profileSummaryRequest || profileSelect.value !== profileName) return;
    const profile = bundle.profile;
    profileDpi.textContent = Number.isInteger(profile.LogitechDPI) ? String(profile.LogitechDPI) : "Unavailable";
    profilePointer.textContent = formatPointerSpeedLevel(profile.PointerSpeed);
    const { Width: width, Height: height, RefreshHz: refreshHz } = profile;
    profileDisplay.textContent = [width, height, refreshHz].every(Number.isInteger)
      ? `${width}x${height} @${refreshHz} Hz`
      : "Unavailable";
    const { Width: cs2Width, Height: cs2Height, RefreshHz: cs2RefreshHz } = bundle.graphics?.Settings || {};
    profileCs2Display.textContent = [cs2Width, cs2Height, cs2RefreshHz].every(Number.isInteger)
      ? `${cs2Width}x${cs2Height} @${cs2RefreshHz} Hz`
      : "Unavailable";
  } catch {
    if (requestNumber !== profileSummaryRequest || profileSelect.value !== profileName) return;
    profileDpi.textContent = "Unavailable";
    profilePointer.textContent = "Unavailable";
    profileDisplay.textContent = "Unavailable";
    profileCs2Display.textContent = "Unavailable";
  }
}

function setUnlockButtonVisible(visible) {
  unlockButton.hidden = !visible;
  document.body.classList.toggle("action-recovery-visible", visible);
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
  const runnerStatus = document.querySelector("#runner-status");
  runnerStatus.textContent = `Runner ready · v${state.runnerVersion}`;
  runnerStatus.dataset.state = "ready";
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
      if (typeof profile.name === "string" && typeof profile.cfg === "string") profileCfgFiles.set(profile.name, profile.cfg);
    }
    if (!profileSelect.options.length) throw new Error("No CafeLauncher profiles are available.");
    profileSelect.disabled = false;
    await refreshSelectedProfileSummary();
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
    "apply-profile": `CafeLauncher will apply the verified ${profile} profile to this Windows session. This can change mouse and NVIDIA settings, install the profile CFG, and set supported Logitech live DPI when the mouse is connected. It also honors the profile's LaunchApps setting: when enabled, CafeLauncher starts Steam and FACEIT Anti-Cheat, opens FACEIT matchmaking and the profile's configured website, and opens Mouse Pointer Options. G HUB stays closed so it cannot overwrite live DPI.`,
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
profileSelect.addEventListener("change", refreshSelectedProfileSummary);

function renderResult(state) {
  latestActionState = state;
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
  const terminal = terminalStates.has(state.state);
  cancelActionButton.hidden = terminal || !state.requestId;
  cancelActionButton.disabled = Boolean(state.cancelRequested);
  cancelActionButton.textContent = state.cancelRequested ? "Stop requested…" : "Stop action";
  downloadActionLogButton.hidden = !state.requestId;
  updateRunnerLink.hidden = !state.runnerUpdateRequired;
  updateRunnerNote.hidden = !state.runnerUpdateRequired;
  cancelNote.hidden = terminal || !state.requestId;
  const fallbackSummary = state.state === "EXPIRED"
    ? "The runner did not claim this request before it expired. Check that CafeLauncher is installed on this PC, then retry."
    : state.state === "CANCELED"
      ? "The action was canceled before it started."
    : state.state === "INTERRUPTED"
      ? "The runner stopped without a final result. The action may have partly changed this PC; check it before retrying."
      : "The runner has not reported a final result yet.";
  document.querySelector("#result-summary").textContent = result?.summary || fallbackSummary;
  updateLiveActionTimer(state);

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

function updateLiveActionTimer(state) {
  if (!currentStepTiming) return;
  if (!state?.requestId || terminalStates.has(state.state)) {
    currentStepTiming.hidden = true;
    displayedProgressEventId = "";
    displayedProgressStartedAt = 0;
    return;
  }
  const events = Array.isArray(state.events) ? state.events : [];
  const currentEvent = [...events].reverse().find((event) => event?.step);
  if (!currentEvent?.step) {
    currentStepTiming.textContent = "Waiting for the runner’s first progress update…";
    currentStepTiming.hidden = false;
    return;
  }
  const eventId = currentEvent.eventId || `${currentEvent.createdUtc || ""}:${currentEvent.step.name || ""}:${currentEvent.step.status || ""}`;
  if (eventId !== displayedProgressEventId) {
    displayedProgressEventId = eventId;
    displayedProgressStartedAt = performance.now();
  }
  const status = currentEvent.step.status || "RUNNING";
  const name = currentEvent.step.name || "CafeLauncher action";
  if (status !== "RUNNING") {
    currentStepTiming.textContent = status === "PENDING"
      ? `Waiting for input: ${name} · PENDING`
      : `Last stage: ${name} · ${status} · Waiting for the runner result…`;
    currentStepTiming.hidden = false;
    return;
  }
  const elapsed = formatElapsedMs(performance.now() - displayedProgressStartedAt);
  currentStepTiming.textContent = `Current stage: ${name} · RUNNING · ${elapsed}`;
  currentStepTiming.hidden = false;
}

function downloadActionLog() {
  const state = latestActionState;
  if (!state) return;
  const events = Array.isArray(state.events) ? state.events : [];
  const createdAt = parseDiagnosticTime(state.createdUtc);
  const reportDownloadedAt = new Date();
  const observedAgeMs = createdAt === null ? null : reportDownloadedAt.getTime() - createdAt;
  const lines = [
    "CafeLauncher action report",
    `Action: ${actionLabels[state.action] || state.action || "Unknown"}`,
    `Profile: ${state.profile || "None"}`,
    `State: ${state.state || "Unknown"}`,
    `Created (UTC): ${state.createdUtc || "Unknown"}`,
    `Runner version: ${state.runnerVersion || "Unknown"}`,
    `Minimum runner version: ${state.minimumRunnerVersion || "Unknown"}`,
    `Runner update required: ${state.runnerUpdateRequired ? "Yes" : "No"}`,
    `Cancel requested: ${state.cancelRequestedUtc || (state.cancelRequested ? "Yes (time unavailable)" : "No")}`,
    `Report downloaded (local): ${reportDownloadedAt.toISOString()}`,
    `Request age at download (client clock estimate): ${formatElapsedMs(observedAgeMs)}`,
    "",
    "Timeline (timestamps and intervals are server recorded):",
  ];
  if (events.length === 0) lines.push("  No runner events were received.");
  let previousEventAt = null;
  for (const event of events) {
    const step = event.step;
    const eventAt = parseDiagnosticTime(event.createdUtc);
    const sincePrevious = previousEventAt === null || eventAt === null ? "unavailable" : formatElapsedMs(eventAt - previousEventAt);
    const sinceCreated = createdAt === null || eventAt === null ? "unavailable" : formatElapsedMs(eventAt - createdAt);
    lines.push(`  ${event.createdUtc || "Time unavailable"} · +${sincePrevious} since previous event · +${sinceCreated} since request · ${event.state || "EVENT"}${step ? ` · ${redactDiagnosticText(step.name)} · ${step.status} · ${redactDiagnosticText(step.detail)}` : ""}`);
    if (eventAt !== null) previousEventAt = eventAt;
  }
  if (events.length > 0) {
    const lastEventAt = parseDiagnosticTime(events.at(-1)?.createdUtc);
    lines.push(`  Server-observed time to last event: ${createdAt === null || lastEventAt === null ? "unavailable" : formatElapsedMs(lastEventAt - createdAt)}`);
    lines.push("  These intervals include runner work and network/server reporting delay; they are not CPU-only timings.");
  } else {
    lines.push("  With no events, the report cannot identify whether time was spent before the runner reported, during network communication, or in a local prompt.");
  }
  const result = state.result;
  if (result) {
    lines.push("", `Summary: ${redactDiagnosticText(result.summary || "No summary returned.")}`, "Result steps:");
    const steps = Array.isArray(result.steps) ? result.steps : [];
    if (steps.length === 0) lines.push("  No result steps returned.");
    for (const step of steps) lines.push(`  ${step.status || "UNKNOWN"} · ${redactDiagnosticText(step.name || "Unnamed step")} · ${redactDiagnosticText(step.detail)}`);
  }
  lines.push("", "Local paths, device identifiers, and raw result data are omitted from this report.");
  const blob = new Blob([lines.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `cafelauncher-action-${state.action || "report"}-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function parseDiagnosticTime(value) {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatElapsedMs(value) {
  if (!Number.isFinite(value) || value < 0) return "unavailable";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function redactDiagnosticText(value) {
  return String(value || "")
    .replace(/(?:\b[A-Za-z]:\\|\\\\)[^\r\n"'<>|?*]*/g, "[local path]")
    .replace(/\b\d{17}\b/g, "[account id]");
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
        setUnlockButtonVisible(true);
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
        setUnlockButtonVisible(true);
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

async function requestActionCancellation() {
  const state = latestActionState;
  if (!state?.requestId || terminalStates.has(state.state) || state.cancelRequested) return;
  if (!window.confirm("Ask CafeLauncher on this PC to stop? It may have already applied some settings. If the result is interrupted, check the PC before running another action.")) return;
  cancelActionButton.disabled = true;
  cancelActionButton.textContent = "Sending stop request…";
  try {
    const response = await readJson(await fetch(`/v1/actions/${encodeURIComponent(state.requestId)}/cancel`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Cafe-CSRF": csrfToken },
      body: "{}",
    }));
    if (response.cancelRequested) {
      renderResult({ ...state, state: response.state, cancelRequested: true, cancelRequestedUtc: state.cancelRequestedUtc || new Date().toISOString() });
      setServiceStatus(response.state === "CANCELED" ? "Canceled before the runner started." : "Stop requested. Waiting for the PC runner to respond…", "notice");
    } else {
      cancelActionButton.hidden = true;
      setServiceStatus(`The action already finished with state ${response.state}. Loading its result…`, "notice");
    }
  } catch (error) {
    cancelActionButton.disabled = false;
    cancelActionButton.textContent = "Stop action";
    setServiceStatus(`Could not send the stop request: ${error.message}`, "error");
  }
}

cancelActionButton.addEventListener("click", requestActionCancellation);
downloadActionLogButton.addEventListener("click", downloadActionLog);

unlockButton.addEventListener("click", () => {
  clearPendingRequest();
  activeRequest = false;
  setUnlockButtonVisible(false);
  setActionsDisabled(false);
  profileSelect.disabled = false;
  setServiceStatus("You confirmed this PC is ready for another action.", "ready");
});

async function startAction(action, profile = null) {
  if (activeRequest || !csrfToken) return;
  activeRequest = true;
  setActionsDisabled(true);
  profileSelect.disabled = true;
  // Apply Profile is a one-click action, matching the local CafeLauncher dashboard.
  // Its profile routine already applies settings and launches configured apps.
  if (action !== "detect-pc" && action !== "apply-profile") {
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
window.setInterval(() => updateLiveActionTimer(latestActionState), 1000);
initialize();
