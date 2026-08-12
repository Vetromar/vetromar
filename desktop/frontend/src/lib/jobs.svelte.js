// Shared job tracking that outlives tab switches.
//
// App.svelte renders tabs with {#if}, so switching destroys the active
// component — any job state held in component $state dies with it, which made
// a running sync LOOK cancelled (the backend job always ran to completion).
// Trackers live at module scope: the webview never reloads on tab switch, so
// a remounted tab re-derives the in-flight job from here. The poll loop also
// lives here — exactly one per tracked job (a generation counter retires
// stale loops), which fixes the old orphaned-poller leak in passing.
import { api } from "../api.js";

const POLL_INTERVAL = 1200;
const WATCH_INTERVAL = 10000;

function createJobTracker() {
  let running = $state(false);
  let jobId = $state(null);
  let kind = $state(null);
  let label = $state(null);
  let job = $state(null); // latest GET /api/jobs/{id} snapshot
  let result = $state(null); // { kind, ...job.result } once done
  let err = $state(null);
  let startedAt = $state(null);
  let finishedCount = $state(0);
  let generation = 0;

  function begin(theLabel, theKind) {
    generation += 1;
    err = null;
    result = null;
    job = null;
    jobId = null;
    label = theLabel;
    kind = theKind;
    running = true;
    startedAt = Date.now();
    return generation;
  }

  function finish() {
    running = false;
    job = null;
    jobId = null;
    label = null;
    kind = null;
    startedAt = null;
    finishedCount += 1;
  }

  async function poll(id, gen, theKind) {
    for (;;) {
      let snapshot;
      try {
        snapshot = await api.job(id);
      } catch (e) {
        if (gen !== generation) return;
        err = e.message;
        finish();
        return;
      }
      if (gen !== generation) return; // superseded by a newer start/attach
      job = snapshot;
      if (snapshot.status === "done" || snapshot.status === "error") {
        if (snapshot.status === "error") err = snapshot.error;
        else result = { kind: theKind, ...snapshot.result };
        finish();
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL));
      if (gen !== generation) return;
    }
  }

  return {
    get running() {
      return running;
    },
    get jobId() {
      return jobId;
    },
    get kind() {
      return kind;
    },
    get label() {
      return label;
    },
    get job() {
      return job;
    },
    get result() {
      return result;
    },
    get err() {
      return err;
    },
    get startedAt() {
      return startedAt;
    },
    get finishedCount() {
      return finishedCount;
    },

    // Kick off a job (startFn returns { job_id }) and follow it to completion.
    // Resolves when the job finishes, like the old await-pollJob pattern.
    async start(theLabel, theKind, startFn) {
      const gen = begin(theLabel, theKind);
      try {
        const { job_id } = await startFn();
        if (gen !== generation) return;
        jobId = job_id;
        await poll(job_id, gen, theKind);
      } catch (e) {
        if (gen !== generation) return;
        err = e.message;
        finish();
      }
    },

    // Adopt an already-running job (e.g. a scheduler-launched auto-sync).
    attach(job_id, theLabel, theKind) {
      if (jobId === job_id) return;
      const gen = begin(theLabel, theKind);
      jobId = job_id;
      poll(job_id, gen, theKind);
    },

    // Dismiss a finished job's result/error.
    clear() {
      result = null;
      err = null;
    },

    async cancel() {
      if (!jobId) return;
      try {
        await api.jobCancel(jobId);
      } catch {
        // the job may have just finished — polling picks up its final state
      }
    },
  };
}

export const sourcesJob = createJobTracker();
export const captureJob = createJobTracker();
export const workspaceJob = createJobTracker();
// The ~8 GB local-model download must survive tab switches like any sync.
export const downloadJob = createJobTracker();
// Document upload + extraction (Knowledge tab) — big PDFs extract in chunks.
export const documentJob = createJobTracker();

// Discover server-initiated sync jobs (background auto-sync + background
// workspace sync) so they render exactly like manual ones. Started once from
// App.svelte.
let watchStarted = false;
export function startBackgroundWatch() {
  if (watchStarted) return;
  watchStarted = true;
  (async () => {
    for (;;) {
      try {
        const active = await api.jobsActive("sync");
        const untracked = active.find((j) => j.id !== sourcesJob.jobId);
        if (untracked && !sourcesJob.running) {
          const src = untracked.meta?.source || "source";
          sourcesJob.attach(untracked.id, `Auto-sync of ${src}`, "sync");
        }
      } catch {
        // server briefly unreachable — retry on the next tick
      }
      try {
        // A tray/notification-started meeting recording renders in the
        // Capture tab like a manual one, Stop button included.
        const active = await api.jobsActive("meeting-record");
        const untracked = active.find((j) => j.id !== captureJob.jobId);
        if (untracked && !captureJob.running) {
          captureJob.attach(untracked.id, "Meeting recording", "meeting-record");
        }
      } catch {
        // ditto
      }
      try {
        const active = await api.jobsActive("workspace-sync");
        const untracked = active.find((j) => j.id !== workspaceJob.jobId);
        if (untracked && !workspaceJob.running) {
          workspaceJob.attach(untracked.id, "Workspace sync", "workspace-sync");
        }
      } catch {
        // ditto
      }
      await new Promise((r) => setTimeout(r, WATCH_INTERVAL));
    }
  })();
}
