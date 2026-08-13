<script>
  import { api } from "../api.js";
  import { captureJob } from "./jobs.svelte.js";
  import { graphsStore } from "./graphs.svelte.js";
  import Results from "./Results.svelte";

  let { backend } = $props();

  // Everything on this tab lands in the active graph (the header switcher) —
  // except auto-detected meeting capture, which stays private for now.
  const activeGraph = $derived(graphsStore.active);
  const sharedActive = $derived(activeGraph && activeGraph.kind !== "private");

  let title = $state("");
  let file = $state(null); // chosen audio file — selected, not yet processed
  let dragOver = $state(false);
  let localErr = $state(null); // pre-flight validation — job errors live in the tracker
  let fileInput = $state();

  // Job state lives in the shared tracker so a running capture — or a LIVE
  // RECORDING — survives tab switches (losing recordJobId used to make a
  // recording unstoppable once you tabbed away).
  const busy = $derived(captureJob.running);
  const job = $derived(captureJob.job);
  const result = $derived(captureJob.result);
  const err = $derived(localErr || captureJob.err);
  const canExtract = $derived(!!file && !busy);

  // Elapsed-time ticker while a job is running, so the wait is never a black box.
  let now = $state(Date.now());
  $effect(() => {
    if (!busy) return;
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });
  const elapsed = $derived(captureJob.startedAt ? fmtElapsed(now - captureJob.startedAt) : "");
  function fmtElapsed(ms) {
    // Clamp: right after attach() the ticker's `now` can predate startedAt.
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  function reset({ keepFile = false } = {}) {
    captureJob.clear();
    localErr = null;
    if (!keepFile) file = null;
  }

  // Choosing a file never runs the pipeline — it just stages the file so the
  // user can see it landed and then set a title. Fixes the old trap where
  // picking a file before typing a title silently discarded it.
  function selectFile(f) {
    if (!f) return;
    file = f;
    reset({ keepFile: true });
  }

  async function extract() {
    if (!file) {
      localErr = "Choose an audio file first.";
      return;
    }
    const chosen = file;
    reset({ keepFile: true });
    await captureJob.start(`Processing ${chosen.name}`, "capture", () =>
      api.capture(chosen, title.trim())
    );
  }

  function onDrop(e) {
    e.preventDefault();
    dragOver = false;
    selectFile(e.dataTransfer?.files?.[0]);
  }

  function onPick(e) {
    selectFile(e.currentTarget.files?.[0]);
    // Reset so re-picking the SAME file still fires a change event next time.
    e.currentTarget.value = "";
  }

  async function startRecord() {
    reset();
    captureJob.start(`Recording ${title.trim() || "meeting"}`, "record", () =>
      api.recordStart(title.trim())
    );
  }

  // Meeting detection: poll while this tab is mounted so a "Zoom is using the
  // microphone" banner appears without the user touching anything. The tray
  // notification covers the window-hidden case; this covers the open one.
  let meeting = $state(null);
  $effect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await api.meetingsStatus();
        if (alive) meeting = s;
      } catch {
        if (alive) meeting = null;
      }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  });
  const meetingDetected = $derived(
    meeting?.state === "detected" && meeting?.candidate && !busy
  );

  async function startMeetingRecord() {
    reset();
    try {
      const { job_id } = await api.meetingRecord(title.trim());
      captureJob.attach(job_id, "Meeting recording", "meeting-record");
    } catch (e) {
      localErr = e.message;
    }
  }

  async function stopRecord() {
    if (!captureJob.jobId) return;
    try {
      await api.recordStop(captureJob.jobId);
    } catch (e) {
      localErr = e.message;
    }
  }

  // Quick note — the lightest entry path; no AI, lands instantly.
  let noteText = $state("");
  let noteBusy = $state(false);
  let noteErr = $state(null);
  let noteSaved = $state(null); // episode title of the last saved note

  async function saveNote() {
    const text = noteText.trim();
    if (!text || noteBusy) return;
    noteBusy = true;
    noteErr = null;
    noteSaved = null;
    try {
      const saved = await api.graphNote(graphsStore.activeId, text);
      noteSaved = saved.episode.title;
      noteText = "";
    } catch (e) {
      noteErr = e.message;
    } finally {
      noteBusy = false;
    }
  }
</script>

<div class="card stack">
  <h2 class="display">Capture a meeting</h2>
  <p class="muted">
    Import an audio file or record live, then Vetromar ingests the information.
    {#if sharedActive}Everything here lands in <strong>{activeGraph.name}</strong>.{/if}
  </p>

  <div>
    <label class="field-label" for="title">Meeting title (optional)</label>
    <input
      id="title"
      type="text"
      bind:value={title}
      placeholder="Optional — defaults to date &amp; time"
      oninput={() => (localErr = null)}
    />
  </div>

  {#if meetingDetected && !result}
    <div class="meeting-banner row spread">
      <div>
        <strong>{meeting.candidate.name} is using the microphone</strong>
        <p class="muted" style="margin:2px 0 0; font-size:13px">
          Capture both sides of the call — your mic plus the meeting's audio.
          {#if sharedActive}Meeting captures land in My graph for now.{/if}
        </p>
      </div>
      <button class="primary" onclick={startMeetingRecord}>
        Start meeting capture
      </button>
    </div>
  {/if}

  {#if !busy && !result}
    <div
      class="dropzone {dragOver ? 'over' : ''}"
      role="button"
      tabindex="0"
      onclick={() => fileInput.click()}
      onkeydown={(e) => e.key === "Enter" && fileInput.click()}
      ondragover={(e) => {
        e.preventDefault();
        dragOver = true;
      }}
      ondragleave={() => (dragOver = false)}
      ondrop={onDrop}
    >
      {#if file}
        <p><strong>{file.name}</strong></p>
        <p style="font-size:13px">click to choose a different file</p>
      {:else}
        <p><strong>Drop an audio file here</strong>, or click to choose one</p>
        <p style="font-size:13px">wav · mp3 · m4a · flac · ogg · aac · mp4</p>
      {/if}
    </div>
    <input
      bind:this={fileInput}
      type="file"
      accept=".wav,.mp3,.m4a,.flac,.ogg,.aac,.mp4,audio/*"
      style="display:none"
      onchange={onPick}
    />

    <div class="row spread">
      <button class="primary" disabled={!canExtract} onclick={extract}>
        Extract decisions
      </button>
      <div class="row" style="gap:10px">
        <span class="muted" style="font-size:13px">— or —</span>
        <button onclick={startRecord}>● Record live</button>
      </div>
    </div>
  {/if}

  {#if busy}
    <div class="stack">
      {#if job?.status === "recording"}
        <div class="row" style="gap:10px">
          <span class="pill Leaning">● Recording · {elapsed}</span>
          <button class="danger" onclick={stopRecord}>Stop &amp; process</button>
        </div>
      {:else}
        <div class="row" style="gap:10px">
          <span class="spinner"></span>
          <strong>
            {job?.stage ?? "Processing…"}{job?.percent != null
              ? ` — ${Math.round(job.percent)}%`
              : ""}
          </strong>
        </div>
        {#if job?.percent != null}
          <div class="progressbar">
            <div class="fill" style="width:{Math.max(2, job.percent)}%"></div>
          </div>
        {/if}
        <p class="muted" style="font-size:13px">
          {elapsed} elapsed · transcription runs locally on your CPU, so time scales
          with the recording's length (a 40-min meeting on large-v3 can take a while)
        </p>
      {/if}
    </div>
  {/if}

  {#if err}<p class="error">{err}</p>{/if}

  {#if result}
    <div class="row">
      <button onclick={() => reset()}>Capture another</button>
    </div>
  {/if}
</div>

{#if result}
  <Results {result} />
{/if}

<div class="card stack" style="margin-top:16px">
  <h3 class="display" style="margin:0">Quick note</h3>
  <p class="muted" style="margin:0">
    Type or paste something worth keeping — it lands in
    <strong>{activeGraph?.name ?? "My graph"}</strong> instantly, no processing.
  </p>
  <textarea
    rows="3"
    placeholder="e.g. Mo switched the prototype to Godot after the physics demo…"
    bind:value={noteText}
    onkeydown={(e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveNote();
    }}
  ></textarea>
  <div class="row spread">
    <button class="primary" disabled={!noteText.trim() || noteBusy} onclick={saveNote}>
      {noteBusy ? "Saving…" : "Save note"}
    </button>
    {#if noteSaved}<span class="muted" style="font-size:13px">Saved: “{noteSaved}”</span>{/if}
  </div>
  {#if noteErr}<p class="error">{noteErr}</p>{/if}
</div>

<style>
  .meeting-banner {
    border: 1px solid var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, transparent);
    border-radius: 10px;
    padding: 12px 14px;
  }
</style>
