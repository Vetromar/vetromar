<script>
  // Sources — the podium from directly overhead. Connected sources are cables
  // plugged into jack holes, trailing off out of frame; the recessed screen
  // lists and filters them; the physical sync button on the podium's front
  // face syncs whichever cable is selected. Real catalog/connect/sync calls.
  import * as THREE from "three";
  import { CSS3DObject } from "three/addons/renderers/CSS3DRenderer.js";
  import { createSceneShell, edgedBox, edgedPlane } from "./scene.js";
  import { readSpatialTheme, watchScheme } from "./theme.js";
  import { makePress, pulse } from "./anim.js";
  import { api } from "../../api.js";
  import { sourcesJob } from "../jobs.svelte.js";
  import { graphsStore } from "../graphs.svelte.js";
  import { status } from "./status.svelte.js";
  import { uiMode } from "./mode.svelte.js";

  let { health } = $props();

  const TOP = 1.4;

  // Jack holes ringing the podium border (greybox coordinates, verbatim).
  const HOLES = [
    [-3.35, -1.5], [-3.35, -0.5], [-3.35, 0.5], [-3.35, 1.5],
    [3.35, -1.5], [3.35, -0.5], [3.35, 0.5], [3.35, 1.5],
    [-2.5, -2.1], [-1.5, -2.1], [-0.5, -2.1], [0.5, -2.1], [1.5, -2.1], [2.5, -2.1],
    [-2.5, 2.1], [-1.5, 2.1], [-0.5, 2.1], [0.5, 2.1], [1.5, 2.1], [2.5, 2.1],
  ];

  let wrapEl = $state(null);
  let screenEl = $state(null);
  let syncLblEl = $state(null);

  let catalog = $state([]);
  let sources = $state([]);
  let search = $state("");
  let srcSel = $state(null); // selected source name
  let saidSharedNote = false;

  const apiMode = $derived(health?.backend === "api");

  // Which source a running job belongs to — covers scene-started jobs AND
  // background auto-syncs adopted by the watcher.
  const plugging = $derived(
    sourcesJob.running && sourcesJob.kind === "connect"
      ? (sourcesJob.label || "").replace(/^Connecting\s+/i, "")
      : null
  );
  const syncingName = $derived(
    sourcesJob.running && sourcesJob.kind === "sync"
      ? sourcesJob.job?.meta?.source ||
          (sourcesJob.label || "")
            .replace(/^(Syncing|Full sync of|Dry-run sync of|Auto-sync of)\s+/i, "")
      : null
  );

  const q = $derived(search.trim().toLowerCase());
  const shownConnected = $derived(
    sources.filter(
      (s) => !q || `${s.name} ${s.source_kind}`.toLowerCase().includes(q)
    )
  );
  const shownCatalog = $derived(
    catalog.filter(
      (e) =>
        !e.connected &&
        (!q || `${e.name} ${e.source_kind}`.toLowerCase().includes(q))
    )
  );

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
      if (srcSel && !sources.some((s) => s.name === srcSel)) srcSel = null;
    } catch (e) {
      status.sayError(e.message);
    }
  }
  $effect(() => {
    void sourcesJob.finishedCount;
    refresh();
  });

  function selectSource(name) {
    srcSel = srcSel === name ? null : name;
    if (srcSel) {
      status.say(`${name} cable selected — press sync on the front of the podium`);
    }
  }

  function connect(entry) {
    if (entry.needs_client_registration) {
      status.say(`${entry.name} needs app credentials — opening the classic sources view`);
      uiMode.set("classic");
      return;
    }
    if (sourcesJob.running) {
      status.say("one thing at a time — a source job is already running");
      return;
    }
    sourcesJob.clear();
    status.say(`connecting ${entry.name} — consent opens in your browser`);
    sourcesJob.start(`Connecting ${entry.name}`, "connect", () =>
      api.sourcesConnect({ name: entry.name })
    );
  }

  const syncPress = makePress();
  function pressSync() {
    if (!srcSel) {
      status.say("select a cable first — click a plugged wire or pick it on the screen");
      return;
    }
    if (!apiMode) {
      status.say("sync runs on the cloud backend — switch in settings");
      return;
    }
    if (sourcesJob.running) {
      status.say("one thing at a time — a source job is already running");
      return;
    }
    const name = srcSel;
    syncPress.fire();
    sourcesJob.clear();
    status.say(`syncing ${name} — pulling what's new`);
    if (!saidSharedNote && graphsStore.active?.kind && graphsStore.active.kind !== "private") {
      saidSharedNote = true;
      setTimeout(() => status.say(`syncing ${name} — heads up: sources land in my graph for now`), 1500);
    }
    sourcesJob.start(`Syncing ${name}`, "sync", () => api.sourcesSync(name, {}));
  }

  $effect(() => {
    if (!wrapEl || !screenEl || !syncLblEl) return;

    const shell = createSceneShell(wrapEl, { fov: 40 });
    const { scene, cssScene, camera } = shell;
    camera.position.set(0, 11.5, 0.2);
    camera.up.set(0, 0, -1);
    camera.lookAt(0, 0, 0.2);

    scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 0.6);
    sun.position.set(4, 10, 6);
    scene.add(sun);

    const edgeMat = new THREE.LineBasicMaterial();
    const podMat = new THREE.MeshLambertMaterial();
    const pod = edgedBox(8, TOP, 5, podMat, edgeMat, { translateY: TOP / 2 });
    scene.add(pod);

    // central recessed screen (the CSS3D surface floats just above it)
    const scrMat = new THREE.MeshBasicMaterial();
    const scr = edgedPlane(5.9, 3.3, scrMat, edgeMat);
    scr.rotation.x = -Math.PI / 2;
    scr.position.set(0, TOP + 0.006, 0);
    scene.add(scr);

    const scrObj = new CSS3DObject(screenEl);
    scrObj.rotation.x = -Math.PI / 2;
    scrObj.scale.setScalar(0.01);
    scrObj.position.set(0, TOP + 0.012, 0);
    cssScene.add(scrObj);

    // jack holes
    const ringGeo = new THREE.RingGeometry(0.09, 0.15, 24);
    const holeGeo = new THREE.CircleGeometry(0.09, 24);
    const holeMat = new THREE.MeshBasicMaterial();
    const ringMat = new THREE.MeshBasicMaterial();
    for (const [hx, hz] of HOLES) {
      const hole = new THREE.Mesh(holeGeo, holeMat);
      hole.rotation.x = -Math.PI / 2;
      hole.position.set(hx, TOP + 0.008, hz);
      scene.add(hole);
      const ring = new THREE.Mesh(ringGeo, ringMat);
      ring.rotation.x = -Math.PI / 2;
      ring.position.set(hx, TOP + 0.009, hz);
      scene.add(ring);
    }

    // sync button on the front face, its label flat on the top face
    const btnMat = new THREE.MeshLambertMaterial();
    const btnGeo = new THREE.BoxGeometry(1.3, 0.55, 0.3).translate(0, 0, 0.15);
    const syncBtn = new THREE.Mesh(btnGeo, btnMat);
    syncBtn.add(new THREE.LineSegments(new THREE.EdgesGeometry(btnGeo), edgeMat));
    syncBtn.position.set(0, 0.75, 2.5);
    scene.add(syncBtn);
    const syncLbl = new CSS3DObject(syncLblEl);
    syncLbl.rotation.x = -Math.PI / 2;
    syncLbl.scale.setScalar(0.01);
    cssScene.add(syncLbl);

    let theme = readSpatialTheme();
    function applyTheme() {
      theme = readSpatialTheme();
      scene.background = theme.bg.clone();
      edgeMat.color.copy(theme.border);
      podMat.color.copy(theme.machine);
      scrMat.color.copy(theme.panel2);
      holeMat.color.copy(theme.panel2);
      ringMat.color.copy(theme.muted);
    }
    applyTheme();
    const unwatch = watchScheme(applyTheme);

    // ---- wires: one tube per connected source, keyed by name ----
    const wires = {};
    function mkWire(name, idx) {
      const [hx, hz] = HOLES[idx % HOLES.length];
      const side = Math.abs(hx) > Math.abs(hz);
      const dir = side ? (hx < 0 ? -1 : 1) : (hz < 0 ? -1 : 1);
      const pts = side
        ? [
            new THREE.Vector3(hx, TOP + 0.02, hz),
            new THREE.Vector3(hx + dir * 0.55, TOP + 0.4, hz),
            new THREE.Vector3(dir * 4.4, 0.5, hz + 0.15),
            new THREE.Vector3(dir * 9, 0.03, hz + 0.45),
            new THREE.Vector3(dir * 24, 0.03, hz + 1.1),
          ]
        : [
            new THREE.Vector3(hx, TOP + 0.02, hz),
            new THREE.Vector3(hx, TOP + 0.4, hz + dir * 0.55),
            new THREE.Vector3(hx + 0.15, 0.5, dir * 2.9),
            new THREE.Vector3(hx + 0.45, 0.03, dir * 6.5),
            new THREE.Vector3(hx + 1.1, 0.03, dir * 18),
          ];
      const curve = new THREE.CatmullRomCurve3(pts);
      const mat = new THREE.MeshLambertMaterial();
      const tubeGeo = new THREE.TubeGeometry(curve, 32, 0.04, 8);
      const tube = new THREE.Mesh(tubeGeo, mat);
      tube.userData.srcName = name;
      scene.add(tube);
      const glowMat = new THREE.MeshBasicMaterial();
      glowMat.color.copy(theme.accent);
      const glow = new THREE.Mesh(new THREE.SphereGeometry(0.07, 10, 8), glowMat);
      glow.visible = false;
      scene.add(glow);
      const lblEl = document.createElement("div");
      lblEl.className = "wire-label";
      lblEl.textContent = name;
      const lbl = new CSS3DObject(lblEl);
      lbl.rotation.x = -Math.PI / 2;
      lbl.scale.setScalar(0.009);
      lbl.position.set(hx, TOP + 0.01, hz + 0.32);
      cssScene.add(lbl);
      return {
        tube,
        mat,
        glow,
        glowMat,
        curve,
        lbl,
        lblEl,
        removeFromScene() {
          scene.remove(tube);
          scene.remove(glow);
          cssScene.remove(lbl);
          tubeGeo.dispose();
          mat.dispose();
          glow.geometry.dispose();
          glowMat.dispose();
          lblEl.remove();
        },
      };
    }

    // ---- interaction ----
    function pickable() {
      return [syncBtn, ...Object.values(wires).map((w) => w.tube)];
    }
    function onClick(e) {
      const h = shell.pick(e, pickable());
      if (!h) return;
      if (h.object === syncBtn) return pressSync();
      selectSource(h.object.userData.srcName);
    }
    function onMove(e) {
      shell.canvas.style.cursor = shell.pick(e, pickable()) ? "pointer" : "";
    }
    wrapEl.addEventListener("click", onClick);
    wrapEl.addEventListener("pointermove", onMove);

    shell.start((dt, t) => {
      // diff wires against the connected list (stable name-sorted hole order)
      const sorted = [...sources].sort((a, b) => a.name.localeCompare(b.name));
      sorted.forEach((s, i) => {
        if (!wires[s.name]) wires[s.name] = mkWire(s.name, i);
      });
      for (const name of Object.keys(wires)) {
        if (!sorted.some((s) => s.name === name)) {
          wires[name].removeFromScene();
          delete wires[name];
          continue;
        }
        const w = wires[name];
        const sel = srcSel === name;
        if (syncingName === name) {
          const p = pulse(t);
          w.mat.color.copy(theme.control).lerp(theme.accent, 0.35 + 0.45 * p);
          w.mat.emissive.setScalar(0.1 + 0.2 * p);
          w.glow.visible = true;
          w.glow.position.copy(w.curve.getPoint((t * 0.45) % 1));
        } else {
          w.mat.color.copy(sel ? theme.border : theme.control);
          w.mat.emissive.setScalar(sel ? 0.15 : 0);
          w.glow.visible = false;
        }
        w.lblEl.classList.toggle("sel", sel);
      }

      const pressed = syncPress.active;
      syncBtn.position.z = pressed ? 2.4 : 2.5;
      btnMat.color.copy(
        pressed ? theme.border : syncingName ? theme.raised : theme.control
      );
      syncLblEl.classList.toggle("pressed", pressed);
      syncLbl.position.set(0, 1.035, syncBtn.position.z + 0.15);
    });

    return () => {
      unwatch();
      wrapEl.removeEventListener("click", onClick);
      wrapEl.removeEventListener("pointermove", onMove);
      for (const name of Object.keys(wires)) wires[name].removeFromScene();
      shell.dispose();
    };
  });
</script>

<div class="spatial-wrap" bind:this={wrapEl}>
  <div class="spatial-caption">
    sources — every connected source is a cable; select one and press sync
  </div>
</div>

<div class="css3d-pool">
  <div
    bind:this={screenEl}
    class="src-screen"
    onpointerdown={(e) => e.stopPropagation()}
  >
    <div class="src-head">
      <span class="screen-label">sources</span>
      <input type="text" placeholder="search sources" bind:value={search} />
    </div>
    <div class="src-rows">
      {#each shownConnected as s (s.name)}
        <button
          class="src-row"
          class:sel={srcSel === s.name}
          onclick={() => selectSource(s.name)}
        >
          <span>{s.name}</span>
          <span class="src-sub">
            {#if syncingName === s.name}syncing…{:else if plugging === s.name}connecting…{:else}plugged{#if s.last_synced_at}
                · synced {fmtAgo(s.last_synced_at)}{:else}
                · never synced{/if}{/if}
          </span>
        </button>
      {/each}
      {#each shownCatalog as entry (entry.name)}
        <div class="src-row dashed">
          <span class="src-sub">{entry.name}</span>
          <button class="src-connect" onclick={() => connect(entry)}>
            {plugging === entry.name
              ? "connecting…"
              : entry.needs_client_registration
                ? "needs setup"
                : "connect"}
          </button>
        </div>
      {/each}
      {#if !shownConnected.length && !shownCatalog.length}
        <div class="src-empty">no sources match “{search}”</div>
      {/if}
    </div>
  </div>
  <!-- stopPropagation: the wrap's click handler raycasts the same 3D button -->
  <button
    bind:this={syncLblEl}
    class="btn-label"
    onclick={(e) => {
      e.stopPropagation();
      pressSync();
    }}>sync</button
  >
</div>

<style>
  .src-screen {
    width: 580px;
    height: 320px;
    box-sizing: border-box;
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    pointer-events: auto;
    font-family: var(--font-mono);
    color: var(--text);
  }
  .src-head {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .src-head input {
    flex: 1;
    font: inherit;
    font-size: 11px;
    padding: 4px 8px;
    background: var(--panel);
    border: 1px solid var(--muted);
    color: var(--text);
    outline: none;
  }
  .screen-label {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .src-rows {
    display: flex;
    flex-direction: column;
    gap: 4px;
    overflow: auto;
    flex: 1;
  }
  .src-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 3px 6px;
    font: inherit;
    font-size: 11px;
    text-align: left;
    color: var(--text);
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--muted) 45%, transparent);
    cursor: pointer;
  }
  .src-row.sel {
    border-color: var(--border);
  }
  .src-row.dashed {
    border-style: dashed;
    cursor: default;
  }
  .src-sub {
    color: var(--muted);
  }
  .src-connect {
    border: none;
    background: transparent;
    padding: 0;
    font: inherit;
    font-size: 11px;
    color: var(--text);
    text-decoration: underline;
    cursor: pointer;
  }
  .src-empty {
    color: var(--muted);
    font-size: 11px;
    padding: 3px 6px;
  }
</style>
