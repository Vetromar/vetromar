<script>
  // Graphs — the server. Every graph is a cartridge seated in the tower's
  // bays; the side panel screen manages them (activate, sync, join by invite,
  // host mode). Clicking an empty bay creates a graph; a joined graph's
  // cartridge slides in and pulses through its first sync.
  import * as THREE from "three";
  import { CSS3DObject } from "three/addons/renderers/CSS3DRenderer.js";
  import { createSceneShell, edgedBox } from "./scene.js";
  import { readSpatialTheme, watchScheme } from "./theme.js";
  import { pulse, cubicOut } from "./anim.js";
  import { api } from "../../api.js";
  import { workspaceJob } from "../jobs.svelte.js";
  import { graphsStore } from "../graphs.svelte.js";
  import { status } from "./status.svelte.js";
  import { uiMode } from "./mode.svelte.js";

  const BAYS = 6;
  const TX = -1.4; // tower x
  const FRONT = 1.2; // tower front face z
  const SEAT_Z = 0.6; // seated cartridge z
  const bayY = (i) => 5.25 - i * 0.78;

  let wrapEl = $state(null);
  let screenEl = $state(null);

  // -- panel state ------------------------------------------------------------
  let newGraphOpen = $state(false);
  let newGraphName = $state("");
  let newGraphInput = $state(null);
  let inviteUrl = $state("");
  let inviteHandle = $state(localStorage.getItem("vetromar.handle") || "");
  let inviteDisplay = $state("");
  let reading = $state(false);
  let host = $state(null);
  let advertiseChoice = $state("");
  let syncingId = $state(null); // graph id whose cartridge pulses
  let quarantine = $state({});
  let saidOverflow = false;

  async function refreshHost() {
    try {
      host = await api.hostStatus();
      if (host.advertise_url_set) advertiseChoice = host.advertise_url;
    } catch (e) {
      status.sayError(e.message);
    }
  }
  async function refreshCounts() {
    try {
      const rows = await api.graphsList(true);
      quarantine = Object.fromEntries(rows.map((g) => [g.id, g.quarantine_count ?? 0]));
    } catch {}
  }
  $effect(() => {
    void workspaceJob.finishedCount;
    graphsStore.refresh().catch(() => {});
    refreshCounts();
    if (syncingId && !workspaceJob.running) {
      const g = graphsStore.list.find((x) => x.id === syncingId);
      if (g && !workspaceJob.err) {
        const n = quarantine[g.id] ?? 0;
        status.say(
          `${g.name} in sync — ${n ? `${n} change${n === 1 ? "" : "s"} quarantined` : "nothing quarantined"}`
        );
      }
      syncingId = null;
    }
  });
  $effect(() => {
    refreshHost();
  });

  function activate(id) {
    const g = graphsStore.list.find((x) => x.id === id);
    if (!g) return;
    graphsStore.setActive(id);
    status.say(`active graph → ${g.name}`);
  }

  function openNewGraph() {
    newGraphOpen = true;
    newGraphName = `graph ${graphsStore.list.length + 1}`;
    status.say("name the new graph on the side panel, then press enter");
    setTimeout(() => newGraphInput?.select(), 50);
  }

  async function createGraph() {
    const name = newGraphName.trim();
    if (!name) return;
    try {
      await graphsStore.create(name);
      newGraphOpen = false;
      newGraphName = "";
      status.say("new graph cartridge slotted in");
    } catch (e) {
      status.sayError(e.message);
    }
  }

  const canSync = (g) => g.kind !== "private" && g.host_url && g.workspace_id;

  async function syncGraph(g) {
    if (syncingId || workspaceJob.running) {
      status.say("a graph sync is already running");
      return;
    }
    try {
      status.say(`syncing ${g.name} with its host…`);
      const { job_id } = await api.graphSync(g.id);
      syncingId = g.id;
      workspaceJob.attach(job_id, `Sync ${g.name}`, "workspace-sync");
    } catch (e) {
      status.sayError(e.message);
    }
  }

  async function insertInvite() {
    if (reading) return;
    const url = inviteUrl.trim();
    if (!url) {
      status.say("the card reader needs an invite link first");
      return;
    }
    const handle = inviteHandle.trim();
    if (!handle) {
      status.say("pick a handle — the name you'll go by in that graph");
      return;
    }
    reading = true;
    status.say("reading invite card…");
    try {
      const joined = await api.graphsJoin({
        invite_url: url,
        handle,
        display_name: inviteDisplay.trim(),
      });
      try {
        localStorage.setItem("vetromar.handle", handle);
      } catch {}
      inviteUrl = "";
      await graphsStore.refresh();
      graphsStore.setActive(joined.id);
      if (joined.sync_job_id) {
        syncingId = joined.id;
        workspaceJob.attach(joined.sync_job_id, "First sync", "workspace-sync");
      }
      status.say("invite accepted — new machine slid into the rack, first sync running");
    } catch (e) {
      status.sayError(e.message);
    } finally {
      reading = false;
    }
  }

  async function toggleHost() {
    if (!host) return;
    try {
      host = await api.hostConfigure({ enabled: !host.enabled });
      status.say(
        host.enabled
          ? `hosting on — serving at ${host.advertise_url || `port ${host.port}`}`
          : "hosting off"
      );
    } catch (e) {
      status.sayError(e.message);
    }
  }

  async function saveAdvertise() {
    if (!advertiseChoice) return;
    try {
      host = await api.hostConfigure({ advertise_url: advertiseChoice });
      status.say(`members connect via ${host.advertise_url}`);
    } catch (e) {
      status.sayError(e.message);
    }
  }

  function toClassic() {
    status.say("opening the classic graphs view — members, invites and vps live there");
    uiMode.set("classic");
  }

  $effect(() => {
    if (!wrapEl || !screenEl) return;

    const shell = createSceneShell(wrapEl, { fov: 40 });
    const { scene, cssScene, camera } = shell;
    camera.position.set(0, 3.2, 10.8);
    camera.lookAt(0.5, 3.1, 0);

    scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 0.6);
    sun.position.set(4, 10, 6);
    scene.add(sun);

    const grid = new THREE.GridHelper(60, 60);
    grid.position.y = 0.002;
    scene.add(grid);

    const edgeMat = new THREE.LineBasicMaterial();
    const dimEdgeMat = new THREE.LineBasicMaterial({ transparent: true, opacity: 0.5 });
    const towerMat = new THREE.MeshLambertMaterial();
    const tower = edgedBox(4.2, 6.2, 2.4, towerMat, edgeMat, { translateY: 3.1 });
    tower.position.x = TX;
    scene.add(tower);

    const pwrMat = new THREE.MeshBasicMaterial();
    const pwr = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 10), pwrMat);
    pwr.position.set(TX + 1.7, 5.85, FRONT + 0.02);
    scene.add(pwr);

    // bays
    const bayGeo = new THREE.PlaneGeometry(3.2, 0.6);
    const bayMat = new THREE.MeshBasicMaterial();
    const bayPlanes = [];
    for (let i = 0; i < BAYS; i++) {
      const p = new THREE.Mesh(bayGeo, bayMat);
      p.position.set(TX, bayY(i), FRONT + 0.006);
      p.userData.bayIdx = i;
      p.add(new THREE.LineSegments(new THREE.EdgesGeometry(bayGeo), dimEdgeMat));
      scene.add(p);
      bayPlanes.push(p);
    }

    // side panel: arm + leg + panel body, screen square to the camera
    const PX = 3.45, PY = 3.3, PZ = 0.9;
    const ctrlMat = new THREE.MeshLambertMaterial();
    const arm = edgedBox(1.7, 0.16, 0.16, ctrlMat, dimEdgeMat);
    arm.geometry.translate(0.85, 0, 0);
    arm.position.set(TX + 2.1, 5.05, 0.45);
    scene.add(arm);
    const leg = edgedBox(0.14, 1.1, 0.14, ctrlMat, dimEdgeMat, { translateY: 0.55 });
    leg.position.set(PX, 0, PZ);
    scene.add(leg);
    const panelMat = new THREE.MeshLambertMaterial();
    const panel = edgedBox(3.6, 4.4, 0.14, panelMat, edgeMat);
    panel.position.set(PX, PY, PZ);
    scene.add(panel);

    const scrObj = new CSS3DObject(screenEl);
    scrObj.scale.setScalar(0.006);
    scrObj.position.set(PX, PY, PZ + 0.09);
    cssScene.add(scrObj);

    let theme = readSpatialTheme();
    function applyTheme() {
      theme = readSpatialTheme();
      scene.background = theme.bg.clone();
      edgeMat.color.copy(theme.border);
      dimEdgeMat.color.copy(theme.muted);
      towerMat.color.copy(theme.machine);
      panelMat.color.copy(theme.machine);
      ctrlMat.color.copy(theme.control);
      bayMat.color.copy(theme.panel2);
      grid.material.color.copy(theme.muted);
      grid.material.opacity = 0.25;
      grid.material.transparent = true;
    }
    applyTheme();
    const unwatch = watchScheme(applyTheme);

    // ---- cartridges, one per graph, diffed against the registry ----
    const carts = {};
    const cartGeo = new THREE.BoxGeometry(3.0, 0.5, 1.8);
    const lampGeo = new THREE.BoxGeometry(0.14, 0.14, 0.03);
    const handleGeo = new THREE.BoxGeometry(0.5, 0.1, 0.12);
    const builtAt = performance.now();
    function mkCart(id, i, animate) {
      const mat = new THREE.MeshLambertMaterial();
      mat.color.copy(theme.raised);
      const body = new THREE.Mesh(cartGeo, mat);
      body.userData.graphId = id;
      body.add(new THREE.LineSegments(new THREE.EdgesGeometry(cartGeo), edgeMat));
      const lampMat = new THREE.MeshBasicMaterial();
      const lamp = new THREE.Mesh(lampGeo, lampMat);
      lamp.position.set(-1.25, 0, 0.91);
      body.add(lamp);
      const handleMat = new THREE.MeshLambertMaterial();
      handleMat.color.copy(theme.control);
      const handle = new THREE.Mesh(handleGeo, handleMat);
      handle.position.set(1.1, 0, 0.95);
      body.add(handle);
      body.position.set(TX, bayY(i), animate ? SEAT_Z + 3.6 : SEAT_Z);
      scene.add(body);
      return {
        body,
        mat,
        lampMat,
        handleMat,
        anim: animate ? performance.now() : 0,
        removeFromScene() {
          scene.remove(body);
          mat.dispose();
          lampMat.dispose();
          handleMat.dispose();
        },
      };
    }

    // ---- interaction ----
    function pickable() {
      const racked = graphsStore.list.slice(0, BAYS).length;
      return [
        ...Object.values(carts).map((c) => c.body),
        ...bayPlanes.filter((p) => p.userData.bayIdx >= racked),
      ];
    }
    function onClick(e) {
      const h = shell.pick(e, pickable());
      if (!h) return;
      if (h.object.userData.graphId) activate(h.object.userData.graphId);
      else openNewGraph();
    }
    function onMove(e) {
      shell.canvas.style.cursor = shell.pick(e, pickable()) ? "pointer" : "";
    }
    wrapEl.addEventListener("click", onClick);
    wrapEl.addEventListener("pointermove", onMove);

    shell.start((dt, t) => {
      const list = graphsStore.list;
      if (list.length > BAYS && !saidOverflow) {
        saidOverflow = true;
        status.say("the rack shows the first six graphs — the side panel lists everything");
      }
      list.slice(0, BAYS).forEach((g, i) => {
        if (!carts[g.id]) carts[g.id] = mkCart(g.id, i, performance.now() - builtAt > 800);
      });
      for (const id of Object.keys(carts)) {
        const idx = list.findIndex((g) => g.id === id);
        const c = carts[id];
        if (idx < 0 || idx >= BAYS) {
          c.removeFromScene();
          delete carts[id];
          continue;
        }
        c.body.position.y = bayY(idx);
        if (c.anim) {
          const p = Math.min(1, (performance.now() - c.anim) / 700);
          c.body.position.z = SEAT_Z + 3.6 * (1 - cubicOut(p));
          if (p >= 1) c.anim = 0;
        }
        c.lampMat.color.copy(graphsStore.activeId === id ? theme.accent : theme.control);
        if (syncingId === id && workspaceJob.running) {
          c.mat.emissive.setScalar(0.12 + 0.15 * pulse(t));
        } else {
          c.mat.emissive.setScalar(0);
        }
      }
      pwrMat.color.copy(host?.enabled ? theme.accent : theme.control);
    });

    return () => {
      unwatch();
      wrapEl.removeEventListener("click", onClick);
      wrapEl.removeEventListener("pointermove", onMove);
      for (const id of Object.keys(carts)) carts[id].removeFromScene();
      cartGeo.dispose();
      lampGeo.dispose();
      handleGeo.dispose();
      shell.dispose();
    };
  });
</script>

<div class="spatial-wrap" bind:this={wrapEl}>
  <div class="spatial-caption">
    graphs — each cartridge is a graph; click one to make it active, click an empty bay
    for a new one
  </div>
</div>

<div class="css3d-pool">
  <div
    bind:this={screenEl}
    class="g-screen"
    onpointerdown={(e) => e.stopPropagation()}
  >
    <div class="g-sec">side panel — settings</div>
    <hr />
    <div class="g-sec">graphs</div>
    <div class="g-rows">
      {#each graphsStore.list as g (g.id)}
        <div class="g-row" class:active={graphsStore.activeId === g.id}>
          <button class="g-name" onclick={() => activate(g.id)}>
            {g.name}
            <span class="g-kind">· {g.kind}</span>
          </button>
          {#if graphsStore.activeId === g.id}<span class="g-tag">active</span>{/if}
          {#if quarantine[g.id]}<span class="g-quar">{quarantine[g.id]} quarantined</span>{/if}
          {#if canSync(g)}
            <button class="g-sync" onclick={() => syncGraph(g)}>
              {syncingId === g.id && workspaceJob.running ? "syncing…" : "sync"}
            </button>
          {/if}
        </div>
      {/each}
      {#if newGraphOpen}
        <div class="g-row">
          <input
            bind:this={newGraphInput}
            type="text"
            bind:value={newGraphName}
            onkeydown={(e) => {
              if (e.key === "Enter") createGraph();
              if (e.key === "Escape") newGraphOpen = false;
            }}
          />
          <button class="g-sync" onclick={createGraph}>create</button>
        </div>
      {:else}
        <button class="g-new" onclick={openNewGraph}>+ new graph</button>
      {/if}
    </div>
    <hr />
    <div class="g-sec">invite card reader</div>
    <input type="text" placeholder="paste an invite link" bind:value={inviteUrl}
      onkeydown={(e) => e.key === "Enter" && insertInvite()} />
    {#if inviteUrl.trim()}
      <div class="g-join-extra">
        <input type="text" placeholder="your handle" bind:value={inviteHandle}
          onkeydown={(e) => e.key === "Enter" && insertInvite()} />
        <input type="text" placeholder="display name (optional)" bind:value={inviteDisplay}
          onkeydown={(e) => e.key === "Enter" && insertInvite()} />
      </div>
    {/if}
    <button class="g-insert" onclick={insertInvite}>
      {reading ? "reading…" : "insert"}
    </button>
    <hr />
    <button class="g-host" onclick={toggleHost}>
      <span class="g-knob" class:on={host?.enabled}><span></span></span>
      host mode — {host?.enabled ? "on" : "off"}
    </button>
    <div class="g-note">
      {host?.enabled
        ? `this machine serves shared graphs — friends sync with it${host?.running ? ` (port ${host.port})` : ""}`
        : "turn on to host shared graphs on this machine, like a game server"}
    </div>
    {#if host?.enabled && !host.advertise_url_set && host.candidates?.length}
      <div class="g-join-extra">
        <select bind:value={advertiseChoice}>
          <option value="">address members connect to…</option>
          {#each host.candidates as c (c.url)}
            <option value={c.url}>{c.url} ({c.kind})</option>
          {/each}
        </select>
        <button class="g-sync" onclick={saveAdvertise}>use</button>
      </div>
    {/if}
    <button class="g-more" onclick={toClassic}>members & more — classic view</button>
  </div>
</div>

<style>
  .g-screen {
    width: 560px;
    height: 680px;
    box-sizing: border-box;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    background: var(--panel-2);
    border: 1px solid var(--muted);
    font-family: var(--font-mono);
    font-size: 15px;
    color: var(--text);
    pointer-events: auto;
    overflow: hidden;
  }
  .g-screen hr {
    border: none;
    border-top: 1px solid color-mix(in srgb, var(--muted) 40%, transparent);
    margin: 0;
    width: 100%;
  }
  .g-sec {
    font-size: 12px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .g-rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
    overflow: auto;
    max-height: 220px;
  }
  .g-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 7px 10px;
    border: 1px solid color-mix(in srgb, var(--muted) 45%, transparent);
  }
  .g-row.active {
    border-color: var(--border);
  }
  .g-name {
    flex: 1;
    text-align: left;
    border: none;
    background: transparent;
    padding: 0;
    font: inherit;
    color: var(--text);
    cursor: pointer;
  }
  .g-kind {
    color: var(--muted);
    font-size: 12px;
  }
  .g-tag {
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .g-quar {
    font-size: 11px;
    color: var(--bad);
  }
  .g-sync {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text);
    padding: 3px 10px;
    font: inherit;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    cursor: pointer;
  }
  .g-new {
    border: 1px dashed color-mix(in srgb, var(--muted) 60%, transparent);
    background: transparent;
    color: var(--muted);
    padding: 6px 10px;
    font: inherit;
    font-size: 12px;
    text-align: left;
    cursor: pointer;
  }
  .g-screen input,
  .g-screen select {
    font: inherit;
    font-size: 14px;
    background: var(--panel);
    border: 1px solid var(--muted);
    color: var(--text);
    padding: 8px 10px;
    width: 100%;
    box-sizing: border-box;
  }
  .g-join-extra {
    display: flex;
    gap: 8px;
  }
  .g-insert {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text);
    padding: 8px 12px;
    font: inherit;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    cursor: pointer;
  }
  .g-host {
    display: flex;
    align-items: center;
    gap: 10px;
    border: none;
    background: transparent;
    padding: 0;
    font: inherit;
    color: var(--text);
    cursor: pointer;
    text-align: left;
  }
  .g-knob {
    width: 34px;
    height: 18px;
    border: 2px solid var(--border);
    background: var(--panel);
    position: relative;
    flex: none;
    display: inline-block;
  }
  .g-knob span {
    position: absolute;
    top: 0;
    left: 0;
    width: 14px;
    height: 14px;
    background: var(--muted);
  }
  .g-knob.on span {
    left: 16px;
    background: var(--accent);
  }
  .g-note {
    font-size: 12px;
    color: var(--muted);
  }
  .g-more {
    margin-top: auto;
    border: none;
    background: transparent;
    padding: 0;
    font: inherit;
    font-size: 12px;
    color: var(--muted);
    text-decoration: underline;
    cursor: pointer;
    text-align: left;
  }
</style>
