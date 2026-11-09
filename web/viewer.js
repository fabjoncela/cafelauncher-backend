"use strict";

const profileSelect = document.querySelector("#profile-select");
const refreshButton = document.querySelector("#refresh-button");
const loadStatus = document.querySelector("#load-status");
const revisionBanner = document.querySelector("#revision-banner");
const cfgTitle = document.querySelector("#cfg-title");
const cfgMeta = document.querySelector("#cfg-meta");
const cfgViewer = document.querySelector("#cfg-viewer");
const currentCfgActions = document.querySelector("#current-cfg-actions");
const profileHistory = document.querySelector("#profile-history");
const loadHistoryButton = document.querySelector("#load-history-button");
const loadMoreHistoryButton = document.querySelector("#load-more-history");
const contentGrid = document.querySelector("#content-grid");
const cfgPanel = document.querySelector("#cfg-panel");
let currentBundle = null;
let historyCursor = null;
let historyLoaded = false;
let viewerSchema = null;

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

function fieldMetadata(key, section = null) {
  return {
    ...(viewerSchema?.fields?.[key] || { label: readableName(key), group: "Other" }),
    ...(section?.fieldOverrides?.[key] || {}),
  };
}

function fieldLabel(key, section = null) {
  return fieldMetadata(key, section).label || readableName(key);
}

function readableValue(value, metadata = null) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  const raw = String(value);
  const label = metadata?.values?.[raw];
  if (label) return label;
  return metadata?.unit ? `${raw}${metadata.unit}` : raw;
}

function renderTable(target, entries, omitted = [], section = null) {
  target.replaceChildren();
  const rows = Object.entries(entries || {}).filter(([key]) => !omitted.includes(key));
  if (!rows.length) {
    showMessage(target, "No settings available.");
    return;
  }
  const grouped = new Map();
  for (const [key, value] of rows) {
    const metadata = fieldMetadata(key, section);
    const group = metadata.group || "Other";
    if (!grouped.has(group)) grouped.set(group, []);
    grouped.get(group).push([key, value, metadata]);
  }
  const groupOrder = viewerSchema?.groups || [];
  const orderedGroups = [...grouped.keys()].sort((left, right) => {
    const leftIndex = groupOrder.indexOf(left);
    const rightIndex = groupOrder.indexOf(right);
    return (leftIndex < 0 ? groupOrder.length : leftIndex) - (rightIndex < 0 ? groupOrder.length : rightIndex) || left.localeCompare(right);
  });
  for (const group of orderedGroups) {
    if (orderedGroups.length > 1) {
      const heading = document.createElement("h3");
      heading.className = "field-group-heading";
      heading.textContent = group;
      target.append(heading);
    }
    const table = document.createElement("table");
    table.className = "settings-table";
    const body = document.createElement("tbody");
    for (const [key, value, metadata] of grouped.get(group)) {
    const row = document.createElement("tr");
    const label = document.createElement("th");
    label.scope = "row";
    label.textContent = fieldLabel(key);
    const cell = document.createElement("td");
    cell.textContent = readableValue(value, metadata);
    row.append(label, cell);
    body.append(row);
    }
    table.append(body);
    target.append(table);
  }
}

function valueAtPath(source, path) {
  return String(path || "").split(".").filter(Boolean).reduce((value, key) => value?.[key], source);
}

function renderSettingsSections(bundle, placeholderMessage = null) {
  for (const oldPanel of contentGrid.querySelectorAll(".schema-panel")) oldPanel.remove();
  for (const section of viewerSchema.sections || []) {
    const panel = document.createElement("section");
    panel.className = "panel schema-panel";
    const heading = document.createElement("div");
    heading.className = "panel-heading";
    const titleGroup = document.createElement("div");
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = section.eyebrow || "SETTINGS";
    const title = document.createElement("h2");
    title.textContent = section.title || "Settings";
    titleGroup.append(eyebrow, title);
    heading.append(titleGroup);
    const table = document.createElement("div");
    table.className = "table-wrap";
    if (placeholderMessage) showMessage(table, placeholderMessage);
    else renderTable(table, valueAtPath(bundle, section.source), section.omit || [], section);
    panel.append(heading, table);
    contentGrid.insertBefore(panel, cfgPanel);
  }
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

function safeFilename(value) {
  return String(value || "cafelauncher.cfg").replace(/[\\/:*?"<>|]/g, "_");
}

function addCfgActions(target, content, filename, copyLabel = "Copy CFG", downloadLabel = "Download CFG") {
  target.replaceChildren();
  const buttons = document.createElement("div");
  buttons.className = "cfg-action-buttons";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = copyLabel;
  copy.addEventListener("click", async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(String(content));
      } else {
        const input = document.createElement("textarea");
        input.value = String(content);
        input.setAttribute("readonly", "");
        input.className = "clipboard-fallback";
        document.body.append(input);
        input.select();
        const copied = document.execCommand("copy");
        input.remove();
        if (!copied) throw new Error("Clipboard access is unavailable.");
      }
      status.textContent = "Copied to clipboard.";
    } catch (error) {
      status.textContent = `Copy failed: ${error.message}`;
    }
  });
  const download = document.createElement("button");
  download.type = "button";
  download.textContent = downloadLabel;
  download.addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([String(content)], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = safeFilename(filename);
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = `Downloaded ${link.download}.`;
  });
  buttons.append(copy, download);
  const status = document.createElement("span");
  status.className = "cfg-action-status muted";
  status.setAttribute("role", "status");
  target.append(buttons, status);
}

function buildLineDiff(currentText, savedText) {
  const currentLines = String(currentText).replace(/\r\n/g, "\n").split("\n");
  const savedLines = String(savedText).replace(/\r\n/g, "\n").split("\n");
  const rows = currentLines.length + 1;
  const columns = savedLines.length + 1;
  const cellCount = rows * columns;
  if (currentLines.length + savedLines.length > 5000 || cellCount > 250000) return null;

  const lcs = new Uint32Array(cellCount);
  for (let current = currentLines.length - 1; current >= 0; current -= 1) {
    for (let saved = savedLines.length - 1; saved >= 0; saved -= 1) {
      const index = current * columns + saved;
      lcs[index] = currentLines[current] === savedLines[saved]
        ? 1 + lcs[(current + 1) * columns + saved + 1]
        : Math.max(lcs[(current + 1) * columns + saved], lcs[index + 1]);
    }
  }

  const operations = [];
  let current = 0;
  let saved = 0;
  while (current < currentLines.length || saved < savedLines.length) {
    if (current < currentLines.length && saved < savedLines.length && currentLines[current] === savedLines[saved]) {
      operations.push({ kind: "same", line: currentLines[current] });
      current += 1;
      saved += 1;
    } else if (current < currentLines.length && (
      saved >= savedLines.length || lcs[(current + 1) * columns + saved] >= lcs[current * columns + saved + 1]
    )) {
      operations.push({ kind: "removed", line: currentLines[current] });
      current += 1;
    } else {
      operations.push({ kind: "added", line: savedLines[saved] });
      saved += 1;
    }
  }
  return operations;
}

function renderCfgDiff(target, currentText, savedText) {
  const operations = buildLineDiff(currentText, savedText);
  if (!operations) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "This CFG is too large for an inline diff. Download it to compare the full file.";
    target.append(note);
    return;
  }
  const diff = document.createElement("div");
  diff.className = "cfg-diff";
  diff.setAttribute("aria-label", "Saved CFG compared with current CFG");
  for (const operation of operations) {
    const row = document.createElement("div");
    row.className = `cfg-diff-line ${operation.kind}`;
    const marker = document.createElement("span");
    marker.className = "cfg-diff-marker";
    marker.textContent = operation.kind === "added" ? "+" : operation.kind === "removed" ? "−" : " ";
    const line = document.createElement("code");
    line.textContent = operation.line || " ";
    row.append(marker, line);
    diff.append(row);
  }
  target.append(diff);
}

function renderCfg(content, filename) {
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
  addCfgActions(currentCfgActions, content, filename);
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
  const flattenBundle = (bundle) => {
    const values = new Map();
    for (const section of viewerSchema.sections || []) {
      const sectionValues = valueAtPath(bundle, section.source) || {};
      for (const [key, value] of Object.entries(sectionValues)) {
        if ((section.omit || []).includes(key)) continue;
        const id = `${section.id}:${key}`;
        values.set(id, { value, label: `${section.title || "Settings"} · ${fieldLabel(key, section)}`, metadata: fieldMetadata(key, section) });
      }
    }
    return values;
  };
  const currentValues = flattenBundle(currentBundle);
  const historicalValues = flattenBundle(snapshot);
  const changedKeys = [...new Set([...currentValues.keys(), ...historicalValues.keys()])]
    .filter((key) => JSON.stringify(currentValues.get(key)?.value) !== JSON.stringify(historicalValues.get(key)?.value));
  const compareHeading = document.createElement("h3");
  compareHeading.className = "snapshot-heading";
  compareHeading.textContent = "Changes vs current";
  target.append(compareHeading);
  if (changedKeys.length) {
    const table = document.createElement("table");
    table.className = "settings-table comparison-table";
    const body = document.createElement("tbody");
    for (const key of changedKeys) {
      const currentValue = currentValues.get(key);
      const historicalValue = historicalValues.get(key);
      const row = document.createElement("tr");
      const label = document.createElement("th");
      label.scope = "row";
      label.textContent = currentValue?.label || historicalValue?.label || key;
      const current = document.createElement("td");
      current.textContent = `Current: ${readableValue(currentValue?.value, currentValue?.metadata)}`;
      const saved = document.createElement("td");
      saved.textContent = `Saved: ${readableValue(historicalValue?.value, historicalValue?.metadata)}`;
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
  cfgStatus.textContent = cfgChanged ? "The saved CFG differs from the current CFG. Removed lines are red; added lines are green." : "The CFG matches the current revision.";
  target.append(cfgStatus);
  const cfgHeading = document.createElement("h3");
  cfgHeading.className = "snapshot-heading";
  cfgHeading.textContent = "Saved CFG";
  target.append(cfgHeading);
  const cfgContent = String(snapshot.cfg ?? "");
  const profile = (viewerSchema.profiles || []).find((item) => item.name === profileSelect.value);
  const cfgActions = document.createElement("div");
  cfgActions.className = "cfg-actions";
  addCfgActions(cfgActions, cfgContent, profile?.cfg || "saved.cfg", "Copy saved CFG", "Download saved CFG");
  target.append(cfgActions);
  if (cfgChanged) renderCfgDiff(target, currentBundle.cfg ?? "", cfgContent);
  for (const section of viewerSchema.sections || []) {
    const heading = document.createElement("h3");
    heading.className = "snapshot-heading";
    heading.textContent = section.title || "Settings";
    const table = document.createElement("div");
    table.className = "table-wrap";
    renderTable(table, valueAtPath(snapshot, section.source), section.omit || [], section);
    target.append(heading, table);
  }
  const savedDetails = document.createElement("details");
  savedDetails.className = "saved-cfg-details";
  const savedSummary = document.createElement("summary");
  savedSummary.textContent = "Show complete saved CFG";
  const cfg = document.createElement("pre");
  cfg.className = "history-cfg";
  cfg.textContent = cfgContent;
  savedDetails.append(savedSummary, cfg);
  target.append(savedDetails);
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
  for (const target of [cfgViewer, profileHistory]) {
    showMessage(target, "Loading…");
  }
  renderSettingsSections(null, "Loading…");
  cfgTitle.textContent = `${cfgName} CFG`;
  cfgMeta.textContent = "";
  currentCfgActions.replaceChildren();

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

    renderSettingsSections(bundle);
    renderCfg(bundle.cfg, cfgName);
    revisionBanner.textContent = `${profileName} · backend revision ${bundle.revision}`;
    revisionBanner.hidden = false;

    showMessage(profileHistory, "Choose “Load history” to view saved revisions.");
    loadStatus.dataset.state = "ok";
    loadStatus.textContent = `Loaded from the backend · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    loadStatus.dataset.state = "error";
    loadStatus.textContent = `Backend data could not be loaded: ${error.message}`;
    showMessage(cfgViewer, "No CFG data loaded.");
    currentCfgActions.replaceChildren();
    showMessage(profileHistory, "Load the current profile before viewing history.");
    renderSettingsSections(null, "No settings loaded.");
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

loadHistoryButton.addEventListener("click", () => loadHistory());
loadMoreHistoryButton.addEventListener("click", () => loadHistory(historyCursor));

async function initializeViewer() {
  refreshButton.disabled = true;
  profileSelect.disabled = true;
  loadStatus.dataset.state = "loading";
  loadStatus.textContent = "Loading viewer settings…";
  try {
    const response = await fetch("/v1/viewer-schema", { cache: "no-store", headers: { Accept: "application/json" } });
    viewerSchema = await readResponse(response, "Viewer schema");
    profileSelect.replaceChildren();
    for (const profile of viewerSchema.profiles || []) {
      const option = document.createElement("option");
      option.value = profile.name;
      option.dataset.cfg = profile.cfg;
      option.textContent = profile.name;
      profileSelect.append(option);
    }
    if (!profileSelect.options.length) throw new Error("The viewer schema contains no profiles.");
    await loadProfile();
  } catch (error) {
    loadStatus.dataset.state = "error";
    loadStatus.textContent = `Viewer settings could not be loaded: ${error.message}`;
  } finally {
    refreshButton.disabled = false;
    profileSelect.disabled = false;
  }
}

refreshButton.addEventListener("click", () => viewerSchema ? loadProfile() : initializeViewer());
profileSelect.addEventListener("change", loadProfile);
initializeViewer();
