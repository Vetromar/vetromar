<script>
  import { api } from "../api.js";
  import { captureJob } from "./jobs.svelte.js";
  import Results from "./Results.svelte";

  let { backend } = $props();

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
    const s = Math.floor(ms / 1000);
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

  async function stopRecord() {
    if (!captureJob.jobId) return;
    try {
      await api.recordStop(captureJob.jobId);
    } catch (e) {
      localErr = e.message;
    }
  }
</script>

<div class="card stack">
  <h2 class="display">Capture a meeting</h2>
  <p class="muted">
    Import an audio file or record live, then Vetromar ingests the information.
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
