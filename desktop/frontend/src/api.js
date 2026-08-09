// One place that knows how to reach the local Python API.
//
// Resolution: the Tauri shell injects `window.__VETROMAR_API__` (the sidecar's
// chosen port) before the app loads. In a plain browser (`npm run dev`) fall
// back to VITE_API_BASE, else same-origin (works when FastAPI serves the SPA).
const API_BASE =
  (typeof window !== "undefined" && window.__VETROMAR_API__) ||
  import.meta.env.VITE_API_BASE ||
  "";

async function request(path, opts) {
  const res = await fetch(API_BASE + path, opts);
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = res.statusText;
    }
    // Structured (non-string) details would render as "[object Object]".
    if (detail && typeof detail !== "string") {
      detail = "Something went wrong. Please try again. (error VM-100)";
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

const jsonPost = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

// Query string from a params object, skipping empty/null values.
function qs(params = {}) {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== "" && v !== false
  );
  return entries.length ? "?" + new URLSearchParams(entries) : "";
}

export const api = {
  health: () => request("/api/health"),
  setupCloud: () => request("/api/setup/cloud", { method: "POST" }),
  setupLocalSelect: () => request("/api/setup/local-select", { method: "POST" }),
  modelsDownload: () => request("/api/models/download", { method: "POST" }),
  capture(file, title, when) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("title", title);
    if (when) fd.append("when", when);
    return request("/api/capture", { method: "POST", body: fd });
  },
  recordStart: (title, when) => jsonPost("/api/record/start", { title, when: when || null }),
  recordStop: (job_id) => jsonPost("/api/record/stop", { job_id }),
  job: (id) => request("/api/jobs/" + id),
  jobCancel: (id) => request("/api/jobs/" + id + "/cancel", { method: "POST" }),
  jobsActive: (kind) => request("/api/jobs" + qs({ kind, active: true })),
  // Settings
  autoSyncGet: () => request("/api/settings/auto-sync"),
  autoSyncSave: (body) => jsonPost("/api/settings/auto-sync", body),
  providerGet: () => request("/api/settings/provider"),
  providerSave: (body) => jsonPost("/api/setup/provider", body),
  deepgramSave: (api_key) => jsonPost("/api/setup/deepgram", { api_key }),
  // Onboarding (first-run tour + getting-started checklist)
  onboardingStatus: () => request("/api/onboarding"),
  onboardingUpdate: (body) => jsonPost("/api/onboarding", body),
  mcpInfo: () => request("/api/mcp"),
  // Workspace (accounts + multi-device sync — the M14 flow)
  workspaceStatus: (refresh = false) => request("/api/workspace" + qs({ refresh })),
  workspaceSignIn: (email, password) => jsonPost("/api/workspace/signin", { email, password }),
  workspaceSignOut: () => request("/api/workspace/signout", { method: "POST" }),
  workspaceMembers: () => request("/api/workspace/members"),
  workspaceRemoveMember: (userId) =>
    request("/api/workspace/members/" + userId, { method: "DELETE" }),
  workspaceInvite: (role = "member", email = null) =>
    jsonPost("/api/workspace/invites", email ? { role, email } : { role }),
  workspaceResetRequest: (email) => jsonPost("/api/workspace/reset-request", { email }),
  workspaceServerUrl: (url) => jsonPost("/api/workspace/server-url", { url }),
  workspaceOpenSignup: () => request("/api/workspace/open-signup", { method: "POST" }),
  websiteOpen: (path) => jsonPost("/api/website/open", { path }),
  workspaceSync: () => request("/api/workspace/sync", { method: "POST" }),
  workspaceBinding: () => request("/api/workspace/binding"),
  workspaceBindingUpload: () => jsonPost("/api/workspace/binding", { action: "upload" }),
  workspaceDelete: (password) => jsonPost("/api/workspace/delete", { password }),
  accountDelete: (password) => jsonPost("/api/account/delete", { password }),
  // Sources (connect + sync — the M10 flow)
  sourcesCatalog: () => request("/api/sources/catalog"),
  sourcesList: () => request("/api/sources"),
  sourcesConnect: (body) => jsonPost("/api/sources/connect", body),
  sourcesSetupPage: (name) => request("/api/sources/" + name + "/setup-page", { method: "POST" }),
  sourcesTest: (name) => request("/api/sources/" + name + "/test", { method: "POST" }),
  sourcesRemove: (name) => request("/api/sources/" + name, { method: "DELETE" }),
  sourcesSync: (name, opts = {}) => jsonPost("/api/sources/" + name + "/sync", opts),
  // Store browsing (read-only)
  storeSearch: (params) => request("/api/store/search" + qs(params)),
  storeUnit: (id) => request("/api/store/units/" + id),
  storeEpisodes: () => request("/api/store/episodes"),
  storeEpisode: (id, includeRaw = false) =>
    request("/api/store/episodes/" + id + qs({ include_raw: includeRaw })),
  storeEpisodeRename: (id, title) =>
    jsonPost("/api/store/episodes/" + id + "/rename", { title }),
  storeEntities: (type) => request("/api/store/entities" + qs({ type })),
  storeEntityUnits: (id) => request("/api/store/entities/" + id + "/units"),
  storeCurrent: (entityId) => request("/api/store/current" + qs({ entity_id: entityId })),
  storeGraph: () => request("/api/store/graph"),
};

// Poll a job to completion, calling onUpdate with each snapshot.
export async function pollJob(id, onUpdate, { interval = 1200 } = {}) {
  for (;;) {
    const job = await api.job(id);
    onUpdate?.(job);
    if (job.status === "done" || job.status === "error") return job;
    await new Promise((r) => setTimeout(r, interval));
  }
}
