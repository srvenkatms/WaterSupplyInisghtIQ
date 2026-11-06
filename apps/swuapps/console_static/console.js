"use strict";
const $ = (id) => document.getElementById(id);
let saved = null;
let dirty = false;
let busy = false;
const csrf = document.querySelector('meta[name="swu-csrf"]').content;
const hostedInAzure = document.body.dataset.hosting === "azure";
const datasetLocation = hostedInAzure ? "Azure-hosted" : "local";

function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
  $("message").hidden = false;
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      redirect: "manual",
      headers: {"Content-Type": "application/json", "X-SWU-CSRF": csrf, ...options.headers},
    });
  } catch (error) {
    if (!["TypeError", "AbortError", "TimeoutError"].includes(error.name)) throw error;
    console.error("Console request failed before a response was received", error);
    throw new Error("The console could not be reached. Check your connection and reload; the request outcome is unconfirmed.");
  }
  if (hostedInAzure && (response.type === "opaqueredirect" || response.status === 401)) {
    const prompt = $("session-expired");
    if (prompt) prompt.hidden = false;
    const error = new Error("Sign-in expired. Use Sign in again to reconnect.");
    error.authenticationRequired = true;
    throw error;
  }
  if (!response.headers.get("Content-Type")?.includes("application/json")) {
    throw new Error("The session or console is unavailable. Reload to sign in again.");
  }
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error || `Request failed (${response.status})`);
    error.details = value;
    throw error;
  }
  return value;
}

function formValue() {
  return {
    mode: document.querySelector('input[name="mode"]:checked').value,
    utilityId: $("utility").value,
    confirmRuns: $("confirm-runs").checked,
    teams: {enabled: $("teams-enabled").checked, teamId: $("team-id").value, channelId: $("channel-id").value},
  };
}

function updateDirty() {
  if (!saved) return;
  const value = formValue();
  dirty = value.mode !== saved.settings.mode || value.utilityId !== saved.settings.utilityId ||
    value.confirmRuns !== saved.settings.confirmRuns ||
    Object.keys(value.teams).some((key) => value.teams[key] !== saved.settings.teams[key]);
  $("save-state").textContent = dirty ? "Unsaved changes" : `Saved revision ${saved.revision}`;
  $("unsaved-label").textContent = dirty ? "Your changes have not been saved." :
    `Settings are stored ${hostedInAzure ? "in Azure" : "locally"}. Running lab resources are unchanged.`;
  $("save").disabled = !dirty || busy;
  $("discard").disabled = !dirty || busy;
  $("team-id").disabled = !$("teams-enabled").checked;
  $("channel-id").disabled = !$("teams-enabled").checked;
  $("teams-export").setAttribute("aria-disabled", String(dirty));
}

function renderSettings(document) {
  saved = document;
  const config = saved.settings;
  documentSelectorMode(config.mode).checked = true;
  $("utility").value = config.utilityId;
  $("confirm-runs").checked = config.confirmRuns;
  $("teams-enabled").checked = config.teams.enabled;
  $("team-id").value = config.teams.teamId;
  $("channel-id").value = config.teams.channelId;
  $("overview-mode").textContent = config.mode === "on-demand" ? "On demand" : "Acceptance";
  $("overview-utility").textContent = `Default context: Utility ${config.utilityId.slice(-1).toUpperCase()}`;
  $("overview-teams").textContent = config.teams.enabled ? "Configured, not connected" : "Disabled";
  updateDirty();
}

function documentSelectorMode(mode) {
  return document.querySelector(`input[name="mode"][value="${mode}"]`);
}

async function renderScenarios() {
  const data = await api("/api/scenarios");
  $("scenario-grid").replaceChildren();
  for (const scenario of data.scenarios) {
    const card = document.createElement("section");
    card.className = "card";
    const code = document.createElement("span");
    code.className = "eyebrow";
    code.textContent = scenario.id;
    const title = document.createElement("h2");
    title.textContent = scenario.name;
    const description = document.createElement("p");
    const rules = [...scenario.primary, ...scenario.eventual];
    description.textContent = rules.length ? `Expected live rules: ${rules.join(", ")}. No live result is claimed here.` : "Control scenario: no High alert is expected.";
    const button = document.createElement("button");
    button.className = "button secondary";
    button.textContent = !scenario.applicable ? "Not applicable" : saved.settings.mode === "acceptance" ? "Use live acceptance harness" : `Generate ${datasetLocation} dataset`;
    button.disabled = !scenario.applicable || saved.settings.mode !== "on-demand" || busy;
    button.addEventListener("click", () => generateDataset(scenario.id));
    card.append(code, title, description, button);
    $("scenario-grid").append(card);
  }
}

async function generateDataset(scenario) {
  if (busy) return;
  if (saved.settings.confirmRuns && !window.confirm(`Generate the ${datasetLocation} ${scenario} dataset? No PLC, HMI, Sentinel or Teams actions will be executed.`)) return;
  busy = true;
  try {
    await renderScenarios();
    const result = await api("/api/datasets", {method: "POST", body: JSON.stringify({scenario, confirmed: true})});
    $("result-content").textContent = JSON.stringify(result, null, 2);
    $("dataset-result").hidden = false;
    message(`${hostedInAzure ? "Azure-hosted" : "Local"} dataset generated. This is not a live attack or acceptance result.`);
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = false;
    await renderScenarios().catch((error) => message(error.message, true));
    updateDirty();
  }
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (!saved) return;
    if (dirty && !window.confirm("Discard your unsaved Admin changes before switching views?")) return;
    if (dirty) renderSettings(saved);
    const view = button.dataset.view;
    document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${view}`; });
    document.querySelectorAll(".nav-button").forEach((nav) => {
      nav.classList.toggle("active", nav.dataset.view === view);
      if (nav.dataset.view === view) nav.setAttribute("aria-current", "page");
      else nav.removeAttribute("aria-current");
    });
    $("breadcrumb").textContent = {admin: "Administration", scenarios: "Scenarios", overview: "Overview", live: "Live plant"}[view];
    window.scrollTo(0, 0);
    if (view === "scenarios") await renderScenarios().catch((error) => message(error.message, true));
  });
});

$("admin-form").addEventListener("input", updateDirty);
$("admin-form").addEventListener("change", updateDirty);
$("discard").addEventListener("click", () => renderSettings(saved));
$("teams-export").addEventListener("click", (event) => {
  if (dirty) { event.preventDefault(); message("Save or discard your changes before exporting.", true); }
});
$("admin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!saved || busy || !dirty) return;
  busy = true;
  updateDirty();
  try {
    const document = await api("/api/settings", {method: "PUT", body: JSON.stringify({revision: saved.revision, settings: formValue()})});
    renderSettings(document);
    message(`Configuration saved ${hostedInAzure ? "in Azure" : "locally"}. Running lab resources and Teams connections were not changed.`);
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = false;
    updateDirty();
  }
});

window.addEventListener("beforeunload", (event) => {
  if (dirty) { event.preventDefault(); event.returnValue = ""; }
});

(async () => {
  try {
    renderSettings(await api("/api/settings"));
    const liveView = document.querySelector('[data-view="live"]');
    if (liveView) liveView.click();
    const {readiness} = await api("/api/status");
    $("readiness-title").textContent = {
      "not-configured": "No Azure manifest", "not-started": "Warm-up not started",
      "warming-up": "Baseline collection in progress", "verification-required": "Live verification required",
    }[readiness.state];
    $("readiness-detail").textContent = readiness.message;
    $("eligible-at").textContent = readiness.eligibleAt ? `Earliest eligibility: ${new Date(readiness.eligibleAt).toUTCString()}` : "";
  } catch (error) {
    $("readiness-title").textContent = "Configuration unavailable";
    message(error.message, true);
  }
})();
