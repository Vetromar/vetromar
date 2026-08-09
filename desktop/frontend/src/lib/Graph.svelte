<script>
  // 3D Obsidian-style view of the knowledge graph: the three abstractions as
  // strata stacked along the vertical axis —
  //   Entities            y = +160   (most abstract)
  //   Knowledge (units)   y =    0
  //   Sources & evidence  y = −160   (episodes + every evidence atom)
  // The d3-force physics is unchanged and 2D — it lays out the horizontal
  // plane; altitude is structural. Orbit/pan/dolly camera, Blender-style
  // axis gizmo, hover neighborhood highlighting, click drills into the
  // existing detail views.
  import {
    forceSimulation,
    forceLink,
    forceManyBody,
    forceCollide,
    forceX,
    forceY,
  } from "d3-force";
  import {
    basis,
    project,
    unprojectToPlane,
    axisView,
    angleDelta,
    clampPitch,
    dot,
  } from "./camera3d.js";

  let { data, onOpenUnit, onOpenEntity, onOpenEpisode } = $props();

  const W = 960;
  const H = 600;
  const VP = { w: W, h: H };

  const ALT = { entity: 160, unit: 0, episode: -160, evidence: -160 };
  const STRATA = [
    { name: "Entities", kinds: ["entity"], alt: 160, minLayer: 1 },
    { name: "Knowledge", kinds: ["unit"], alt: 0, minLayer: 2 },
    { name: "Sources & evidence", kinds: ["episode", "evidence"], alt: -160, minLayer: 3 },
  ];

  let layer = $state(3); // how many strata are visible, top-down
  let nodes = $state([]);
  let links = $state([]);
  let hovered = $state(null);
  let cam = $state({ yaw: 0.35, pitch: -0.5, dist: 1100, target: { x: W / 2, y: 0, z: 300 } });

  let svgEl;
  let sim = null;
  let neighbors = {};
  let drag = null; // {type:'orbit'|'pan'|'node', ...}
  let prevPositions = {};
  let userMoved = false;

  const UNIT_COLORS = {
    decision: "#4ade80",
    claim: "#6ea8fe",
    commitment: "#fbbf24",
    question: "#f472b6",
    metric: "#2dd4bf",
  };

  const LAYERS = [
    { n: 1, label: "Entities" },
    { n: 2, label: "Knowledge" },
    { n: 3, label: "Sources & evidence" },
  ];

  function color(node) {
    if (node.kind === "entity") return "#c084fc";
    if (node.kind === "unit") return UNIT_COLORS[node.type] ?? "#9aa3b2";
    if (node.kind === "episode") return "#94a3b8";
    return "#64748b";
  }

  function radius(node) {
    if (node.kind === "entity") return 11 + 2.4 * Math.sqrt(node.degree ?? 0);
    if (node.kind === "episode") return 17;
    if (node.kind === "evidence") return 4;
    return 9;
  }

  function short(text, n = 30) {
    return text.length > n ? text.slice(0, n - 1) + "…" : text;
  }

  const visibleKinds = $derived(
    new Set(STRATA.filter((s) => layer >= s.minLayer).flatMap((s) => s.kinds))
  );

  // -- graph assembly (all strata always simulated; visibility is render-time)

  function buildGraph(d) {
    const ns = [];
    const ls = [];
    const entityIds = new Set(d.entities.map((e) => e.id));
    for (const e of d.entities)
      ns.push({ id: e.id, kind: "entity", label: e.name, sub: e.type });
    for (const u of d.units)
      ns.push({
        id: u.id,
        kind: "unit",
        label: u.content,
        type: u.type,
        status: u.status,
        superseded: u.superseded,
        episode_id: u.episode_id,
      });
    for (const ep of d.episodes)
      ns.push({ id: ep.id, kind: "episode", label: ep.title, sub: ep.source_kind });

    for (const ed of d.edges)
      ls.push({ source: ed.from_id, target: ed.to_id, kind: ed.kind, confidence: ed.confidence });
    for (const u of d.units) ls.push({ source: u.id, target: u.episode_id, kind: "from" });
    for (const u of d.units)
      u.evidence.forEach((ev, i) => {
        const id = `${u.id}#ev${i}`;
        ns.push({ id, kind: "evidence", label: ev.text, sub: ev.kind, parent: u.id });
        ls.push({ source: id, target: u.id, kind: "evidence" });
      });

    // entity–entity co-mention links: structure for the top stratum
    const unitEnts = {};
    for (const ed of d.edges) {
      const ent = entityIds.has(ed.to_id) ? ed.to_id : entityIds.has(ed.from_id) ? ed.from_id : null;
      if (!ent) continue;
      const unit = ent === ed.to_id ? ed.from_id : ed.to_id;
      (unitEnts[unit] ??= new Set()).add(ent);
    }
    const seen = new Set();
    for (const ents of Object.values(unitEnts)) {
      const arr = [...ents];
      for (let i = 0; i < arr.length; i++)
        for (let j = i + 1; j < arr.length; j++) {
          const key = arr[i] < arr[j] ? arr[i] + "|" + arr[j] : arr[j] + "|" + arr[i];
          if (seen.has(key)) continue;
          seen.add(key);
          ls.push({ source: arr[i], target: arr[j], kind: "co-mentioned" });
        }
    }

    const ids = new Set(ns.map((n) => n.id));
    const valid = ls.filter((l) => ids.has(l.source) && ids.has(l.target));
    const adj = {};
    const byId = Object.fromEntries(ns.map((n) => [n.id, n]));
    for (const n of ns) n.degree = 0;
    for (const l of valid) {
      byId[l.source].degree++;
      byId[l.target].degree++;
      (adj[l.source] ??= new Set()).add(l.target);
      (adj[l.target] ??= new Set()).add(l.source);
    }
    return { ns, ls: valid, adj };
  }

  function seedPositions(ns) {
    const placed = {};
    for (const n of ns) {
      const prev = prevPositions[n.id];
      if (prev) {
        n.x = prev.x;
        n.y = prev.y;
        placed[n.id] = n;
      }
    }
    for (const n of ns) {
      if (n.x !== undefined) continue;
      const anchor = (n.parent && placed[n.parent]) || (n.episode_id && placed[n.episode_id]);
      const cx = anchor ? anchor.x : W / 2;
      const cy = anchor ? anchor.y : 300;
      n.x = cx + (Math.random() - 0.5) * 80;
      n.y = cy + (Math.random() - 0.5) * 80;
    }
  }

  const LINK_DISTANCE = {
    evidence: 30,
    from: 70,
    mentions: 60,
    about: 60,
    "co-mentioned": 100,
  };

  $effect(() => {
    if (!data) return;
    const { ns, ls, adj } = buildGraph(data);
    neighbors = adj;
    seedPositions(ns);

    sim?.stop();
    sim = forceSimulation(ns)
      .force(
        "link",
        forceLink(ls)
          .id((n) => n.id)
          .distance((l) => LINK_DISTANCE[l.kind] ?? 80)
      )
      .force(
        "charge",
        forceManyBody().strength((n) => (n.kind === "evidence" ? -20 : -140))
      )
      .force("collide", forceCollide().radius((n) => radius(n) + 6))
      .force("x", forceX(W / 2).strength(0.06))
      .force("y", forceY(300).strength(0.06))
      .on("tick", () => {
        nodes = [...ns];
        links = [...ls];
        if (!userMoved) fitCamera(ns);
      });
    // userMoved deliberately NOT reset here: `data` now refreshes live during
    // syncs, and a background refresh must never yank a camera the user has
    // taken over. Auto-fit keeps following growth until they do.

    for (const n of ns) prevPositions[n.id] = n;
    return () => sim?.stop();
  });

  function world(n) {
    return { x: n.x, y: ALT[n.kind], z: n.y };
  }

  function fitCamera(ns) {
    if (!ns.length) return;
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const n of ns) {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minZ) minZ = n.y;
      if (n.y > maxZ) maxZ = n.y;
    }
    const c = { x: (minX + maxX) / 2, y: 0, z: (minZ + maxZ) / 2 };
    let r = 0;
    for (const n of ns) {
      const w = world(n);
      r = Math.max(r, Math.hypot(w.x - c.x, w.y - c.y, w.z - c.z));
    }
    cam.target = c;
    cam.dist = Math.min(3000, Math.max(420, r * 2.2 + 140));
  }

  // -- projected scene (depth-sorted painter's algorithm) ---------------------

  const scene = $derived.by(() => {
    const b = basis(cam);
    const items = [];

    for (const s of STRATA) {
      if (layer < s.minLayer) continue;
      const members = nodes.filter((n) => s.kinds.includes(n.kind));
      if (!members.length) continue;
      let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
      for (const n of members) {
        if (n.x < minX) minX = n.x;
        if (n.x > maxX) maxX = n.x;
        if (n.y < minZ) minZ = n.y;
        if (n.y > maxZ) maxZ = n.y;
      }
      minX -= 80; maxX += 80; minZ -= 80; maxZ += 80;
      const corners = [
        { x: minX, y: s.alt, z: minZ },
        { x: maxX, y: s.alt, z: minZ },
        { x: maxX, y: s.alt, z: maxZ },
        { x: minX, y: s.alt, z: maxZ },
      ].map((c) => project(c, b, VP));
      if (corners.some((c) => !c)) continue;
      const center = project({ x: (minX + maxX) / 2, y: s.alt, z: (minZ + maxZ) / 2 }, b, VP);
      items.push({
        t: "plane",
        key: "plane:" + s.name,
        points: corners.map((c) => `${c.sx},${c.sy}`).join(" "),
        caption: s.name,
        cap: corners[0],
        depth: (center?.depth ?? 0) + 60, // bias: planes paint behind their nodes
      });
    }

    for (const l of links) {
      if (!visibleKinds.has(l.source.kind) || !visibleKinds.has(l.target.kind)) continue;
      const a = project(world(l.source), b, VP);
      const c = project(world(l.target), b, VP);
      if (!a || !c) continue;
      items.push({
        t: "link",
        key: "l:" + l.source.id + "→" + l.target.id + l.kind,
        l,
        x1: a.sx, y1: a.sy, x2: c.sx, y2: c.sy,
        depth: (a.depth + c.depth) / 2 + 4, // bias: links paint behind nodes
      });
    }

    for (const n of nodes) {
      if (!visibleKinds.has(n.kind)) continue;
      const p = project(world(n), b, VP);
      if (!p) continue;
      items.push({
        t: "node",
        key: n.id,
        n,
        sx: p.sx, sy: p.sy,
        r: Math.max(1.4, radius(n) * p.scale),
        scale: p.scale,
        depth: p.depth,
        fade: Math.max(0.4, Math.min(1, (1.5 * cam.dist) / p.depth - 0.5)),
      });
    }

    items.sort((a, b2) => b2.depth - a.depth);
    return items;
  });

  // -- camera controls --------------------------------------------------------

  $effect(() => {
    if (!svgEl) return;
    svgEl.addEventListener("wheel", onWheel, { passive: false });
    return () => svgEl.removeEventListener("wheel", onWheel);
  });

  function toView(e) {
    const rect = svgEl.getBoundingClientRect();
    return {
      x: ((e.clientX - rect.left) * W) / rect.width,
      y: ((e.clientY - rect.top) * H) / rect.height,
    };
  }

  function onWheel(e) {
    e.preventDefault();
    userMoved = true;
    cam.dist = Math.min(3500, Math.max(250, cam.dist * Math.exp(e.deltaY * 0.0015)));
  }

  function onBackgroundDown(e) {
    userMoved = true;
    drag = { type: e.shiftKey ? "pan" : "orbit", last: toView(e), moved: 0 };
    svgEl.setPointerCapture(e.pointerId);
  }

  function onNodeDown(e, node) {
    e.stopPropagation();
    userMoved = true;
    drag = { type: "node", node, last: toView(e), moved: 0 };
    svgEl.setPointerCapture(e.pointerId);
    sim?.alphaTarget(0.25).restart();
    node.fx = node.x;
    node.fy = node.y;
  }

  function onMove(e) {
    if (!drag) return;
    const v = toView(e);
    const dx = v.x - drag.last.x;
    const dy = v.y - drag.last.y;
    drag.moved += Math.abs(dx) + Math.abs(dy);
    drag.last = v;
    if (drag.type === "orbit") {
      cam.yaw -= dx * 0.005;
      cam.pitch = clampPitch(cam.pitch + dy * 0.005);
    } else if (drag.type === "pan") {
      const b = basis(cam);
      const s = (H / 2 / Math.tan((50 * Math.PI) / 360)) / cam.dist; // px per world unit at target
      cam.target = {
        x: cam.target.x - (b.r.x * dx) / s + (b.u.x * dy) / s,
        y: cam.target.y - (b.r.y * dx) / s + (b.u.y * dy) / s,
        z: cam.target.z - (b.r.z * dx) / s + (b.u.z * dy) / s,
      };
    } else {
      const b = basis(cam);
      const pt = unprojectToPlane(v.x, v.y, ALT[drag.node.kind], b, VP);
      if (pt) {
        drag.node.fx = pt.x;
        drag.node.fy = pt.z;
      }
    }
  }

  function onUp() {
    if (!drag) return;
    if (drag.type === "node") {
      sim?.alphaTarget(0);
      const node = drag.node;
      node.fx = null;
      node.fy = null;
      if (drag.moved < 4) open(node);
    }
    drag = null;
  }

  function open(node) {
    if (node.kind === "unit") onOpenUnit?.(node.id);
    else if (node.kind === "entity") onOpenEntity?.(node.id, node.label);
    else if (node.kind === "episode") onOpenEpisode?.(node.id);
    else if (node.kind === "evidence") onOpenUnit?.(node.parent);
  }

  // -- axis gizmo -------------------------------------------------------------

  const GIZMO_AXES = [
    { id: "+x", v: { x: 1, y: 0, z: 0 }, color: "#f87171", label: "X" },
    { id: "+y", v: { x: 0, y: 1, z: 0 }, color: "#4ade80", label: "Y" },
    { id: "+z", v: { x: 0, y: 0, z: 1 }, color: "#6ea8fe", label: "Z" },
    { id: "-x", v: { x: -1, y: 0, z: 0 }, color: "#f87171" },
    { id: "-y", v: { x: 0, y: -1, z: 0 }, color: "#4ade80" },
    { id: "-z", v: { x: 0, y: 0, z: -1 }, color: "#6ea8fe" },
  ];

  const gizmo = $derived.by(() => {
    const b = basis(cam);
    return GIZMO_AXES.map((a) => ({
      ...a,
      x: 46 + 32 * dot(a.v, b.r),
      y: 46 - 32 * dot(a.v, b.u),
      depth: dot(a.v, b.f),
    })).sort((p, q) => q.depth - p.depth); // far first
  });

  let gizmoDrag = null;
  let gizmoEl;

  function snapView(id) {
    userMoved = true;
    animateTo(axisView(id, cam.yaw));
  }

  function animateTo(view) {
    const sy = cam.yaw;
    const sp = cam.pitch;
    const dy = angleDelta(sy, view.yaw);
    const dp = view.pitch - sp;
    const t0 = performance.now();
    const dur = 350;
    function step(t) {
      const q = Math.min(1, (t - t0) / dur);
      const e = q < 0.5 ? 4 * q * q * q : 1 - Math.pow(-2 * q + 2, 3) / 2;
      cam.yaw = sy + dy * e;
      cam.pitch = sp + dp * e;
      if (q < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function onGizmoDown(e) {
    userMoved = true;
    gizmoDrag = { lastX: e.clientX, lastY: e.clientY, moved: 0 };
    gizmoEl.setPointerCapture(e.pointerId);
  }

  function onGizmoMove(e) {
    if (!gizmoDrag) return;
    const dx = e.clientX - gizmoDrag.lastX;
    const dy = e.clientY - gizmoDrag.lastY;
    gizmoDrag.moved += Math.abs(dx) + Math.abs(dy);
    gizmoDrag.lastX = e.clientX;
    gizmoDrag.lastY = e.clientY;
    cam.yaw -= dx * 0.01;
    cam.pitch = clampPitch(cam.pitch + dy * 0.01);
  }

  function onGizmoUp() {
    gizmoDrag = null;
  }

  // -- styling ---------------------------------------------------------------

  function dimNode(node) {
    return hovered && hovered !== node.id && !neighbors[hovered]?.has(node.id);
  }

  function dimLink(l) {
    return hovered && l.source.id !== hovered && l.target.id !== hovered;
  }

  function labelOpacity(it) {
    const node = it.n;
    if (hovered === node.id) return 1;
    if (dimNode(node)) return 0;
    if (node.kind === "entity" || node.kind === "episode")
      return Math.min(1, it.scale + 0.5) * it.fade;
    if (node.kind === "evidence") return 0;
    return Math.max(0, Math.min(1, (it.scale - 0.45) * 2.2)) * it.fade;
  }

  const counts = $derived(
    data
      ? {
          1: data.entities.length,
          2: data.entities.length + data.units.length,
          3:
            data.entities.length +
            data.units.length +
            data.episodes.length +
            data.units.reduce((s, u) => s + u.evidence.length, 0),
        }
      : { 1: 0, 2: 0, 3: 0 }
  );

  const hoveredNode = $derived(nodes.find((n) => n.id === hovered));
</script>

<div class="stack">
  <div class="row" style="gap:8px; flex-wrap:wrap">
    <div class="tabs">
      {#each LAYERS as l}
        <button class:active={layer === l.n} onclick={() => (layer = l.n)}>
          {l.n} · {l.label}
          <span class="count">{counts[l.n]}</span>
        </button>
      {/each}
    </div>
    <span class="muted" style="font-size:12.5px">
      drag to orbit · shift+drag to pan · scroll to zoom · drag a node to move it · click to open
    </span>
  </div>

  {#if nodes.length === 0}
    <p class="muted">Nothing captured yet — record or import a meeting in Capture.</p>
  {:else}
    <div class="graph-wrap">
      <svg
        bind:this={svgEl}
        class="scene"
        viewBox="0 0 {W} {H}"
        onpointerdown={onBackgroundDown}
        onpointermove={onMove}
        onpointerup={onUp}
        onpointercancel={onUp}
        role="img"
        aria-label="Knowledge graph"
      >
        {#each scene as it (it.key)}
          {#if it.t === "plane"}
            <polygon class="stratum" points={it.points} />
            <text class="stratum-caption" x={it.cap.sx} y={it.cap.sy - 8}>{it.caption}</text>
          {:else if it.t === "link"}
            <line
              x1={it.x1}
              y1={it.y1}
              x2={it.x2}
              y2={it.y2}
              class="glink"
              class:supersedes={it.l.kind === "supersedes"}
              opacity={dimLink(it.l) ? 0.05 : (it.l.kind === "evidence" ? 0.3 : 0.5) * (it.fade ?? 1)}
            />
          {:else}
            <g
              transform="translate({it.sx},{it.sy})"
              class="gnode"
              opacity={dimNode(it.n) ? 0.13 : 1}
              onpointerdown={(e) => onNodeDown(e, it.n)}
              onpointerenter={() => (hovered = it.n.id)}
              onpointerleave={() => (hovered = null)}
              role="button"
              tabindex="-1"
            >
              <circle
                r={it.r}
                fill={color(it.n)}
                fill-opacity={(it.n.superseded ? 0.3 : it.n.kind === "episode" ? 0.25 : 0.92) * it.fade}
                stroke={it.n.superseded ? "#f87171" : it.n.kind === "episode" ? color(it.n) : "none"}
                stroke-width={it.n.superseded || it.n.kind === "episode" ? 1.6 : 0}
              />
              {#if it.n.kind !== "evidence" || hovered === it.n.id}
                <text y={it.r + 11} opacity={labelOpacity(it)}>
                  {short(it.n.label)}
                </text>
              {/if}
            </g>
          {/if}
        {/each}
      </svg>

      <svg
        bind:this={gizmoEl}
        class="gizmo"
        viewBox="0 0 92 92"
        onpointerdown={onGizmoDown}
        onpointermove={onGizmoMove}
        onpointerup={onGizmoUp}
        onpointercancel={onGizmoUp}
        role="button"
        tabindex="-1"
        aria-label="View orientation gizmo"
      >
        <circle cx="46" cy="46" r="44" class="gizmo-bg" />
        {#each gizmo as a (a.id)}
          {#if a.label}
            <line x1="46" y1="46" x2={a.x} y2={a.y} stroke={a.color} stroke-width="1.8"
                  opacity={a.depth > 0 ? 0.55 : 0.95} />
          {/if}
        {/each}
        {#each gizmo as a (a.id + "b")}
          <g
            onpointerdown={(e) => e.stopPropagation()}
            onclick={() => snapView(a.id)}
            role="button"
            tabindex="-1"
            style="cursor:pointer"
          >
            {#if a.label}
              <circle cx={a.x} cy={a.y} r="8.5" fill={a.color} opacity={a.depth > 0 ? 0.5 : 1} />
              <text class="gizmo-label" x={a.x} y={a.y + 3.2}>{a.label}</text>
            {:else}
              <circle cx={a.x} cy={a.y} r="6" fill="none" stroke={a.color} stroke-width="1.5"
                      opacity={a.depth > 0 ? 0.4 : 0.85} />
              <circle cx={a.x} cy={a.y} r="6" fill="var(--panel)" opacity="0.01" />
            {/if}
          </g>
        {/each}
      </svg>

      {#if hoveredNode}
        <div class="graph-tip">
          <span class="badge">{hoveredNode.sub ?? hoveredNode.status ?? hoveredNode.type ?? hoveredNode.kind}</span>
          {hoveredNode.label}
        </div>
      {/if}
    </div>

    <div class="row legend" style="flex-wrap:wrap">
      <span><i style="background:#c084fc"></i> entity</span>
      <span><i style="background:#4ade80"></i> decision</span>
      <span><i style="background:#6ea8fe"></i> claim</span>
      <span><i style="background:#fbbf24"></i> commitment</span>
      <span><i style="background:#f472b6"></i> question</span>
      <span><i style="background:#2dd4bf"></i> metric</span>
      {#if layer >= 3}
        <span><i class="ring"></i> episode</span>
        <span><i style="background:#64748b; width:6px; height:6px"></i> evidence</span>
      {/if}
      <span><i style="background:transparent; border:1.5px solid #f87171"></i> superseded</span>
    </div>
  {/if}
</div>
