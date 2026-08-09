// Auto-update state that outlives tab switches (module-scope runes, like
// jobs.svelte.js). The flow: check silently on launch
// and on an interval, download silently when an update exists, then show a
// visible "update ready — relaunch" prompt. Nothing here ever interrupts.
//
// This SPA also runs OUTSIDE Tauri (FastAPI serves it to a browser during
// headless verification), so everything no-ops when __TAURI_INTERNALS__ is
// absent, and the Tauri plugin packages are loaded via dynamic import inside
// that guard — the browser path never executes plugin code.

const CHECK_INTERVAL = 4 * 60 * 60 * 1000; // launch + every 4h
const LAUNCH_DELAY = 15_000; // let the sidecar handshake + first paint settle

let status = $state("idle"); // idle | checking | downloading | ready
let version = $state(null);
let notes = $state(null);
let dismissed = $state(false); // "Later" collapses the banner to a header badge
let pendingUpdate = null; // the plugin's Update object, kept for install()

function inTauri() {
  return typeof window !== "undefined" && !!window.__TAURI_INTERNALS__;
}

async function checkOnce() {
  const { check } = await import("@tauri-apps/plugin-updater");
  status = "checking";
  const update = await check();
  if (!update) {
    status = "idle";
    return;
  }
  status = "downloading"; // silent — no UI until the download is complete
  await update.download();
  pendingUpdate = update;
  version = update.version;
  notes = update.body || null;
  status = "ready";
}

let started = false;
export function startUpdateWatch() {
  if (started) return;
  // Test hook: lets the headless verify harness exercise the ready-banner UI
  // without Tauri. Set before load, e.g. { version: "9.9.9", notes: "..." }.
  const mock = typeof window !== "undefined" && window.__VETROMAR_UPDATE_MOCK__;
  if (mock) {
    started = true;
    version = mock.version || "0.0.0";
    notes = mock.notes || null;
    status = "ready";
    return;
  }
  if (!inTauri()) return; // browser/verify run — stay inert
  started = true;
  (async () => {
    await new Promise((r) => setTimeout(r, LAUNCH_DELAY));
    for (;;) {
      if (status !== "ready") {
        try {
          await checkOnce();
        } catch (e) {
          // Dev builds, offline, a translocated install, a feed without our
          // platform — all fail soft and retry next tick. Never a user-facing
          // error: an app that can't update is just an app that keeps working.
          console.warn("update check failed:", e);
          status = "idle";
        }
      }
      await new Promise((r) => setTimeout(r, CHECK_INTERVAL));
    }
  })();
}

// Apply the downloaded update and restart. install() swaps the .app on disk;
// relaunch() exits (the shell kills the sidecar on ExitRequested) and starts
// the new bundle, which spawns the new sidecar.
export async function relaunchToUpdate() {
  if (!pendingUpdate) return;
  await pendingUpdate.install();
  const { relaunch } = await import("@tauri-apps/plugin-process");
  await relaunch();
}

export const updater = {
  get status() {
    return status;
  },
  get version() {
    return version;
  },
  get notes() {
    return notes;
  },
  get dismissed() {
    return dismissed;
  },
  dismiss() {
    dismissed = true;
  },
  reopen() {
    dismissed = false;
  },
};
