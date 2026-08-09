<script>
  // Browsing of the knowledge store: hybrid search, the current state
  // grouped by entity, and episode drill-down. Selecting a unit anywhere
  // drills into UnitDetail; edges navigate onward in-view. The lone
  // mutation is renaming an episode's title — units stay read-only.
  import { api } from "../api.js";
  import { captureJob, documentJob, sourcesJob, workspaceJob } from "./jobs.svelte.js";
  import Graph from "./Graph.svelte";
  import UnitDetail from "./UnitDetail.svelte";

  let mode = $state("graph"); // graph | search | current | episodes
  let error = $state(null);
  let busy = $state(false);

  // search
  let query = $state("");
  let typeFilter = $state("");
  let currentOnly = $state(false);
  let asOf = $state("");
  let results = $state(null); // list of {unit, episode, edges, labels}
  let contextLabel = $state(null); // e.g. "Units involving Sam"

  // current state
  let current = $state(null); // {entities: [...], unattributed: [...]}

  // episodes
  let episodes = $state(null);
  let episodeDetail = $state(null); // {episode, units}
  let showRaw = $state(false);
  let renaming = $state(false);
  let renameText = $state("");

  // graph
  let graphData = $state(null);

  // drill-in
  let selected = $state(null); // full unit payload

  const TYPES = ["decision", "claim", "commitment", "question", "metric"];

  function fmt(iso) {
    return iso ? new Date(iso).toLocaleString() : "";
  }

  function pillLabel(u) {
    return u.payload?.status ?? (u.type ? u.type[0].toUpperCase() + u.type.slice(1) : "Unit");
  }

  async function run(fn) {
    busy = true;
    error = null;
    try {
      await fn();
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  function search() {
    contextLabel = null;
    return run(async () => {
      const hits = await api.storeSearch({
        text: query.trim() || undefined,
        type: typeFilter || undefined,
        current_only: currentOnly,
        as_of: asOf || undefined,
        k: 50,
      });
      // Listings come back oldest-first; show newest first when not ranked.
      results = query.trim() ? hits : hits.slice().reverse();
    });
  }

  function loadCurrent() {
    return run(async () => {
      current = await api.storeCurrent();
    });
  }

  function loadEpisodes() {
    episodeDetail = null;
    return run(async () => {
      episodes = await api.storeEpisodes();
    });
  }

  function openEpisode(id, includeRaw = false) {
    showRaw = includeRaw;
    renaming = false;
    return run(async () => {
      episodeDetail = await api.storeEpisode(id, includeRaw);
    });
  }

  function startRename() {
    renameText = episodeDetail.episode.title;
    renaming = true;
  }

  function saveRename() {
    return run(async () => {
      const ep = await api.storeEpisodeRename(
        episodeDetail.episode.id,
        renameText.trim()
      );
      // Keep any loaded raw — the rename response never carries it.
      episodeDetail = {
        ...episodeDetail,
        episode: { ...episodeDetail.episode, title: ep.title },
      };
      episodes = await api.storeEpisodes();
      // These caches carry episode titles; drop them so they refetch lazily.
      graphData = null;
      results = null;
      renaming = false;
    });
  }

  function loadGraph() {
    return run(async () => {
      graphData = await api.storeGraph();
    });
  }

  function openEpisodeFromGraph(id) {
    mode = "episodes";
    showRaw = false;
    return run(async () => {
      if (!episodes) episodes = await api.storeEpisodes();
      episodeDetail = await api.storeEpisode(id, false);
    });
  }

  function openUnit(id) {
    return run(async () => {
      selected = await api.storeUnit(id);
    });
  }

  function openEntity(id, name) {
    selected = null;
    mode = "search";
    contextLabel = name ? `Units involving ${name}` : "Units for entity";
    return run(async () => {
      results = (await api.storeEntityUnits(id)).slice().reverse();
    });
  }

  function navigate(id, node) {
    if (node === "entity") {
      const label = selected?.labels?.[id]?.label;
      openEntity(id, label);
    } else {
      openUnit(id);
    }
  }

  function setMode(m) {
    selected = null;
    mode = m;
    if (m === "graph" && !graphData) loadGraph();
    if (m === "current" && !current) loadCurrent();
    if (m === "episodes" && !episodes) loadEpisodes();
  }

  // -- document upload (drag-drop or picker; PDF/DOCX/MD/TXT) ---------------
  const DOC_EXTENSIONS = [".pdf", ".docx", ".md", ".txt"];
  let fileInput = $state(null);
  let dragOver = $state(false);

  function isDocFile(file) {
    const name = (file?.name || "").toLowerCase();
    return DOC_EXTENSIONS.some((ext) => name.endsWith(ext));
  }

  async function ingestDocument(file) {
    if (!file) return;
    if (!isDocFile(file)) {
      error = `Unsupported file type — supported: ${DOC_EXTENSIONS.join(", ")}`;
      return;
    }
    error = null;
    await documentJob.start(`Ingesting ${file.name}`, "document", () =>
      api.uploadDocument(file)
    );
    // New episode + units landed (or failed visibly) — refresh what we show.
    episodes = null;
    if (mode === "episodes") loadEpisodes();
    if (mode === "graph") refreshGraphQuietly();
  }

  function pickDocument(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    ingestDocument(file);
  }

  function onDrop(e) {
    e.preventDefault();
    dragOver = false;
    ingestDocument(e.dataTransfer?.files?.[0]);
  }

  function onDragOver(e) {
    // Only light up for file drags, and only while nothing is ingesting.
    if (!documentJob.running && e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      dragOver = true;
    }
  }

  const documentStage = $derived(
    documentJob.job?.progress?.length
      ? documentJob.job.progress[documentJob.job.progress.length - 1]
      : "starting…"
  );

  // Initial load so the screen is never blank.
  $effect(() => {
    if (mode === "graph" && graphData === null) loadGraph();
    if (mode === "search" && results === null) search();
  });

  // Live graph while knowledge is arriving: any ingesting job (source sync —
  // manual or scheduler-launched, workspace pull, capture) quietly re-fetches
  // the graph every few seconds, plus once more when the job finishes, so new
  // nodes drift into the field as they land. Silent by design: no spinner, and
  // a failed poll never surfaces — the next one retries.
  const ingesting = $derived(
    sourcesJob.running || workspaceJob.running || captureJob.running || documentJob.running
  );

  async function refreshGraphQuietly() {
    try {
      graphData = await api.storeGraph();
    } catch {
      // transient — the next poll or manual reload covers it
    }
  }

  $effect(() => {
    if (mode !== "graph" || !ingesting) return;
    const id = setInterval(refreshGraphQuietly, 5000);
    return () => {
      clearInterval(id);
      // Runs when the job completes (or the user leaves the graph) — one
      // final fetch so the finished sync's last deliveries always render.
      refreshGraphQuietly();
    };
  });
</script>

{#if selected}
  <UnitDetail payload={selected} onBack={() => (selected = null)} onNavigate={navigate} />
{:else}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="card stack"
    class:dropping={dragOver}
    ondragover={onDragOver}
    ondragleave={() => (dragOver = false)}
    ondrop={onDrop}
  >
    <div class="row" style="gap:8px">
      <button class:primary={mode === "graph"} onclick={() => setMode("graph")}>Graph</button>
      <button class:primary={mode === "search"} onclick={() => setMode("search")}>Search</button>
      <button class:primary={mode === "current"} onclick={() => setMode("current")}>Current state</button>
      <button class:primary={mode === "episodes"} onclick={() => setMode("episodes")}>Episodes</button>
      {#if busy}<span class="spinner"></span>{/if}
      <span style="flex:1"></span>
      <input
        type="file"
        accept={DOC_EXTENSIONS.join(",")}
        style="display:none"
        bind:this={fileInput}
        onchange={pickDocument}
      />
      <button
        onclick={() => fileInput?.click()}
        disabled={documentJob.running}
        title="Ingest a PDF, DOCX, Markdown, or text file — or drag one anywhere onto this panel"
      >
        + Add document
      </button>
    </div>

    {#if dragOver}
      <div class="dropzone">Drop to ingest ({DOC_EXTENSIONS.join(" · ")})</div>
    {/if}

    {#if documentJob.running}
      <div class="row" style="gap:8px">
        <span class="spinner"></span>
        <span class="muted">{documentJob.label} — {documentStage}</span>
      </div>
    {:else if documentJob.err}
      <div class="row spread">
        <p class="error" style="margin:0">{documentJob.err}</p>
        <button onclick={() => documentJob.clear()}>Dismiss</button>
      </div>
    {:else if documentJob.result?.kind === "document"}
      <div class="row spread">
        <span class="muted">
          Ingested “{documentJob.result.title}” — {documentJob.result.units} unit{documentJob.result.units === 1 ? "" : "s"} extracted.
        </span>
        <div class="row" style="gap:8px">
          <button onclick={() => (openEpisode(documentJob.result.episode_id), (mode = "episodes"), documentJob.clear())}>
            View episode
          </button>
          <button onclick={() => documentJob.clear()}>Dismiss</button>
        </div>
      </div>
    {/if}

    {#if error}
      <p class="error">{error}</p>
    {/if}

    {#if mode === "graph"}
      {#if graphData}
        <Graph
          data={graphData}
          onOpenUnit={openUnit}
          onOpenEntity={openEntity}
          onOpenEpisode={openEpisodeFromGraph}
        />
      {/if}
    {:else if mode === "search"}
      <form
        class="stack"
        onsubmit={(e) => {
          e.preventDefault();
          search();
        }}
      >
        <input
          type="text"
          placeholder="Search knowledge — paraphrases match (e.g. 'who owns the scoping doc')"
          bind:value={query}
        />
        <div class="row" style="flex-wrap:wrap">
          <select bind:value={typeFilter} onchange={search}>
            <option value="">all types</option>
            {#each TYPES as t}
              <option value={t}>{t}</option>
            {/each}
          </select>
          <label class="check">
            <input type="checkbox" bind:checked={currentOnly} onchange={search} />
            current only
          </label>
          <label class="check muted" title="Time travel: only units that were valid at this moment">
            as of
            <input type="datetime-local" bind:value={asOf} onchange={search} />
          </label>
          <button type="submit" class="primary">Search</button>
        </div>
      </form>

      {#if contextLabel}
        <div class="row spread">
          <strong>{contextLabel}</strong>
          <button onclick={search}>Clear</button>
        </div>
      {/if}

      {#if results?.length === 0}
        <p class="muted">
          {query.trim() || contextLabel
            ? "No units match."
            : "Nothing captured yet — record or import a meeting in Capture, or push knowledge via MCP."}
        </p>
      {/if}
      {#each results ?? [] as hit (hit.unit.id)}
        <button class="unit clickable" onclick={() => openUnit(hit.unit.id)}>
          <div class="meta">
            <span class="pill {hit.unit.payload?.status ?? ''}">{pillLabel(hit.unit)}</span>
            {#if hit.unit.valid_to}<span class="pill superseded">superseded</span>{/if}
            <span class="muted" style="font-size:12.5px">
              {hit.unit.provenance?.method} · {hit.episode?.source_kind} · {hit.episode?.title}
            </span>
          </div>
          <h3>{hit.unit.content}</h3>
          <span class="muted" style="font-size:12.5px">{fmt(hit.unit.valid_from)}</span>
        </button>
      {/each}
    {:else if mode === "current"}
      {#if current}
        {#if current.entities.length === 0 && current.unattributed.length === 0}
          <p class="muted">Nothing captured yet.</p>
        {/if}
        {#each current.entities as bucket (bucket.entity.id)}
          <div>
            <div class="section-title row" style="gap:8px">
              {bucket.entity.name}
              <span class="badge">{bucket.entity.type}</span>
            </div>
            {#each bucket.units as row (row.id)}
              <button class="unit clickable slim" onclick={() => openUnit(row.id)}>
                <span class="pill {row.status ?? ''}">{row.status ?? row.type}</span>
                {row.content}
              </button>
            {/each}
          </div>
        {/each}
        {#if current.unattributed.length}
          <div>
            <div class="section-title">Unattributed</div>
            {#each current.unattributed as row (row.id)}
              <button class="unit clickable slim" onclick={() => openUnit(row.id)}>
                <span class="pill {row.status ?? ''}">{row.status ?? row.type}</span>
                {row.content}
              </button>
            {/each}
          </div>
        {/if}
      {/if}
    {:else if mode === "episodes"}
      {#if episodeDetail}
        <div class="row spread">
          {#if renaming}
            <form
              class="row"
              style="gap:8px; flex:1"
              onsubmit={(e) => {
                e.preventDefault();
                saveRename();
              }}
            >
              <input
                type="text"
                bind:value={renameText}
                style="flex:1"
                aria-label="Meeting title"
              />
              <button type="submit" class="primary" disabled={!renameText.trim()}>
                Save
              </button>
              <button type="button" onclick={() => (renaming = false)}>Cancel</button>
            </form>
          {:else}
            <h2>{episodeDetail.episode.title}</h2>
            <div class="row" style="gap:8px">
              <button onclick={startRename}>Rename</button>
              <button onclick={() => ((episodeDetail = null), (renaming = false))}>
                ← All episodes
              </button>
            </div>
          {/if}
        </div>
        <div class="row" style="flex-wrap:wrap">
          <span class="badge">{episodeDetail.episode.source_kind}</span>
          <span class="muted" style="font-size:13px">{fmt(episodeDetail.episode.occurred_at)}</span>
          <label class="check muted">
            <input
              type="checkbox"
              checked={showRaw}
              onchange={(e) => openEpisode(episodeDetail.episode.id, e.target.checked)}
            />
            show raw
          </label>
        </div>
        {#if showRaw && episodeDetail.episode.raw}
          <pre class="raw">{episodeDetail.episode.raw}</pre>
        {/if}
        {#if episodeDetail.units.length === 0}
          <p class="muted">No units from this episode.</p>
        {/if}
        {#each episodeDetail.units as unit (unit.id)}
          <button class="unit clickable" onclick={() => openUnit(unit.id)}>
            <div class="meta">
              <span class="pill {unit.payload?.status ?? ''}">{pillLabel(unit)}</span>
              {#if unit.valid_to}<span class="pill superseded">superseded</span>{/if}
            </div>
            <h3>{unit.content}</h3>
          </button>
        {/each}
      {:else}
        {#if episodes?.length === 0}
          <p class="muted">No episodes yet.</p>
        {/if}
        {#each episodes ?? [] as ep (ep.id)}
          <button class="unit clickable" onclick={() => openEpisode(ep.id)}>
            <div class="meta">
              <span class="badge">{ep.source_kind}</span>
              <span class="muted" style="font-size:12.5px">{fmt(ep.occurred_at)}</span>
            </div>
            <h3>{ep.title}</h3>
          </button>
        {/each}
      {/if}
    {/if}
  </div>
{/if}

<style>
  .card.dropping {
    outline: 2px dashed var(--accent, #7c6cf0);
    outline-offset: -6px;
  }
  .dropzone {
    border: 1px dashed var(--accent, #7c6cf0);
    border-radius: 8px;
    padding: 10px 14px;
    text-align: center;
    font-size: 13px;
    opacity: 0.85;
  }
</style>
