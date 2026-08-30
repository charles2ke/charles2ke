"use strict";

const DATA_URL = "failures.json";
const REFRESH_MS = 60000;
const API_ROOT = "https://api.github.com";
const DISMISSED_KEY = "failures:dismissed";
const TOKEN_KEY = "failures:token";
const ALLOWED_LINK_PREFIX = "https://github.com/";

const elements = {
  repositories: document.getElementById("repositories"),
  summary: document.getElementById("summary"),
  refreshStatus: document.getElementById("refresh-status"),
  selectAll: document.getElementById("select-all"),
  selectionCount: document.getElementById("selection-count"),
  autoRefresh: document.getElementById("auto-refresh"),
  refreshNow: document.getElementById("refresh-now"),
  rerun: document.getElementById("action-rerun"),
  cancel: document.getElementById("action-cancel"),
  open: document.getElementById("action-open"),
  copy: document.getElementById("action-copy"),
  dismiss: document.getElementById("action-dismiss"),
  restore: document.getElementById("action-restore"),
  dismissedCount: document.getElementById("dismissed-count"),
  token: document.getElementById("token"),
  tokenSave: document.getElementById("token-save"),
  tokenClear: document.getElementById("token-clear"),
  log: document.getElementById("action-log"),
  repoTemplate: document.getElementById("repo-template"),
  failureTemplate: document.getElementById("failure-template"),
};

let snapshot = { repositories: [], generated_at: "" };
let selected = new Set();
let dismissed = readDismissed();
let refreshTimer = null;
let busy = false;

function readDismissed() {
  try {
    const raw = window.localStorage.getItem(DISMISSED_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.map(String) : []);
  } catch (error) {
    return new Set();
  }
}

function persistDismissed() {
  try {
    window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed]));
  } catch (error) {
    /* storage unavailable — dismissals stay in memory for this session */
  }
}

function getToken() {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || "";
  } catch (error) {
    return "";
  }
}

function setToken(value) {
  try {
    if (value) {
      window.sessionStorage.setItem(TOKEN_KEY, value);
    } else {
      window.sessionStorage.removeItem(TOKEN_KEY);
    }
  } catch (error) {
    /* storage unavailable — actions will prompt for a token again */
  }
}

function keyFor(repository, failure) {
  return `${repository.full_name}#${failure.id}`;
}

function safeUrl(value) {
  return typeof value === "string" && value.startsWith(ALLOWED_LINK_PREFIX) ? value : "";
}

function formatTimestamp(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function log(message, isError) {
  elements.log.textContent = message;
  elements.log.classList.toggle("error", Boolean(isError));
}

function visibleFailures() {
  const entries = [];
  for (const repository of snapshot.repositories || []) {
    for (const failure of repository.failures || []) {
      if (!dismissed.has(keyFor(repository, failure))) {
        entries.push({ repository, failure });
      }
    }
  }
  return entries;
}

function render() {
  const entries = visibleFailures();
  const grouped = new Map();

  for (const entry of entries) {
    const list = grouped.get(entry.repository.full_name) || [];
    list.push(entry.failure);
    grouped.set(entry.repository.full_name, list);
  }

  // Drop selections and dismissals for failures that are no longer unresolved.
  const live = new Set(entries.map((entry) => keyFor(entry.repository, entry.failure)));
  selected = new Set([...selected].filter((key) => live.has(key)));
  const prunedDismissed = new Set([...dismissed].filter((key) => live.has(key)));
  if (prunedDismissed.size !== dismissed.size) {
    dismissed = prunedDismissed;
    persistDismissed();
  }

  elements.repositories.textContent = "";

  if (entries.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No unresolved failures. Everything is green.";
    elements.repositories.append(empty);
  }

  for (const repository of snapshot.repositories || []) {
    const failures = grouped.get(repository.full_name);
    if (!failures || failures.length === 0) {
      continue;
    }
    elements.repositories.append(renderRepository(repository, failures));
  }

  elements.summary.textContent =
    entries.length === 0
      ? "0 unresolved failures"
      : `${entries.length} unresolved ${entries.length === 1 ? "failure" : "failures"} in ` +
        `${grouped.size} ${grouped.size === 1 ? "repository" : "repositories"}`;
  elements.refreshStatus.textContent = snapshot.generated_at
    ? `Snapshot ${formatTimestamp(snapshot.generated_at)}`
    : "";
  elements.dismissedCount.textContent = String(dismissed.size);
  elements.restore.disabled = dismissed.size === 0;

  updateSelectionState();
}

function renderRepository(repository, failures) {
  const node = elements.repoTemplate.content.firstElementChild.cloneNode(true);
  node.dataset.repo = repository.full_name;
  node.querySelector(".repo-name").textContent = repository.full_name;
  node.querySelector(".repo-count").textContent =
    `${failures.length} ${failures.length === 1 ? "failure" : "failures"}`;

  const link = node.querySelector(".repo-link");
  const repoUrl = safeUrl(repository.url);
  if (repoUrl) {
    link.href = repoUrl;
  } else {
    link.remove();
  }

  const repoSelect = node.querySelector(".repo-select");
  repoSelect.addEventListener("change", () => {
    for (const failure of failures) {
      const key = keyFor(repository, failure);
      if (repoSelect.checked) {
        selected.add(key);
      } else {
        selected.delete(key);
      }
    }
    syncCheckboxes();
  });

  const list = node.querySelector(".failures");
  for (const failure of failures) {
    list.append(renderFailure(repository, failure));
  }

  return node;
}

function renderFailure(repository, failure) {
  const node = elements.failureTemplate.content.firstElementChild.cloneNode(true);
  const key = keyFor(repository, failure);
  node.dataset.key = key;

  node.querySelector(".failure-workflow").textContent =
    `${failure.workflow}${failure.run_number ? ` #${failure.run_number}` : ""}`;
  node.querySelector(".conclusion").textContent = String(failure.conclusion || "failure").replace(
    /_/g,
    " ",
  );
  node.querySelector(".branch").textContent = failure.branch ? `branch: ${failure.branch}` : "";
  node.querySelector(".event").textContent = failure.event ? `on: ${failure.event}` : "";
  node.querySelector(".updated").textContent = formatTimestamp(failure.updated_at);

  const link = node.querySelector(".failure-link");
  const runUrl = safeUrl(failure.url);
  if (runUrl) {
    link.href = runUrl;
  } else {
    link.remove();
  }

  const checkbox = node.querySelector(".failure-select");
  checkbox.checked = selected.has(key);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) {
      selected.add(key);
    } else {
      selected.delete(key);
    }
    syncCheckboxes();
  });

  return node;
}

function syncCheckboxes() {
  for (const node of elements.repositories.querySelectorAll(".failure")) {
    node.querySelector(".failure-select").checked = selected.has(node.dataset.key);
  }

  for (const repoNode of elements.repositories.querySelectorAll(".repo")) {
    const boxes = [...repoNode.querySelectorAll(".failure-select")];
    const checked = boxes.filter((box) => box.checked).length;
    const repoSelect = repoNode.querySelector(".repo-select");
    repoSelect.checked = boxes.length > 0 && checked === boxes.length;
    repoSelect.indeterminate = checked > 0 && checked < boxes.length;
  }

  updateSelectionState();
}

function updateSelectionState() {
  const total = visibleFailures().length;
  const count = selected.size;

  elements.selectionCount.textContent = `${count} selected`;
  elements.selectAll.checked = total > 0 && count === total;
  elements.selectAll.indeterminate = count > 0 && count < total;

  const disabled = count === 0 || busy;
  for (const button of [
    elements.rerun,
    elements.cancel,
    elements.open,
    elements.copy,
    elements.dismiss,
  ]) {
    button.disabled = disabled;
  }
}

function selectedEntries() {
  return visibleFailures().filter((entry) => selected.has(keyFor(entry.repository, entry.failure)));
}

async function load() {
  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    snapshot = {
      generated_at: payload.generated_at || "",
      repositories: Array.isArray(payload.repositories) ? payload.repositories : [],
    };
    render();
  } catch (error) {
    elements.summary.textContent = "Could not load the failure snapshot";
    elements.refreshStatus.textContent = "";
    log(`Failed to load ${DATA_URL}: ${error.message}`, true);
  }
}

async function runBulkApiAction(path, label) {
  const token = getToken();
  if (!token) {
    log(`A GitHub token is required to ${label}. Add one under “Token for re-run and cancel actions”.`, true);
    return;
  }

  const entries = selectedEntries();
  busy = true;
  updateSelectionState();
  log(`${label}: 0/${entries.length}…`);

  let succeeded = 0;
  const failed = [];
  const headers = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  headers.Authorization = "Bearer " + token;

  for (const [index, entry] of entries.entries()) {
    const url = `${API_ROOT}/repos/${entry.repository.full_name}/actions/runs/${entry.failure.id}/${path}`;
    try {
      const response = await fetch(url, { method: "POST", headers });
      if (response.ok) {
        succeeded += 1;
      } else {
        failed.push(`${entry.repository.full_name} #${entry.failure.run_number}: HTTP ${response.status}`);
      }
    } catch (error) {
      failed.push(`${entry.repository.full_name} #${entry.failure.run_number}: ${error.message}`);
    }
    log(`${label}: ${index + 1}/${entries.length}…`);
  }

  busy = false;
  const summary = `${label}: ${succeeded} succeeded, ${failed.length} failed.`;
  log(failed.length ? `${summary}\n${failed.join("\n")}` : summary, failed.length > 0);
  await load();
}

function bulkOpen() {
  for (const entry of selectedEntries()) {
    const url = safeUrl(entry.failure.url);
    if (url) {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  }
}

async function bulkCopy() {
  const urls = selectedEntries()
    .map((entry) => safeUrl(entry.failure.url))
    .filter(Boolean);

  try {
    await navigator.clipboard.writeText(urls.join("\n"));
    log(`Copied ${urls.length} link${urls.length === 1 ? "" : "s"} to the clipboard.`);
  } catch (error) {
    log(`Could not copy links: ${error.message}`, true);
  }
}

function bulkDismiss() {
  const count = selected.size;
  for (const key of selected) {
    dismissed.add(key);
  }
  selected = new Set();
  persistDismissed();
  render();
  log(`Dismissed ${count} failure${count === 1 ? "" : "s"} in this browser.`);
}

function restoreDismissed() {
  dismissed = new Set();
  persistDismissed();
  render();
  log("Restored all dismissed failures.");
}

function scheduleRefresh() {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (elements.autoRefresh.checked) {
    refreshTimer = window.setInterval(() => {
      if (!busy) {
        load();
      }
    }, REFRESH_MS);
  }
}

elements.selectAll.addEventListener("change", () => {
  selected = elements.selectAll.checked
    ? new Set(visibleFailures().map((entry) => keyFor(entry.repository, entry.failure)))
    : new Set();
  syncCheckboxes();
});

elements.refreshNow.addEventListener("click", () => load());
elements.autoRefresh.addEventListener("change", scheduleRefresh);
elements.rerun.addEventListener("click", () =>
  runBulkApiAction("rerun-failed-jobs", "Re-run failed jobs"),
);
elements.cancel.addEventListener("click", () => runBulkApiAction("cancel", "Cancel runs"));
elements.open.addEventListener("click", bulkOpen);
elements.copy.addEventListener("click", bulkCopy);
elements.dismiss.addEventListener("click", bulkDismiss);
elements.restore.addEventListener("click", restoreDismissed);

elements.tokenSave.addEventListener("click", () => {
  setToken(elements.token.value.trim());
  elements.token.value = "";
  log(getToken() ? "Token saved for this tab." : "Token cleared.");
});

elements.tokenClear.addEventListener("click", () => {
  setToken("");
  elements.token.value = "";
  log("Token cleared.");
});

load();
scheduleRefresh();
