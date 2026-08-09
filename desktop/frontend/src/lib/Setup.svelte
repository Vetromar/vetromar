<script>
  import { onMount } from "svelte";
  import { api } from "../api.js";
  import { downloadJob } from "./jobs.svelte.js";

  let { health, onDone, onChanged = null, onBack = null, onReplayTour = null } = $props();

  let busy = $state(false);
  let err = $state(null);

  // BYO AI provider (Anthropic key, or any OpenAI-compatible endpoint).
  let provider = $state(null); // GET /api/settings/provider snapshot
  let providerType = $state("anthropic");
  let anthropicKey = $state("");
  let openaiBaseUrl = $state("");
  let openaiKey = $state("");
  let openaiModel = $state("");
  let providerBusy = $state(false);
  let providerErr = $state(null);
  let providerSaved = $state(false);

  // Fast transcription (optional Deepgram key).
  let deepgramKey = $state("");
  let deepgramBusy = $state(false);
  let deepgramErr = $state(null);
  let deepgramSaved = $state(false);

  const providerFormValid = $derived(
    providerType === "anthropic"
      ? anthropicKey.trim().length > 0
      : openaiBaseUrl.trim().length > 0 && openaiModel.trim().length > 0
  );

  // Automatic sync — reachable directly from Settings.
  let autoSyncEnabled = $state(false);
  let autoSyncInterval = $state(60);
  let autoSyncSaved = $state(false);
  let autoSyncErr = $state(null);

  const firstRun = $derived(!onBack);
  const backend = $derived(health?.backend);
  const models = $derived(health?.local_models);
  const extractionReady = $derived(
    models?.extraction?.runtime && models?.extraction?.model_present
  );
  const transcriptionReady = $derived(models?.transcription?.present);
  const localModelsReady = $derived(extractionReady && transcriptionReady);

  onMount(async () => {
    try {
      const s = await api.autoSyncGet();
      autoSyncEnabled = s.enabled;
      autoSyncInterval = s.interval_minutes;
    } catch {
      // server briefly unreachable — the section keeps its defaults
    }
    try {
      provider = await api.providerGet();
      providerType = provider.provider === "openai" ? "openai" : "anthropic";
      openaiBaseUrl = provider.openai_base_url || "";
      if (provider.provider === "openai") openaiModel = provider.model || "";
    } catch {
      // section renders with blank defaults
    }
  });

  async function saveProvider() {
    providerBusy = true;
    providerErr = null;
    providerSaved = false;
    try {
      const body =
        providerType === "anthropic"
          ? { provider: "anthropic", api_key: anthropicKey.trim() }
          : {
              provider: "openai",
              base_url: openaiBaseUrl.trim(),
              api_key: openaiKey.trim() || null,
              model: openaiModel.trim(),
            };
      provider = await api.providerSave(body);
      providerSaved = true;
      anthropicKey = "";
      openaiKey = "";
      onChanged?.();
    } catch (e) {
      providerErr = e.message;
    } finally {
      providerBusy = false;
    }
  }

  async function saveDeepgram() {
    deepgramBusy = true;
    deepgramErr = null;
    deepgramSaved = false;
    try {
      await api.deepgramSave(deepgramKey.trim());
      deepgramSaved = true;
      deepgramKey = "";
      provider = await api.providerGet();
      onChanged?.();
    } catch (e) {
      deepgramErr = e.message;
    } finally {
      deepgramBusy = false;
    }
  }

  async function saveAutoSync() {
    autoSyncErr = null;
    autoSyncSaved = false;
    try {
      const s = await api.autoSyncSave({
        enabled: autoSyncEnabled,
        interval_minutes: Number(autoSyncInterval),
      });
      autoSyncEnabled = s.enabled;
      autoSyncInterval = s.interval_minutes;
      autoSyncSaved = true;
    } catch (e) {
      autoSyncErr = e.message;
    }
  }

  async function chooseCloud() {
    busy = true;
    err = null;
    try {
      await api.setupCloud();
      if (firstRun) onDone?.();
      else onChanged?.();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function chooseLocal() {
    busy = true;
    err = null;
    try {
      await api.setupLocalSelect();
      onChanged?.();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function startDownload({ andContinue = false } = {}) {
    if (downloadJob.running) return;
    err = null;
    await downloadJob.start(
      "Downloading local models",
      "download-models",
      api.modelsDownload
    );
    if (downloadJob.err) return;
    if (andContinue) onDone?.();
    else onChanged?.();
  }
</script>

<div class="card stack">
  <div class="row spread">
    <h2 class="display">{firstRun ? "Set up extraction" : "Settings"}</h2>
    {#if onBack}<button onclick={onBack}>Back</button>{/if}
  </div>
  <p class="muted">
    Choose how meetings are turned into decision units. You can change this later.
  </p>

  <div class="choice-grid">
    <button class="choice" class:active={backend === "api"} disabled={busy} onclick={chooseCloud}>
      <h3>Cloud {#if backend === "api"}<span class="current">— current</span>{/if}</h3>
      <span class="sub">
        Uses your own AI provider (configured below) — best quality, needs an
        API key or endpoint. Data goes to the provider you choose.
      </span>
    </button>
    <button class="choice" class:active={backend === "local"} disabled={busy} onclick={chooseLocal}>
      <h3>Local {#if backend === "local"}<span class="current">— current</span>{/if}</h3>
      <span class="sub">
        Private, on-device — audio and text never leave this machine. Needs
        the local models downloaded below.
      </span>
    </button>
  </div>
  {#if err}<p class="error">{err}</p>{/if}

  <div class="stack" style="margin-top: 8px">
    <h3>AI provider</h3>
    <p class="muted">
      Bring your own model: an Anthropic API key, or any OpenAI-compatible
      endpoint (OpenAI, OpenRouter, Groq, Ollama, LM Studio, vLLM, …). Keys
      are stored only on this machine.
    </p>
    {#if provider?.provider === "openai" && provider?.openai_base_url}
      <p class="muted">
        Current: {provider.openai_base_url} · {provider.model}
        {provider.has_openai_key ? " · key saved" : ""}
      </p>
    {:else if provider?.has_anthropic_key}
      <p class="muted">Current: Anthropic · {provider.model} · key saved</p>
    {/if}
    <div class="row" style="gap: 8px">
      <button class:active-pill={providerType === "anthropic"} class="pill"
        onclick={() => (providerType = "anthropic")}>Anthropic</button>
      <button class:active-pill={providerType === "openai"} class="pill"
        onclick={() => (providerType = "openai")}>OpenAI-compatible</button>
    </div>
    {#if providerType === "anthropic"}
      <input
        type="password"
        placeholder="Anthropic API key (sk-ant-…)"
        bind:value={anthropicKey}
      />
    {:else}
      <input
        placeholder="Base URL — e.g. https://api.openai.com/v1, or http://localhost:11434/v1 for Ollama"
        bind:value={openaiBaseUrl}
      />
      <input
        type="password"
        placeholder="API key (leave empty for local servers like Ollama)"
        bind:value={openaiKey}
      />
      <input
        placeholder="Model — e.g. gpt-5-mini, or an Ollama tag like qwen3.5:9b"
        bind:value={openaiModel}
      />
    {/if}
    <div class="row">
      <button class="primary" disabled={providerBusy || !providerFormValid} onclick={saveProvider}>
        {#if providerBusy}<span class="spinner"></span> Validating…{:else}Save & validate{/if}
      </button>
      {#if providerSaved}<span class="muted">✓ Saved</span>{/if}
    </div>
    {#if providerErr}<p class="error">{providerErr}</p>{/if}
  </div>

  <div class="stack" style="margin-top: 8px">
    <h3>Fast transcription <span class="muted">(optional)</span></h3>
    <p class="muted">
      Add your own Deepgram API key to transcribe recordings in seconds
      (audio is uploaded to Deepgram). Without one, transcription runs
      locally on this machine.
    </p>
    {#if provider?.has_deepgram_key}
      <p class="muted">✓ A Deepgram key is configured — cloud transcription is on.</p>
    {/if}
    <div class="row" style="gap: 8px; align-items: center">
      <input
        type="password"
        placeholder="Deepgram API key"
        bind:value={deepgramKey}
        style="flex: 1"
      />
      <button
        class="primary"
        disabled={deepgramBusy || !deepgramKey.trim()}
        onclick={saveDeepgram}
      >
        {#if deepgramBusy}<span class="spinner"></span>{:else}Save{/if}
      </button>
      {#if deepgramSaved}<span class="muted">✓ Saved</span>{/if}
    </div>
    {#if deepgramErr}<p class="error">{deepgramErr}</p>{/if}
  </div>

  <div class="stack" style="margin-top: 8px">
    <h3>Local models</h3>
    <p class="muted">
      Local mode runs entirely on this machine. The models download once,
      on your say-so — nothing is fetched automatically.
    </p>
    {#if models}
      <ul class="model-list">
        <li class="row spread">
          <span>Extraction model <span class="muted">· {models.extraction.model} (~6.6 GB)</span></span>
          <span class="muted">{extractionReady ? "✓ downloaded" : "not downloaded"}</span>
        </li>
        <li class="row spread">
          <span>Transcription models <span class="muted">· speech + alignment + speaker ID (~2 GB)</span></span>
          <span class="muted">{transcriptionReady ? "✓ downloaded" : "not downloaded"}</span>
        </li>
        <li class="row spread">
          <span>Search embedding model <span class="muted">· (~65 MB, automatic)</span></span>
          <span class="muted">{models.embedding?.cached ? "✓ downloaded" : "not downloaded"}</span>
        </li>
      </ul>
    {/if}
    {#if !localModelsReady || downloadJob.running}
      <div class="row">
        <button
          class="primary"
          disabled={downloadJob.running}
          onclick={() => startDownload({ andContinue: firstRun && backend === "local" })}
        >
          {#if downloadJob.running}
            <span class="spinner"></span> Downloading…
          {:else if firstRun && backend === "local"}
            Download local models (~8 GB) & continue
          {:else}
            Download local models (~8 GB)
          {/if}
        </button>
      </div>
    {/if}
    {#if downloadJob.running && downloadJob.job?.progress?.length}
      <div class="progress-log">{downloadJob.job.progress.join("\n")}</div>
    {/if}
    {#if downloadJob.err}<p class="error">{downloadJob.err}</p>{/if}
  </div>

  {#if firstRun && health?.ready}
    <div class="row">
      <button class="primary" onclick={() => onDone?.()}>Continue →</button>
    </div>
  {/if}

  <div class="stack" style="margin-top: 8px">
    <h3>Automatic sync</h3>
    <p class="muted">
      Sync connected sources in the background while the app is open, so the
      knowledge store stays current without clicking Sync.
    </p>
    <label class="row" style="gap:8px; cursor:pointer">
      <input type="checkbox" bind:checked={autoSyncEnabled} />
      Sync connected sources automatically
    </label>
    <div>
      <label class="field-label" for="autosync-interval">Every (minutes)</label>
      <input
        id="autosync-interval"
        type="number"
        min="5"
        step="5"
        bind:value={autoSyncInterval}
        disabled={!autoSyncEnabled}
        style="max-width:120px"
      />
    </div>
    <div class="row">
      <button class="primary" onclick={saveAutoSync}>Save</button>
      {#if autoSyncSaved}
        <span class="muted">✓ Saved{autoSyncEnabled ? " — next check within a minute" : ""}</span>
      {/if}
    </div>
    {#if autoSyncErr}<p class="error">{autoSyncErr}</p>{/if}
  </div>

  {#if onReplayTour}
    <div class="stack" style="margin-top: 8px">
      <h3>Onboarding</h3>
      <p class="muted">Replay the welcome tour any time.</p>
      <div class="row">
        <button onclick={onReplayTour}>Replay tour</button>
      </div>
    </div>
  {/if}

  {#if health?.app_version}
    <p class="muted" style="margin-top: 16px; font-size: 0.85em">
      Vetromar {health.app_version}
    </p>
  {/if}
</div>

<style>
  .model-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .choice.active {
    border-color: var(--accent);
  }
  .pill.active-pill {
    border-color: var(--accent);
    color: var(--text);
  }
  .current {
    font-size: 0.8em;
    color: var(--accent-strong);
    font-weight: normal;
  }
</style>
