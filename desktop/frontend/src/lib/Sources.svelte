<script>
  import { api } from "../api.js";
  import { sourcesJob } from "./jobs.svelte.js";

  let { health } = $props();

  let catalog = $state([]);
  let sources = $state([]);
  let localErr = $state(null); // list/remove errors — job errors live in the tracker

  let showCustom = $state(false);
  let customName = $state("");
  let customUrl = $state("");
  let customKind = $state("document");

  // Catalog search — client-side filter; the catalog is small static data.
  let search = $state("");
  const shownCatalog = $derived.by(() => {
    const q = search.trim().toLowerCase();
    if (!q) return catalog;
    return catalog.filter(
      (e) =>
        e.name.toLowerCase().includes(q) ||
        e.description.toLowerCase().includes(q) ||
        e.source_kind.toLowerCase().includes(q)
    );
  });

  // Credential form for providers without dynamic client registration
  // (Slack-class): one open at a time, keyed by catalog name.
  let credFor = $state(null);
  let credId = $state("");
  let credSecret = $state("");
  let setupUrlShown = $state(null); // copyable fallback when the browser didn't open

  // Job state lives in the shared tracker so a running sync survives tab
  // switches — this component just renders whatever the tracker holds.
  const busy = $derived(sourcesJob.running);
  const job = $derived(sourcesJob.job);
  const result = $derived(sourcesJob.result);
  const err = $derived(localErr || sourcesJob.err);

  const apiMode = $derived(health?.backend === "api");
  // OAuth waits are abortable; the sync agent is not (yet).
  const canCancel = $derived(
    busy && sourcesJob.jobId && (sourcesJob.kind === "connect" || sourcesJob.kind === "test")
  );
  const cancelled = $derived(err && /cancelled/i.test(err));

  // Elapsed-time ticker while a job runs (OAuth consent + the sync agent can
  // both take a while) — same pattern as Capture.
  let now = $state(Date.now());
  $effect(() => {
    if (!busy) return;
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });
  const elapsed = $derived(sourcesJob.startedAt ? fmtElapsed(now - sourcesJob.startedAt) : "");
  function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  function fmtAgo(iso) {
    if (!iso) return null;
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.floor(hours / 24)} d ago`;
  }

  async function refresh() {
    try {
      [catalog, sources] = await Promise.all([api.sourcesCatalog(), api.sourcesList()]);
    } catch (e) {
      localErr = e.message;
    }
  }

  // Refresh the lists on mount AND whenever a tracked job finishes — even one
  // that finished while this tab wasn't open.
  $effect(() => {
    void sourcesJob.finishedCount;
    refresh();
  });

  function runJob(label, start, kind) {
    localErr = null;
    return sourcesJob.start(label, kind, start);
  }

  const connect = (name) =>
    runJob(`Connecting ${name}`, () => api.sourcesConnect({ name }), "connect");

  function toggleCredForm(entry) {
    credFor = credFor === entry.name ? null : entry.name;
    credId = "";
    credSecret = "";
    setupUrlShown = null;
  }

  async function connectWithCredentials(entry) {
    await runJob(
      `Connecting ${entry.name}`,
      () =>
        api.sourcesConnect({
          name: entry.name,
          client_id: credId.trim(),
          client_secret: credSecret.trim(),
        }),
      "connect"
    );
    if (!sourcesJob.err) {
      credFor = null;
      credId = "";
      credSecret = "";
    }
  }

  async function openSetupPage(entry) {
    try {
      const r = await api.sourcesSetupPage(entry.name);
      setupUrlShown = r.opened ? null : r.url;
    } catch (e) {
      localErr = e.message;
    }
  }

  async function connectCustom() {
    await runJob(
      `Connecting ${customName.trim()}`,
      () =>
        api.sourcesConnect({
          name: customName.trim(),
          url: customUrl.trim(),
          source_kind: customKind,
        }),
      "connect"
    );
    if (!sourcesJob.err) {
      showCustom = false;
      customName = "";
      customUrl = "";
    }
  }

  const test = (name) => runJob(`Testing ${name}`, () => api.sourcesTest(name), "test");

  const sync = (name, opts = {}) =>
    runJob(
      opts.full
        ? `Full sync of ${name}`
        : opts.dry_run
          ? `Dry-run sync of ${name}`
          : `Syncing ${name}`,
      () => api.sourcesSync(name, opts),
      "sync"
    );

  async function remove(name) {
    if (!confirm(`Disconnect ${name}? Its saved OAuth tokens are deleted too.`)) return;
    localErr = null;
    sourcesJob.clear();
    try {
      await api.sourcesRemove(name);
      await refresh();
    } catch (e) {
      localErr = e.message;
    }
  }
</script>

<div class="card stack">
  <h2 class="display">Sources</h2>
  <p class="muted">
    Connect Vetromar to your stack — one browser consent click per source, no
    tokens or config files. Sync pulls what's new into the knowledge store.
  </p>
  {#if sources.length}
    <div>
      <div class="section-title">Connected</div>
      {#each sources as s (s.name)}
        <div class="source-row">
          <div class="source-info">
            <div class="row" style="gap:8px">
              <strong>{s.name}</strong>
              <span class="badge">{s.transport}</span>
              <span class="badge">{s.source_kind}</span>
              {#if !s.enabled}<span class="badge">disabled</span>{/if}
            </div>
            <div class="muted where">
              {s.where}{#if s.last_synced_at}
                · last synced {fmtAgo(s.last_synced_at)}{/if}
            </div>
          </div>
          <div class="row" style="gap:8px; flex-wrap:wrap">
            <button
              class="primary"
              disabled={busy || !apiMode}
              onclick={() => sync(s.name)}>Sync</button
            >
            <button
              disabled={busy || !apiMode}
              title="Ingest the entire source, not just recent content"
              onclick={() => sync(s.name, { full: true })}>Full sync</button
            >
            <button disabled={busy || !apiMode} onclick={() => sync(s.name, { dry_run: true })}>
              Dry-run
            </button>
            <button disabled={busy} onclick={() => test(s.name)}>Test</button>
            <button class="danger" disabled={busy} onclick={() => remove(s.name)}>
              Remove
            </button>
          </div>
        </div>
      {/each}
      {#if !apiMode}
        <p class="muted" style="font-size:13px; margin-top:8px">
          Sync runs on the cloud backend — switch in Settings. (Local mode still
          captures meetings; connect and test work either way.)
        </p>
      {/if}
    </div>
  {/if}

  {#if busy}
    <div class="stack">
      <div class="row" style="gap:10px">
        <span class="spinner"></span>
        <strong>{sourcesJob.label}…</strong>
        <span class="muted" style="font-size:13px">{elapsed} elapsed</span>
        {#if canCancel}
          <button class="danger" onclick={() => sourcesJob.cancel()}>Cancel</button>
        {/if}
      </div>
      {#if job?.stage}
        <p class="muted" style="font-size:13.5px">{job.stage}</p>
      {/if}
      {#if job?.progress?.length}
        <div class="progress-log">{job.progress.join("\n")}</div>
      {/if}
    </div>
  {/if}

  {#if err}
    {#if cancelled}
      <p class="muted">Connection cancelled — retry whenever you're ready.</p>
    {:else}
      <p class="error">{err}</p>
    {/if}
  {/if}

  {#if result}
    {#if result.kind === "connect"}
      <p class="good-note">
        ✓ {result.name} connected — {result.tools.length} tool(s) available
      </p>
    {:else if result.kind === "test"}
      <div class="stack">
        <p class="good-note">
          ✓ {result.name} reachable — {result.tools.length} tool(s)
        </p>
        <div class="progress-log">{result.tools.join("\n")}</div>
      </div>
    {:else if result.kind === "sync"}
      <div class="sync-report">
        <h3>
          {result.dry_run
            ? "Dry run — nothing was written"
            : result.incomplete
              ? "Sync incomplete"
              : "Sync complete"}
        </h3>
        {#if result.incomplete}
          <p class="warn-note">
            The sync stopped before finishing — everything fetched so far is
            saved. Run Sync again to continue where it left off.
          </p>
        {/if}
        <p>
          {result.dry_run ? "Would ingest" : "Ingested"}
          <strong>{result.created.length}</strong> episode(s),
          {result.duplicates.length} duplicate(s) skipped{#if !result.dry_run},
            <strong>{result.units}</strong> unit(s) extracted{/if}
        </p>
        {#if result.created.length || result.extraction_failures.length}
          <div class="progress-log">
            {#each result.created as eid}+ {eid}{"\n"}{/each}
            {#each result.duplicates as eid}= {eid} (already in store){"\n"}{/each}
            {#each result.extraction_failures as eid}! extraction failed for {eid} (raw episode kept){"\n"}{/each}
          </div>
        {/if}
        {#if result.cursor && !result.dry_run}
          <p class="muted" style="font-size:13px">cursor → {result.cursor}</p>
        {/if}
      </div>
    {/if}
  {/if}

  <div>
    <div class="row spread catalog-head">
      <div class="section-title" style="margin:0">Catalog</div>
      <input
        class="catalog-search"
        type="search"
        placeholder="Search sources…"
        bind:value={search}
      />
    </div>
    <div class="catalog-grid">
      {#each shownCatalog as entry (entry.name)}
        <div class="catalog-card" class:expanded={credFor === entry.name}>
          <div class="row spread" style="flex-wrap:wrap; row-gap:4px">
            <div class="row" style="gap:8px">
              <h3>{entry.name}</h3>
              <span class="badge">{entry.source_kind}</span>
            </div>
            {#if entry.connected}
              <span class="badge connected-badge">connected</span>
            {/if}
          </div>
          <span class="sub">{entry.description}</span>
          {#if !entry.connected}
            {#if entry.needs_client_registration}
              <button
                class="primary"
                style="margin-top:auto"
                disabled={busy}
                onclick={() => toggleCredForm(entry)}
                >{credFor === entry.name ? "Cancel" : "Connect…"}</button
              >
              {#if credFor === entry.name}
                <div class="stack cred-form">
                  <p class="muted" style="font-size:13px; margin:0">
                    {entry.name} requires your own app (its server has no automatic
                    registration).
                    <button class="linklike" onclick={() => openSetupPage(entry)}>
                      Create one in the {entry.name} console →
                    </button>
                    Set its redirect URL to <code>{entry.redirect_uri}</code>, then
                    paste the app's credentials here — they stay on this device.
                  </p>
                  {#if setupUrlShown}
                    <p class="muted" style="font-size:12.5px; margin:0">
                      Open manually: {setupUrlShown}
                    </p>
                  {/if}
                  <div>
                    <label class="field-label" for="cred-id-{entry.name}">Client ID</label>
                    <input id="cred-id-{entry.name}" type="text" bind:value={credId} />
                  </div>
                  <div>
                    <label class="field-label" for="cred-secret-{entry.name}"
                      >Client secret</label
                    >
                    <input
                      id="cred-secret-{entry.name}"
                      type="password"
                      bind:value={credSecret}
                    />
                  </div>
                  <div class="row">
                    <button
                      class="primary"
                      disabled={busy || !credId.trim() || !credSecret.trim()}
                      onclick={() => connectWithCredentials(entry)}>Connect</button
                    >
                  </div>
                </div>
              {/if}
            {:else}
              <button
                class="primary"
                style="margin-top:auto"
                disabled={busy}
                onclick={() => connect(entry.name)}>Connect</button
              >
            {/if}
          {/if}
        </div>
      {/each}
    </div>
    {#if !shownCatalog.length}
      <p class="muted" style="margin-top:8px">
        No sources match “{search}” — connect a custom MCP server below.
      </p>
    {/if}
  </div>

  <div>
    <button class="linklike" onclick={() => (showCustom = !showCustom)}>
      {showCustom ? "− Hide custom server" : "+ Custom MCP server"}
    </button>
    {#if showCustom}
      <div class="stack" style="margin-top:12px">
        <div>
          <label class="field-label" for="src-name">Name</label>
          <input
            id="src-name"
            type="text"
            bind:value={customName}
            placeholder="e.g. mywiki (lowercase, dashes ok)"
          />
        </div>
        <div>
          <label class="field-label" for="src-url">Server URL</label>
          <input
            id="src-url"
            type="text"
            bind:value={customUrl}
            placeholder="https://…/mcp"
          />
        </div>
        <div>
          <label class="field-label" for="src-kind">Content kind</label>
          <select id="src-kind" bind:value={customKind}>
            <option value="document">document</option>
            <option value="ticket">ticket</option>
            <option value="chat">chat</option>
            <option value="email">email</option>
            <option value="metric_pull">metric_pull</option>
          </select>
        </div>
        <div class="row">
          <button
            class="primary"
            disabled={busy || !customName.trim() || !customUrl.trim()}
            onclick={connectCustom}>Connect</button
          >
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .source-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    border: 2px solid var(--border);
    background: var(--panel);
    padding: 14px 18px;
  }
  .source-row + .source-row {
    margin-top: 10px;
  }
  .source-info .where {
    font-size: 12.5px;
    margin-top: 2px;
    word-break: break-all;
  }
  .catalog-head {
    margin-bottom: 10px;
    gap: 12px;
    flex-wrap: wrap;
  }
  .catalog-search {
    max-width: 260px;
    padding: 7px 12px;
    font-size: 13.5px;
  }
  .catalog-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
    gap: 14px;
  }
  .catalog-card.expanded {
    grid-column: 1 / -1;
  }
  .cred-form {
    border-top: 1px solid var(--border);
    margin-top: 10px;
    padding-top: 12px;
    gap: 10px;
  }
  .cred-form code {
    font-size: 12.5px;
    user-select: all;
  }
  .catalog-card {
    display: flex;
    flex-direction: column;
    gap: 6px;
    border: 2px solid var(--border);
    background: var(--panel);
    padding: 16px 18px;
    min-height: 118px;
  }
  .catalog-card h3 {
    font-size: 15.5px;
    text-transform: capitalize;
  }
  .catalog-card .sub {
    color: var(--muted);
    font-size: 13px;
  }
  .connected-badge {
    color: var(--good);
    border-color: color-mix(in srgb, var(--good) 45%, transparent);
    background: color-mix(in srgb, var(--good) 12%, transparent);
  }
  .good-note {
    color: var(--good);
    font-weight: 600;
    margin: 0;
  }
  .warn-note {
    color: var(--warn);
    font-weight: 600;
  }
  .sync-report {
    border: 2px solid var(--border);
    background: var(--panel);
    padding: 16px 18px;
  }
  .sync-report h3 {
    font-size: 15.5px;
    margin-bottom: 6px;
  }
  .sync-report p {
    margin: 0 0 8px;
  }
  .linklike {
    border: none;
    background: none;
    padding: 0;
    color: var(--accent);
    font: inherit;
    cursor: pointer;
  }
  .linklike:hover {
    text-decoration: underline;
  }
</style>
