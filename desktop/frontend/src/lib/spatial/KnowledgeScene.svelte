<script>
  // Knowledge — the walkable graph room. First-person camera at eye height:
  // WASD walks, drag turns, wheel looks up/down. The graph floats in three
  // bands — entities above, knowledge in the middle, episodes below — and a
  // tablet in your hands searches it (look down to read it).
  import * as THREE from "three";
  import { CSS3DObject } from "three/addons/renderers/CSS3DRenderer.js";
  import {
    forceSimulation,
    forceLink,
    forceManyBody,
    forceCollide,
    forceX,
    forceY,
  } from "d3-force";
  import { createSceneShell } from "./scene.js";
  import { readSpatialTheme, watchScheme } from "./theme.js";
  import { api } from "../../api.js";
  import {
    captureJob,
    documentJob,
    sourcesJob,
    workspaceJob,
  } from "../jobs.svelte.js";
  import { status } from "./status.svelte.js";
  import { uiMode } from "./mode.svelte.js";

  const BAND_Y = { entity: 3.55, unit: 2.25, episode: 0.85 };
  const WORLD_R = 16; // layout clamped to ±16 on the floor plane

  let wrapEl = $state(null);
  let tabEl = $state(null);
  let hoverLabelEl = $state(null);

  let kQuery = $state("");
  let kSel = $state(null); // selected node {id, kind, label, sub}
  let selUnit = $state(null); // lazy GET /api/store/units/{id} payload
  let counts = $state(null);

  // plain (non-rune) scene-shared state — the frame loop owns it
  let graphData = null;
  let rebuildPending = false;
  let saidEmpty = false;

  async function load() {
    try {
      graphData = await api.storeGraph();
      rebuildPending = true;
      counts = {
        entities: graphData.entities.length,
        units: graphData.units.length,
        episodes: graphData.episodes.length,
      };
      if (!counts.entities && !counts.units && !counts.episodes && !saidEmpty) {
        saidEmpty = true;
        status.say("nothing here yet — capture something and it will appear in this room");
      }
    } catch (e) {
      status.sayError(e.message);
    }
  }
  // refetch whenever anything finished ingesting, even from another tab
  $effect(() => {
    void captureJob.finishedCount;
    void documentJob.finishedCount;
    void sourcesJob.finishedCount;
    void workspaceJob.finishedCount;
    load();
  });

  function select(node) {
    if (!node || kSel?.id === node.id) {
      kSel = null;
      selUnit = null;
      return;
    }
    kSel = node;
    selUnit = null;
    status.say(`inspecting ${node.label.length > 60 ? node.label.slice(0, 59) + "…" : node.label}`);
    if (node.kind === "unit") {
      api
        .storeUnit(node.id)
        .then((u) => {
          if (kSel?.id === node.id) selUnit = u;
        })
        .catch(() => {});
    }
  }

  function toClassic() {
    status.say("opening the classic knowledge view");
    uiMode.set("classic");
  }

  // ---- layout: d3-force on the floor plane, ticked synchronously ----
  function layout(d) {
    const ns = [];
    const entityIds = new Set(d.entities.map((e) => e.id));
    for (const e of d.entities) ns.push({ id: e.id, kind: "entity", label: e.name, sub: e.type });
    for (const u of d.units)
      ns.push({
        id: u.id,
        kind: "unit",
        label: u.content,
        sub: u.type,
        type: u.type,
        episode_id: u.episode_id,
      });
    for (const ep of d.episodes)
      ns.push({ id: ep.id, kind: "episode", label: ep.title, sub: ep.source_kind });

    const ls = [];
    for (const ed of d.edges) ls.push({ source: ed.from_id, target: ed.to_id });
    for (const u of d.units) ls.push({ source: u.id, target: u.episode_id });
    // entity–entity co-mentions give the top band its structure
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
          if (!seen.has(key)) {
            seen.add(key);
            ls.push({ source: arr[i], target: arr[j] });
          }
        }
    }
    const ids = new Set(ns.map((n) => n.id));
    const valid = ls.filter((l) => ids.has(l.source) && ids.has(l.target));

    const sim = forceSimulation(ns)
      .force("link", forceLink(valid).id((n) => n.id).distance(80))
      .force("charge", forceManyBody().strength(-140))
      .force("collide", forceCollide().radius(20))
      .force("x", forceX(0).strength(0.06))
      .force("y", forceY(0).strength(0.06))
      .stop();
    sim.tick(250);

    let maxAbs = 1;
    for (const n of ns) maxAbs = Math.max(maxAbs, Math.abs(n.x), Math.abs(n.y));
    const s = Math.min(0.055, WORLD_R / maxAbs);
    for (const n of ns) {
      n.wx = n.x * s;
      n.wz = n.y * s;
      n.wy = BAND_Y[n.kind];
    }
    const adj = {};
    for (const l of valid) {
      const a = l.source.id, b = l.target.id;
      (adj[a] ??= new Set()).add(b);
      (adj[b] ??= new Set()).add(a);
    }
    return { ns, ls: valid.map((l) => [l.source.id, l.target.id]), adj };
  }

  $effect(() => {
    if (!wrapEl || !tabEl || !hoverLabelEl) return;

    const shell = createSceneShell(wrapEl, { fov: 50, near: 0.05, far: 200 });
    const { scene, cssScene, camera } = shell;
    camera.rotation.order = "YXZ";
    const cam = { x: 0, z: 9.5, yaw: 0, pitch: 0 };

    scene.add(new THREE.AmbientLight(0xffffff, 0.85));
    const sun = new THREE.DirectionalLight(0xffffff, 0.7);
    sun.position.set(4, 10, 6);
    scene.add(sun);

    const floorMat = new THREE.MeshLambertMaterial();
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(60, 60), floorMat);
    floor.rotation.x = -Math.PI / 2;
    scene.add(floor);
    const grid = new THREE.GridHelper(60, 60);
    grid.position.y = 0.002;
    grid.material.transparent = true;
    grid.material.opacity = 0.3;
    scene.add(grid);

    let theme = readSpatialTheme();

    // ---- graph objects, rebuilt when data changes ----
    const sphereGeo = new THREE.SphereGeometry(0.24, 18, 14);
    const boxGeo = new THREE.BoxGeometry(0.34, 0.34, 0.34);
    const octaGeo = new THREE.OctahedronGeometry(0.36);
    let meshes = [];
    let byId = {};
    let threads = [];
    let adj = {};

    function nodeColor(n) {
      if (n.kind === "entity") return theme.g.entity;
      if (n.kind === "episode") return theme.g.episode;
      return theme.g[n.type] ?? theme.g.episode;
    }

    function clearGraph() {
      for (const m of meshes) {
        scene.remove(m);
        m.material.dispose();
        if (m.children[0]) m.children[0].material.dispose();
      }
      for (const l of threads) {
        scene.remove(l);
        l.geometry.dispose();
        l.material.dispose();
      }
      meshes = [];
      threads = [];
      byId = {};
      adj = {};
    }

    function buildGraphObjects() {
      clearGraph();
      if (!graphData) return;
      const built = layout(graphData);
      adj = built.adj;
      built.ns.forEach((n, i) => {
        const geo = n.kind === "entity" ? sphereGeo : n.kind === "episode" ? octaGeo : boxGeo;
        const mat = new THREE.MeshLambertMaterial({ transparent: true });
        mat.color.copy(nodeColor(n));
        const m = new THREE.Mesh(geo, mat);
        if (n.kind !== "entity") {
          const em = new THREE.LineBasicMaterial({ transparent: true });
          em.color.copy(theme.border);
          m.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo), em));
        }
        m.userData = { node: n, ph: i * 1.73 };
        scene.add(m);
        meshes.push(m);
        byId[n.id] = m;
      });
      for (const [a, b] of built.ls) {
        const g = new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(),
          new THREE.Vector3(),
        ]);
        const mat = new THREE.LineBasicMaterial({ transparent: true, opacity: 0.45 });
        mat.color.copy(theme.muted);
        const line = new THREE.Line(g, mat);
        line.userData = { a, b };
        scene.add(line);
        threads.push(line);
      }
    }

    function applyTheme() {
      theme = readSpatialTheme();
      scene.background = theme.bg.clone();
      floorMat.color.copy(theme.bg);
      grid.material.color.copy(theme.muted);
      for (const m of meshes) {
        m.material.color.copy(nodeColor(m.userData.node));
        if (m.children[0]) m.children[0].material.color.copy(theme.border);
      }
      for (const l of threads) l.material.color.copy(theme.muted);
    }
    applyTheme();
    const unwatch = watchScheme(applyTheme);

    // ---- first-person controls ----
    const keys = {};
    const onKeyDown = (e) => {
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
      const k = e.key.toLowerCase();
      if ("wasd".includes(k)) {
        keys[k] = true;
        e.preventDefault();
      }
    };
    const onKeyUp = (e) => {
      keys[e.key.toLowerCase()] = false;
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);

    // tablet rig: follows position + yaw only, so pitching down reveals it
    const rig = new THREE.Object3D();
    cssScene.add(rig);
    const tab = new CSS3DObject(tabEl);
    tab.scale.setScalar(0.0022);
    tab.position.set(0, -0.85, -1.15);
    tab.rotation.x = -0.65;
    rig.add(tab);

    let drag = null;
    let hoverId = null;
    function pickNode(e) {
      const h = shell.pick(e, meshes);
      return h ? h.object : null;
    }
    const onDown = (e) => {
      drag = { x: e.clientX, y: e.clientY, moved: 0 };
      wrapEl.setPointerCapture(e.pointerId);
      wrapEl.style.cursor = "grabbing";
    };
    const onMove = (e) => {
      if (drag) {
        const dx = e.clientX - drag.x;
        const dy = e.clientY - drag.y;
        drag.moved += Math.abs(dx) + Math.abs(dy);
        drag.x = e.clientX;
        drag.y = e.clientY;
        cam.yaw -= dx * 0.004;
        cam.pitch = Math.min(1.2, Math.max(-1.35, cam.pitch - dy * 0.003));
      } else {
        const h = pickNode(e);
        hoverId = h ? h.userData.node.id : null;
        wrapEl.style.cursor = hoverId ? "pointer" : "grab";
      }
    };
    const onUp = (e) => {
      if (drag && drag.moved < 5) {
        const h = pickNode(e);
        select(h ? h.userData.node : null);
      }
      drag = null;
      wrapEl.style.cursor = "grab";
    };
    const onWheel = (e) => {
      e.preventDefault();
      cam.pitch = Math.min(1.2, Math.max(-1.35, cam.pitch - e.deltaY * 0.0022));
    };
    wrapEl.addEventListener("pointerdown", onDown);
    wrapEl.addEventListener("pointermove", onMove);
    wrapEl.addEventListener("pointerup", onUp);
    wrapEl.addEventListener("wheel", onWheel, { passive: false });

    const v = new THREE.Vector3();
    shell.start((dt, t) => {
      if (rebuildPending) {
        rebuildPending = false;
        buildGraphObjects();
      }

      // walk
      const sp = 3.4 * dt;
      const fx = -Math.sin(cam.yaw), fz = -Math.cos(cam.yaw);
      const rx = Math.cos(cam.yaw), rz = -Math.sin(cam.yaw);
      if (keys.w) { cam.x += fx * sp; cam.z += fz * sp; }
      if (keys.s) { cam.x -= fx * sp; cam.z -= fz * sp; }
      if (keys.a) { cam.x -= rx * sp; cam.z -= rz * sp; }
      if (keys.d) { cam.x += rx * sp; cam.z += rz * sp; }
      cam.x = Math.min(20, Math.max(-20, cam.x));
      cam.z = Math.min(20, Math.max(-20, cam.z));
      camera.position.set(cam.x, 1.7, cam.z);
      camera.rotation.y = cam.yaw;
      camera.rotation.x = cam.pitch;
      rig.position.set(cam.x, 1.7, cam.z);
      rig.rotation.y = cam.yaw;

      const q = kQuery.trim().toLowerCase();
      const nb = hoverId ? adj[hoverId] : null;
      for (const m of meshes) {
        const u = m.userData;
        const n = u.node;
        m.position.set(
          n.wx + 0.06 * Math.sin(t * 0.5 + u.ph * 2.1),
          n.wy + 0.14 * Math.sin(t * 0.7 + u.ph),
          n.wz + 0.06 * Math.cos(t * 0.45 + u.ph)
        );
        m.rotation.y += dt * 0.15;
        const dim =
          (hoverId && n.id !== hoverId && !(nb && nb.has(n.id))) ||
          (q && !n.label.toLowerCase().includes(q));
        m.material.opacity = THREE.MathUtils.damp(m.material.opacity, dim ? 0.12 : 1, 10, dt);
        if (m.children[0]) m.children[0].material.opacity = m.material.opacity;
        m.scale.setScalar(
          THREE.MathUtils.damp(m.scale.x, kSel?.id === n.id ? 1.45 : 1, 10, dt)
        );
      }
      for (const line of threads) {
        const A = byId[line.userData.a]?.position;
        const B = byId[line.userData.b]?.position;
        if (!A || !B) continue;
        const pos = line.geometry.attributes.position;
        pos.setXYZ(0, A.x, A.y, A.z);
        pos.setXYZ(1, B.x, B.y, B.z);
        pos.needsUpdate = true;
        const target = hoverId
          ? line.userData.a === hoverId || line.userData.b === hoverId
            ? 0.9
            : 0.05
          : 0.45;
        line.material.opacity = THREE.MathUtils.damp(line.material.opacity, target, 10, dt);
      }

      // hover label pinned above the node
      const hm = hoverId ? byId[hoverId] : null;
      if (hm) {
        v.copy(hm.position);
        v.y += 0.45;
        v.project(camera);
        if (v.z < 1) {
          const n = hm.userData.node;
          hoverLabelEl.textContent =
            n.label.length > 70 ? n.label.slice(0, 69) + "…" : n.label;
          hoverLabelEl.style.opacity = 1;
          hoverLabelEl.style.transform = `translate(-50%,-100%) translate(${(((v.x + 1) / 2) * wrapEl.clientWidth).toFixed(1)}px,${(((1 - v.y) / 2) * wrapEl.clientHeight).toFixed(1)}px)`;
        } else hoverLabelEl.style.opacity = 0;
      } else hoverLabelEl.style.opacity = 0;
    });

    return () => {
      unwatch();
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      wrapEl.removeEventListener("pointerdown", onDown);
      wrapEl.removeEventListener("pointermove", onMove);
      wrapEl.removeEventListener("pointerup", onUp);
      wrapEl.removeEventListener("wheel", onWheel);
      clearGraph();
      sphereGeo.dispose();
      boxGeo.dispose();
      octaGeo.dispose();
      shell.dispose();
    };
  });
</script>

<div class="spatial-wrap knowledge-wrap" bind:this={wrapEl}>
  <div class="spatial-caption">
    knowledge — entities float above, knowledge in the middle, sources below
    {#if counts}· {counts.entities} entities · {counts.units} units · {counts.episodes} episodes{/if}
  </div>
  <div class="hover-label" bind:this={hoverLabelEl}></div>
</div>

<div class="css3d-pool">
  <div
    bind:this={tabEl}
    class="k-tablet"
    onpointerdown={(e) => e.stopPropagation()}
    onwheel={(e) => e.stopPropagation()}
  >
    <div class="k-head">tablet — search knowledge</div>
    <input type="search" placeholder="search knowledge…" bind:value={kQuery} />
    {#if kSel}
      <div class="k-sel">
        <div class="k-sel-kind">{kSel.kind}{kSel.sub ? ` · ${kSel.sub}` : ""}</div>
        <div class="k-sel-label">{kSel.label}</div>
        {#if selUnit}
          <div class="k-sel-sub">
            {selUnit.status ?? ""}{selUnit.superseded ? " · superseded" : ""}
          </div>
          {#if selUnit.evidence?.length}
            <div class="k-sel-quote">“{selUnit.evidence[0].text}”</div>
          {/if}
        {/if}
        <div class="k-sel-row">
          <button onclick={() => select(null)}>clear</button>
          <button onclick={toClassic}>more — classic view</button>
        </div>
      </div>
    {:else}
      <div class="k-hint">
        matches stay lit in the graph.<br />
        wasd — walk · drag — look · scroll — look up/down
      </div>
    {/if}
  </div>
</div>

<style>
  .knowledge-wrap {
    cursor: grab;
    touch-action: none;
    user-select: none;
  }
  .hover-label {
    position: absolute;
    left: 0;
    top: 0;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text);
    background: color-mix(in srgb, var(--panel) 85%, transparent);
    padding: 2px 6px;
    pointer-events: none;
    opacity: 0;
    white-space: nowrap;
    letter-spacing: 0.05em;
    z-index: 2;
  }
  .k-tablet {
    width: 380px;
    height: 250px;
    box-sizing: border-box;
    background: var(--panel);
    border: 2px solid var(--border);
    padding: 18px;
    font-family: var(--font-mono);
    color: var(--text);
    pointer-events: auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow: hidden;
  }
  .k-head {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .k-tablet input {
    font: inherit;
    font-size: 15px;
    background: var(--panel-2);
    border: 1.5px solid var(--muted);
    color: var(--text);
    padding: 10px 12px;
    width: 100%;
    box-sizing: border-box;
  }
  .k-hint {
    font-size: 10.5px;
    color: var(--muted);
    line-height: 1.5;
  }
  .k-sel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    overflow: hidden;
  }
  .k-sel-kind {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .k-sel-label {
    font-size: 12px;
    max-height: 54px;
    overflow: hidden;
  }
  .k-sel-sub {
    font-size: 10.5px;
    color: var(--muted);
  }
  .k-sel-quote {
    font-size: 10.5px;
    color: var(--muted);
    font-style: italic;
    max-height: 40px;
    overflow: hidden;
  }
  .k-sel-row {
    display: flex;
    gap: 10px;
  }
  .k-sel-row button {
    border: none;
    background: transparent;
    padding: 0;
    font: inherit;
    font-size: 10.5px;
    color: var(--muted);
    text-decoration: underline;
    cursor: pointer;
  }
</style>
