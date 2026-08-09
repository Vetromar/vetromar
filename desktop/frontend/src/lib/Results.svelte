<script>
  let { result } = $props();

  const episode = $derived(result.episode);
  const units = $derived(result.units ?? []);

  function stamp(ms) {
    const s = Math.floor(ms / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  function label(unit) {
    return unit.payload?.status ?? (unit.type ? unit.type[0].toUpperCase() + unit.type.slice(1) : "Unit");
  }

  function quotes(unit) {
    return (unit.evidence ?? []).filter((e) => e.kind === "quote");
  }

  function excerpts(unit) {
    return (unit.evidence ?? []).filter((e) => e.kind === "excerpt");
  }
</script>

<div class="card stack">
  <div class="row spread">
    <h2>{episode?.title}</h2>
    <span class="muted" style="font-size:13px">
      {episode?.source_kind} · {units.length} unit{units.length === 1 ? "" : "s"}
    </span>
  </div>

  {#if units.length === 0}
    <p class="muted">No units were extracted from this recording.</p>
  {/if}

  {#each units as unit, i}
    <div class="unit">
      <div class="meta">
        <span class="pill {unit.payload?.status ?? ''}">{label(unit)}</span>
        <span class="muted" style="font-size:13px">Unit {i + 1}</span>
      </div>
      <h3>{unit.content}</h3>
      <dl>
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
      </dl>
      {#if quotes(unit).length || excerpts(unit).length}
        <div style="margin-top:10px">
          {#each quotes(unit) as q}
            <div class="quote">
              <span class="muted">[{stamp(q.start_ms)}]</span>
              <span class="who">{q.speaker.ref}:</span> “{q.text}”
            </div>
          {/each}
          {#each excerpts(unit) as e}
            <div class="quote">
              {#if e.author}<span class="who">{e.author.ref}:</span>{/if} “{e.text}”
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/each}
</div>
