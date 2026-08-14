<script>
  // The fixed bottom bar of the spatial UI. Renders whatever status.say()
  // last wrote, and narrates the shared job trackers so background work
  // (auto-syncs, tray recordings) speaks here too. Mounted only in spatial
  // mode, so the $effects only run when the bar is actually visible.
  import { status } from "./status.svelte.js";
  import {
    captureJob,
    sourcesJob,
    workspaceJob,
    documentJob,
  } from "../jobs.svelte.js";

  function narrateRunning(tracker) {
    const job = tracker.job;
    if (job?.status === "recording") return; // the scene narrates elapsed time
    const label = (tracker.label || "working").toLowerCase();
    const stage = job?.stage ? ` — ${job.stage.toLowerCase()}` : "";
    const pct = job?.percent != null ? ` · ${Math.round(job.percent)}%` : "";
    status.say(label + stage + pct);
  }

  function narrateDone(tracker) {
    const r = tracker.result;
    if (!r) return;
    if (r.kind === "sync") {
      const name = (tracker.label || "").toLowerCase().replace(/^syncing\s+|^auto-sync of\s+/, "") || "source";
      if (r.incomplete) {
        status.say(`${name} sync stopped early — run it again to continue`);
      } else {
        status.say(
          `${name} sync complete — ${r.created.length} episode(s) ingested, ${r.duplicates.length} duplicate(s) skipped`
        );
      }
    } else if (r.kind === "connect") {
      status.say(`${r.name} connected — cable plugged in`);
    } else if (r.kind === "capture" || r.kind === "record" || r.kind === "meeting-record" || r.kind === "document") {
      const n = Array.isArray(r.units) ? r.units.length : r.units;
      status.say(
        `${(r.episode?.title || "item").toLowerCase()} processed — ${n ?? 0} unit(s) extracted, filed to knowledge`
      );
    } else if (r.kind === "workspace-sync") {
      status.say(`graph in sync`);
    }
  }

  // One effect per tracker: reacts to running/job (progress), err, and
  // finishedCount+result (completion). Last write wins by design.
  for (const tracker of [captureJob, sourcesJob, workspaceJob, documentJob]) {
    $effect(() => {
      if (tracker.running) narrateRunning(tracker);
    });
    $effect(() => {
      if (tracker.err) status.sayError(tracker.err);
    });
    $effect(() => {
      void tracker.finishedCount;
      narrateDone(tracker);
    });
  }
</script>

{#if status.text}
  <div class="status-line" class:error={status.kind === "error"}>{status.text}</div>
{/if}

<style>
  .status-line {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 4;
    padding: 5px 14px;
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    color: var(--muted);
    background: var(--panel);
    border-top: 2px solid var(--border);
    text-transform: lowercase;
  }
  .status-line.error {
    color: var(--bad);
    background: color-mix(in srgb, var(--bad) 10%, var(--panel));
  }
</style>
