<script>
  // Full view of one unit payload ({unit, episode, edges, labels}) as served
  // by /api/store/units/{id}. Read-only; edges navigate via onNavigate.
  let { payload, onBack, onNavigate } = $props();

  const unit = $derived(payload.unit);
  const episode = $derived(payload.episode);
  const edges = $derived(payload.edges ?? []);
  const labels = $derived(payload.labels ?? {});

  function fmt(iso) {
    return iso ? new Date(iso).toLocaleString() : "";
  }

  function stamp(ms) {
    const s = Math.floor(ms / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  function pillLabel(u) {
    return u.payload?.status ?? (u.type ? u.type[0].toUpperCase() + u.type.slice(1) : "Unit");
  }

  // An edge, oriented from this unit's point of view.
  function edgeView(edge) {
    const outgoing = edge.from_id === unit.id;
    const otherId = outgoing ? edge.to_id : edge.from_id;
    const other = labels[otherId] ?? { label: otherId, node: "unknown" };
    let verb = edge.kind;
    if (edge.kind === "supersedes") verb = outgoing ? "supersedes" : "superseded by";
    else if (!outgoing && edge.kind === "mentions") verb = "mentioned in"; // entity edges point unit→entity
    return { otherId, other, verb, outgoing };
  }
</script>

<div class="card stack">
  <div class="row spread">
    <button onclick={onBack}>← Back</button>
    <span class="muted" style="font-size:13px">{unit.id}</span>
  </div>

  <div class="meta row" style="flex-wrap:wrap">
    <span class="pill {unit.payload?.status ?? ''}">{pillLabel(unit)}</span>
    <span class="badge">{unit.provenance?.method}</span>
    <span class="badge">{episode?.source_kind}</span>
    {#if unit.provenance?.contributor?.handle}
      <span class="badge" title={unit.provenance.contributor.display_name || ""}>
        @{unit.provenance.contributor.handle}
      </span>
    {/if}
    {#if unit.valid_to}
      <span class="pill superseded">superseded</span>
    {/if}
  </div>

  <h2>{unit.content}</h2>

  <dl class="detail-dl">
    {#if unit.reasoning}
      <dt>Why</dt>
      <dd>{unit.reasoning}</dd>
    {/if}
    {#if unit.payload?.advocate}
      <dt>Advocate</dt>
      <dd>{unit.payload.advocate.ref}</dd>
    {/if}
    {#if unit.payload?.objectors?.length}
      <dt>Objections</dt>
      <dd>
        {#each unit.payload.objectors as o}
          <div><strong>{o.person.ref}:</strong> {o.grounds}</div>
        {/each}
      </dd>
    {/if}
    {#if unit.payload?.rejected_alternatives?.length}
      <dt>Rejected</dt>
      <dd>
        {#each unit.payload.rejected_alternatives as a}
          <div><strong>{a.alternative}</strong> — {a.why_rejected}</div>
        {/each}
      </dd>
    {/if}
    {#if unit.payload?.owner}
      <dt>Owner</dt>
      <dd>{unit.payload.owner.ref}</dd>
    {/if}
    {#if unit.payload?.due}
      <dt>Due</dt>
      <dd>{fmt(unit.payload.due)}</dd>
    {/if}
    {#if unit.payload?.raised_by}
      <dt>Raised by</dt>
      <dd>{unit.payload.raised_by.ref}</dd>
    {/if}
    {#if unit.payload?.kind === "question"}
      <dt>Resolved</dt>
      <dd>{unit.payload.resolved ? "yes" : "no"}</dd>
    {/if}
    {#if unit.payload?.kind === "metric"}
      <dt>Metric</dt>
      <dd>{unit.payload.metric}</dd>
      <dt>Value</dt>
      <dd>{unit.payload.value}{unit.payload.unit ? ` ${unit.payload.unit}` : ""}</dd>
      {#if unit.payload.at}
        <dt>Measured</dt>
        <dd>{fmt(unit.payload.at)}</dd>
      {/if}
      {#if unit.payload.source_system}
        <dt>Source</dt>
        <dd>{unit.payload.source_system}</dd>
      {/if}
    {/if}
  </dl>

  {#if unit.evidence?.length}
    <div>
      <div class="section-title">Evidence</div>
      {#each unit.evidence as ev}
        <div class="quote">
          {#if ev.kind === "quote"}
            <span class="muted">[{stamp(ev.start_ms)}]</span>
            <span class="who">{ev.speaker.ref}:</span> “{ev.text}”
          {:else if ev.kind === "excerpt"}
            {#if ev.author}<span class="who">{ev.author.ref}:</span>{/if} “{ev.text}”
            {#if ev.locator}<span class="muted"> ({ev.locator})</span>{/if}
          {:else if ev.kind === "datapoint"}
            <span class="who">{ev.description}:</span> {ev.value}
            <span class="muted"> at {fmt(ev.at)}</span>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <div>
    <div class="section-title">Provenance</div>
    <dl class="detail-dl">
      <dt>Episode</dt>
      <dd>{episode?.title} <span class="muted">({episode?.source_kind}, {fmt(episode?.occurred_at)})</span></dd>
      <dt>Method</dt>
      <dd>
        {unit.provenance?.method}{unit.provenance?.agent ? ` — ${unit.provenance.agent}` : ""}
      </dd>
      <dt>Valid</dt>
      <dd>
        {fmt(unit.valid_from)} → {unit.valid_to ? fmt(unit.valid_to) : "now"}
      </dd>
      <dt>Ingested</dt>
      <dd>{fmt(unit.ingested_at)}</dd>
    </dl>
  </div>

  {#if edges.length}
    <div>
      <div class="section-title">Links</div>
      {#each edges as edge}
        {@const v = edgeView(edge)}
        <div class="edge">
          <span class="badge">{v.verb}</span>
          {#if v.other.node === "unknown"}
            <span class="muted">{v.other.label}</span>
          {:else}
            <button class="linklike" onclick={() => onNavigate(v.otherId, v.other.node)}>
              {v.other.label}
            </button>
          {/if}
          {#if edge.confidence != null}
            <span class="muted" style="font-size:12px">conf {edge.confidence.toFixed(2)}</span>
          {/if}
          {#if edge.rationale}
            <div class="muted" style="font-size:12.5px; margin-left:2px">{edge.rationale}</div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>
