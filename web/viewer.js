"use strict";

const profileSelect = document.querySelector("#profile-select");
const refreshButton = document.querySelector("#refresh-button");
const loadStatus = document.querySelector("#load-status");
const revisionBanner = document.querySelector("#revision-banner");
const profileSettings = document.querySelector("#profile-settings");
const graphicsPolicy = document.querySelector("#graphics-policy");
const graphicsSettings = document.querySelector("#graphics-settings");
const cfgTitle = document.querySelector("#cfg-title");
const cfgMeta = document.querySelector("#cfg-meta");
const cfgViewer = document.querySelector("#cfg-viewer");
const profileHistory = document.querySelector("#profile-history");
const loadHistoryButton = document.querySelector("#load-history-button");
const loadMoreHistoryButton = document.querySelector("#load-more-history");
let currentBundle = null;
let historyCursor = null;
let historyLoaded = false;

function showMessage(target, message) {
  target.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.className = "muted";
  paragraph.textContent = message;
  target.append(paragraph);
}

function readableName(value) {
  return String(value).replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/^./, (letter) => letter.toUpperCase());
}

function readableValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderTable(target, entries, omitted = []) {
  target.replaceChildren();
  const rows = Object.entries(entries || {}).filter(([key]) => !omitted.includes(key));
  if (!rows.length) {
    showMessage(target, "No settings available.");
    return;
  }
  const table = document.createElement("table");
  table.className = "settings-table";
  const body = document.createElement("tbody");
  for (const [key, value] of rows) {
    const row = document.createElement("tr");
    const label = document.createElement("th");
    label.scope = "row";
    label.textContent = readableName(key);
    const cell = document.createElement("td");
    cell.textContent = readableValue(value);
    row.append(label, cell);
    body.append(row);
  }
  table.append(body);
  target.append(table);
}

function appendHighlightedCode(code, line) {
  if (/^\s*\/\//.test(line)) {
    const comment = document.createElement("span");
    comment.className = "cfg-comment";
    comment.textContent = line;
    code.append(comment);
    return;
  }
  const directive = line.match(/^(\s*)(\S+)(.*)$/);
  if (!directive) {
    code.textContent = line;
    return;
  }
  code.append(document.createTextNode(directive[1]));
  const command = document.createElement("span");
  command.className = "cfg-command";
  command.textContent = directive[2];
  code.append(command);
  const rest = directive[3];
  const tokens = /("(?:\\.|[^"\\])*")|(\s+)|([^\s"]+)/g;
  let match;
  while ((match = tokens.exec(rest)) !== null) {
    if (match[1]) {
      const value = document.createElement("span");
      value.className = "cfg-string";
      value.textContent = match[1];
      code.append(value);
    } else if (match[2]) {
      code.append(document.createTextNode(match[2]));
    } else {
      const value = document.createElement("span");
      value.className = "cfg-value";
      value.textContent = match[3];
      code.append(value);
    }
  }
}

function renderCfg(content) {
  cfgViewer.replaceChildren();
  const lines = String(content).replace(/\r\n/g, "\n").split("\n");
  const fragment = document.createDocumentFragment();
  lines.forEach((line, index) => {
    const row = document.createElement("div");
    row.className = "cfg-line";
    const number = document.createElement("span");
    number.className = "line-number";
    number.textContent = String(index + 1);
    const code = document.createElement("code");
    appendHighlightedCode(code, line);
    row.append(number, code);
    fragment.append(row);
  });
  cfgViewer.append(fragment);
  cfgMeta.textContent = `${lines.length} lines · ${new TextEncoder().encode(content).length.toLocaleString()} bytes`;
}

function formatDate(value) {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function appendHistoryEntries(payload, profileName) {
  const revisions = Array.isArray(payload?.revisions) ? payload.revisions : [];
  if (!historyLoaded) profileHistory.replaceChildren();
  if (!revisions.length && !historyLoaded) {
    showMessage(profileHistory, "No saved profile revisions are available yet.");
  }
  for (const entry of revisions) {
    const details = document.createElement("details");
    details.className = "history-entry";
    const summary = document.createElement("summary");
    summary.textContent = `Revision ${entry.revision} · ${formatDate(entry.createdUtc)}`;
    const content = document.createElement("div");
    content.className = "history-snapshot";
    const loading = document.createElement("p");
    loading.className = "muted";
    loading.textContent = "Open this revision to load its saved values.";
    content.append(loading);
    details.append(summary, content);
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded || details.dataset.loading) return;
      details.dataset.loading = "true";
      showMessage(content, "Loading this saved revision…");
      try {
        const response = await fetch(`/v1/profiles/${encodeURIComponent(profileName)}/history/${entry.revision}`, { cache: "no-store", headers: { Accept: "application/json" } });
        const snapshot = await readResponse(response, "Profile history");
        if (profileSelect.value !== profileName || !currentBundle || !profileHistory.contains(details)) return;
        renderHistoricalBundle(content, snapshot);
        details.dataset.loaded = "true";
      } catch (error) {
        showMessage(content, `Could not load revision: ${error.message}`);
      } finally {
        delete details.dataset.loading;
      }
    });
    profileHistory.append(details);
  }
  historyLoaded = true;
  historyCursor = payload?.hasMore ? payload.nextBeforeRevision : null;
  loadMoreHistoryButton.hidden = !historyCursor;
  loadHistoryButton.textContent = "Reload history";
}

function renderHistoricalBundle(target, snapshot) {
  target.replaceChildren();
  const currentRevision = currentBundle.revision;
  const heading = document.createElement("p");
  heading.className = "small-meta snapshot-meta";
  heading.textContent = `Saved ${formatDate(snapshot.createdUtc)} · revision ${snapshot.revision} · current revision ${currentRevision}`;
  target.append(heading);
  const currentValues = {
    ...Object.fromEntries(Object.entries(currentBundle.profile || {}).map(([key, value]) => [`Profile · ${readableName(key)}`, value])),
    ...Object.fromEntries(Object.entries(currentBundle.graphics || {}).filter(([key]) => !["Settings", "SchemaVersion", "ProfileName"].includes(key)).map(([key, value]) => [`Graphics · ${readableName(key)}`, value])),
    ...Object.fromEntries(Object.entries(currentBundle.graphics?.Settings || {}).map(([key, value]) => [`Graphics · ${readableName(key)}`, value])),
  };
  const historicalValues = {
    ...Object.fromEntries(Object.entries(snapshot.profile || {}).map(([key, value]) => [`Profile · ${readableName(key)}`, value])),
    ...Object.fromEntries(Object.entries(snapshot.graphics || {}).filter(([key]) => !["Settings", "SchemaVersion", "ProfileName"].includes(key)).map(([key, value]) => [`Graphics · ${readableName(key)}`, value])),
    ...Object.fromEntries(Object.entries(snapshot.graphics?.Settings || {}).map(([key, value]) => [`Graphics · ${readableName(key)}`, value])),
  };
  const changedKeys = [...new Set([...Object.keys(currentValues), ...Object.keys(historicalValues)])]
    .filter((key) => JSON.stringify(currentValues[key]) !== JSON.stringify(historicalValues[key]));
  const compareHeading = document.createElement("h3");
  compareHeading.className = "snapshot-heading";
  compareHeading.textContent = "Changes vs current";
  target.append(compareHeading);
  if (changedKeys.length) {
    const table = document.createElement("table");
    table.className = "settings-table comparison-table";
    const body = document.createElement("tbody");
    for (const key of changedKeys) {
      const row = document.createElement("tr");
      const label = document.createElement("th");
      label.scope = "row";
      label.textContent = key;
      const current = document.createElement("td");
      current.textContent = `Current: ${readableValue(currentValues[key])}`;
      const saved = document.createElement("td");
      saved.textContent = `Saved: ${readableValue(historicalValues[key])}`;
      row.append(label, current, saved);
      body.append(row);
    }
    table.append(body);
    target.append(table);
  } else {
    const unchanged = document.createElement("p");
    unchanged.className = "muted";
    unchanged.textContent = "Profile and graphics settings match the current revision.";
    target.append(unchanged);
  }
  const cfgChanged = String(snapshot.cfg ?? "") !== String(currentBundle.cfg ?? "");
  const cfgStatus = document.createElement("p");
  cfgStatus.className = cfgChanged ? "history-change-status" : "muted";
  cfgStatus.textContent = cfgChanged ? "The saved CFG differs from the current CFG; the saved text is shown below." : "The CFG matches the current revision.";
  target.append(cfgStatus);
  const sections = [
    ["Profile settings", snapshot.profile || {}, ["SchemaVersion", "ProfileName", "CfgDownloadUrl"]],
    ["Graphics policy", Object.fromEntries(Object.entries(snapshot.graphics || {}).filter(([key]) => !["Settings", "SchemaVersion", "ProfileName"].includes(key))), []],
    ["Graphics values", snapshot.graphics?.Settings || {}, []],
  ];
  for (const [title, values, omitted] of sections) {
    const heading = document.createElement("h3");
    heading.className = "snapshot-heading";
    heading.textContent = title;
    const table = document.createElement("div");
    table.className = "table-wrap";
    renderTable(table, values, omitted);
    target.append(heading, table);
  }
  const cfgHeading = document.createElement("h3");
  cfgHeading.className = "snapshot-heading";
  cfgHeading.textContent = "Saved CFG";
  const cfg = document.createElement("pre");
  cfg.className = "history-cfg";
  cfg.textContent = String(snapshot.cfg ?? "");
  target.append(cfgHeading, cfg);
}

async function readResponse(response, label, asText = false) {
  if (!response.ok) throw new Error(`${label} returned HTTP ${response.status}.`);
  return asText ? response.text() : response.json();
}

async function loadProfile() {
  const profileName = profileSelect.value;
  const option = profileSelect.selectedOptions[0];
  const cfgName = option?.dataset.cfg;
  if (!profileName || !cfgName) return;
  refreshButton.disabled = true;
  profileSelect.disabled = true;
  loadStatus.dataset.state = "loading";
  loadStatus.textContent = `Loading ${profileName} from the backend…`;
  revisionBanner.hidden = true;
  currentBundle = null;
  historyLoaded = false;
  historyCursor = null;
  loadMoreHistoryButton.hidden = true;
  loadHistoryButton.textContent = "Load history";
  for (const target of [profileSettings, graphicsPolicy, graphicsSettings, cfgViewer, profileHistory]) {
    showMessage(target, "Loading…");
  }
  cfgTitle.textContent = `${cfgName} CFG`;
  cfgMeta.textContent = "";

  try {
    let bundle;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const [profileResponse, graphicsResponse, cfgResponse] = await Promise.all([
        fetch(`/v1/profiles/${encodeURIComponent(profileName)}`, { cache: "no-store", headers: { Accept: "application/json" } }),
        fetch(`/v1/graphics/${encodeURIComponent(profileName)}`, { cache: "no-store", headers: { Accept: "application/json" } }),
        fetch(`/v1/cfg/${encodeURIComponent(cfgName)}`, { cache: "no-store", headers: { Accept: "text/plain" } }),
      ]);
      const [profile, graphics, cfg] = await Promise.all([
        readResponse(profileResponse, "Profile"),
        readResponse(graphicsResponse, "Graphics"),
        readResponse(cfgResponse, "CFG", true),
      ]);
      const revisions = [profileResponse, graphicsResponse, cfgResponse].map((response) => response.headers.get("X-Profile-Revision"));
      if (revisions[0] && revisions.every((revision) => revision === revisions[0])) {
        bundle = { profile, graphics, cfg, revision: revisions[0] };
        break;
      }
    }
    if (!bundle) throw new Error("The profile changed while it was loading. Press Refresh to try again.");
    currentBundle = bundle;

    renderTable(profileSettings, bundle.profile, ["SchemaVersion", "ProfileName", "CfgDownloadUrl"]);
    const policy = Object.fromEntries(Object.entries(bundle.graphics).filter(([key]) => !["Settings", "SchemaVersion", "ProfileName"].includes(key)));
    renderTable(graphicsPolicy, policy);
    renderTable(graphicsSettings, bundle.graphics.Settings || {});
    renderCfg(bundle.cfg);
    revisionBanner.textContent = `${profileName} · backend revision ${bundle.revision}`;
    revisionBanner.hidden = false;

    showMessage(profileHistory, "Choose “Load history” to view saved revisions.");
    loadStatus.dataset.state = "ok";
    loadStatus.textContent = `Loaded from the backend · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    loadStatus.dataset.state = "error";
    loadStatus.textContent = `Backend data could not be loaded: ${error.message}`;
    showMessage(profileSettings, "No profile data loaded.");
    showMessage(graphicsPolicy, "No graphics data loaded.");
    showMessage(graphicsSettings, "No graphics data loaded.");
    showMessage(cfgViewer, "No CFG data loaded.");
    showMessage(profileHistory, "Load the current profile before viewing history.");
  } finally {
    refreshButton.disabled = false;
    profileSelect.disabled = false;
  }
}

async function loadHistory(beforeRevision = null) {
  if (!currentBundle || loadHistoryButton.disabled) return;
  const profileName = profileSelect.value;
  loadHistoryButton.disabled = true;
  loadMoreHistoryButton.disabled = true;
  if (!beforeRevision) {
    historyLoaded = false;
    historyCursor = null;
  }
  try {
    const query = new URLSearchParams({ limit: "20" });
    if (beforeRevision) query.set("beforeRevision", String(beforeRevision));
    const response = await fetch(`/v1/profiles/${encodeURIComponent(profileName)}/history?${query}`, { cache: "no-store", headers: { Accept: "application/json" } });
    const payload = await readResponse(response, "Profile history");
    if (profileSelect.value !== profileName || !currentBundle) return;
    appendHistoryEntries(payload, profileName);
  } catch (error) {
    if (!historyLoaded) showMessage(profileHistory, `Could not load profile history: ${error.message}`);
    else {
      const message = document.createElement("p");
      message.className = "muted";
      message.textContent = `Could not load older revisions: ${error.message}`;
      profileHistory.append(message);
    }
  } finally {
    loadHistoryButton.disabled = false;
    loadMoreHistoryButton.disabled = false;
  }
}

refreshButton.addEventListener("click", loadProfile);
profileSelect.addEventListener("change", loadProfile);
loadHistoryButton.addEventListener("click", () => loadHistory());
loadMoreHistoryButton.addEventListener("click", () => loadHistory(historyCursor));
loadProfile();
