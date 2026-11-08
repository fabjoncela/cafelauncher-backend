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
const graphicsHistory = document.querySelector("#graphics-history");

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

function renderHistory(payload) {
  graphicsHistory.replaceChildren();
  const revisions = Array.isArray(payload?.revisions) ? payload.revisions : [];
  if (!revisions.length) {
    showMessage(graphicsHistory, "No graphics history is available yet.");
    return;
  }
  for (const entry of revisions) {
    const details = document.createElement("details");
    details.className = "history-entry";
    const summary = document.createElement("summary");
    summary.textContent = `Revision ${entry.revision} · ${formatDate(entry.createdUtc)}`;
    const tableWrap = document.createElement("div");
    tableWrap.className = "table-wrap";
    const historicalGraphics = { ...(entry.graphics || {}) };
    const historicalSettings = historicalGraphics.Settings || {};
    delete historicalGraphics.Settings;
    delete historicalGraphics.SchemaVersion;
    delete historicalGraphics.ProfileName;
    renderTable(tableWrap, { ...historicalGraphics, ...historicalSettings });
    details.append(summary, tableWrap);
    graphicsHistory.append(details);
  }
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
  for (const target of [profileSettings, graphicsPolicy, graphicsSettings, cfgViewer, graphicsHistory]) {
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

    renderTable(profileSettings, bundle.profile, ["SchemaVersion", "ProfileName", "CfgDownloadUrl"]);
    const policy = Object.fromEntries(Object.entries(bundle.graphics).filter(([key]) => !["Settings", "SchemaVersion", "ProfileName"].includes(key)));
    renderTable(graphicsPolicy, policy);
    renderTable(graphicsSettings, bundle.graphics.Settings || {});
    renderCfg(bundle.cfg);
    revisionBanner.textContent = `${profileName} · backend revision ${bundle.revision}`;
    revisionBanner.hidden = false;

    try {
      const response = await fetch(`/v1/graphics/${encodeURIComponent(profileName)}/history?limit=20`, { cache: "no-store", headers: { Accept: "application/json" } });
      renderHistory(await readResponse(response, "Graphics history"));
    } catch (error) {
      showMessage(graphicsHistory, `Could not load graphics history: ${error.message}`);
    }
    loadStatus.dataset.state = "ok";
    loadStatus.textContent = `Loaded from the backend · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    loadStatus.dataset.state = "error";
    loadStatus.textContent = `Backend data could not be loaded: ${error.message}`;
    showMessage(profileSettings, "No profile data loaded.");
    showMessage(graphicsPolicy, "No graphics data loaded.");
    showMessage(graphicsSettings, "No graphics data loaded.");
    showMessage(cfgViewer, "No CFG data loaded.");
    showMessage(graphicsHistory, "No history data loaded.");
  } finally {
    refreshButton.disabled = false;
    profileSelect.disabled = false;
  }
}

refreshButton.addEventListener("click", loadProfile);
profileSelect.addEventListener("change", loadProfile);
loadProfile();
