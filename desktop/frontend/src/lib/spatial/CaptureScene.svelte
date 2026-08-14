<script>
  // Capture — the podium desk. Drop a file anywhere on it and the item sinks
  // into the desk while the real ingestion job runs; a recessed screen takes
  // quick notes; a physical push button records live. Geometry and camera come
  // from the greybox handoff; colors come from the sea-glass tokens.
  import { untrack } from "svelte";
  import * as THREE from "three";
  import { CSS3DObject } from "three/addons/renderers/CSS3DRenderer.js";
  import { createSceneShell, edgedBox, edgedPlane } from "./scene.js";
  import { readSpatialTheme, watchScheme } from "./theme.js";
  import { makePress, pulse } from "./anim.js";
  import { api } from "../../api.js";
  import { captureJob, documentJob } from "../jobs.svelte.js";
  import { graphsStore } from "../graphs.svelte.js";
  import { status } from "./status.svelte.js";

  const TOP = 1.4; // podium height — desk surface lives at y=TOP

  const AUDIO_EXT = new Set(["wav", "mp3", "m4a", "flac", "ogg", "aac", "mp4"]);

  let wrapEl = $state(null);
  let noteEl = $state(null);
  let saveLblEl = $state(null);
  let recLblEl = $state(null);
  let fileInput = $state(null);
  let noteText = $state("");
  let dragOver = $state(false);

  const activeGraph = $derived(graphsStore.active);
  const sharedActive = $derived(activeGraph && activeGraph.kind !== "private");

  // Meeting detection — same poll as the classic tab, surfaced as a chip.
  let meeting = $state(null);
  $effect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await api.meetingsStatus();
        if (alive) meeting = s;
      } catch {
        if (alive) meeting = null;
      }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  });
  const meetingDetected = $derived(
    meeting?.state === "detected" && meeting?.candidate && !captureJob.running
  );

  // ---- desk items (imperative scene objects; one per in-flight job) ----
  // { id, label, mode: "capture"|"document"|"note", dx, dz, pct, phase,
  //   resolved, errText, fadeAt, removeAt, obj: {slot, box, cardObj, card…} }
  let items = [];
  let itemN = 0;
  let hasItems = $state(false); // template-visible mirror of items.length
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const randX = () => -0.3 + Math.random() * 3.4;
  const randZ = () => -1.6 + Math.random() * 2.2;

  const savePress = makePress();
  const recPress = makePress();

  function trackerFor(mode) {
    return mode === "capture" ? captureJob : mode === "document" ? documentJob : null;
  }

  function spawnItem(label, mode, dx, dz, pct = 0) {
    const item = {
      id: "i" + ++itemN,
      label,
      mode,
      dx: clamp(typeof dx === "number" ? dx : randX(), -0.55, 3.3),
      dz: clamp(typeof dz === "number" ? dz : randZ(), -1.7, 0.9),
      pct,
      phase: "processing",
      resolved: false,
      errText: null,
      fadeAt: 0,
      removeAt: 0,
      obj: null, // created lazily by the frame loop once the scene exists
    };
    items.push(item);
    hasItems = true;
    return item;
  }

  function routeFile(f, dx, dz) {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    const isAudio = AUDIO_EXT.has(ext);
    const tracker = isAudio ? captureJob : documentJob;
    if (tracker.running) {
      status.say("the desk is busy — wait for the current item to sink in");
      return;
    }
    tracker.clear();
    spawnItem(f.name, isAudio ? "capture" : "document", dx, dz);
    status.say(`${f.name.toLowerCase()} landed on the desk — processing`);
    tracker.start(`Processing ${f.name}`, isAudio ? "capture" : "document", () =>
      isAudio ? api.capture(f, "") : api.uploadDocument(f)
    );
  }

  function pressNote() {
    const t = noteText.trim();
    if (!t) {
      status.say("type a note first — then press the button");
      return;
    }
    savePress.fire();
    noteText = "";
    const short = t.length > 18 ? t.slice(0, 17) + "…" : t;
    const item = spawnItem(`note “${short}”`, "note", -1.35 + 1.2, 0.2);
    api
      .graphNote(graphsStore.activeId, t)
      .then((saved) => {
        item.resolved = true;
        status.say(`note saved — “${saved.episode.title.toLowerCase()}”`);
      })
      .catch((e) => {
        item.phase = "error";
        item.errText = e.message;
        status.sayError(e.message);
      });
  }

  function pressRec() {
    const job = captureJob.job;
    if (captureJob.running && job?.status === "recording") {
      recPress.fire();
      api.recordStop(captureJob.jobId).catch((e) => status.sayError(e.message));
      status.say("recording stopped — processing");
    } else if (captureJob.running) {
      status.say("the desk is busy — wait for the current item to sink in");
    } else if (meetingDetected) {
      recPress.fire();
      captureJob.clear();
      api
        .meetingRecord("")
        .then(({ job_id }) => captureJob.attach(job_id, "Meeting recording", "meeting-record"))
        .catch((e) => status.sayError(e.message));
      status.say(`capturing the ${meeting.candidate.name.toLowerCase()} call`);
    } else {
      recPress.fire();
      captureJob.clear();
      captureJob.start("Recording", "record", () => api.recordStart(""));
      status.say("recording started");
    }
  }

  function fmtElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  $effect(() => {
    if (!wrapEl || !noteEl || !saveLblEl || !recLblEl) return;

    const shell = createSceneShell(wrapEl, { fov: 40 });
    const { scene, cssScene, camera } = shell;
    camera.position.set(0, 6.4, 9.2);
    camera.lookAt(0, 0.4, 0);

    scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 0.6);
    sun.position.set(4, 10, 6);
    scene.add(sun);

    const edgeMat = new THREE.LineBasicMaterial();
    const podMat = new THREE.MeshLambertMaterial();
    const pod = edgedBox(8, TOP, 5, podMat, edgeMat, { translateY: TOP / 2 });
    scene.add(pod);

    // note screen recess — the CSS3D note surface sits just above it
    const recessMat = new THREE.MeshBasicMaterial();
    const recess = edgedPlane(2.7, 3.1, recessMat, edgeMat);
    recess.rotation.x = -Math.PI / 2;
    recess.position.set(-2.2, TOP + 0.006, 0.45);
    scene.add(recess);

    const noteObj = new CSS3DObject(noteEl);
    noteObj.rotation.x = -Math.PI / 2;
    noteObj.scale.setScalar(0.01);
    noteObj.position.set(-2.2, TOP + 0.012, -0.05);
    cssScene.add(noteObj);

    const saveMat = new THREE.MeshLambertMaterial();
    const saveBtn = edgedBox(1.1, 0.32, 0.62, saveMat, edgeMat, { translateY: 0.16 });
    saveBtn.position.set(-2.2, TOP, 1.55);
    saveBtn.userData.act = "save";
    scene.add(saveBtn);
    const saveLbl = new CSS3DObject(saveLblEl);
    saveLbl.rotation.x = -Math.PI / 2;
    saveLbl.scale.setScalar(0.01);
    cssScene.add(saveLbl);

    const recMat = new THREE.MeshLambertMaterial();
    const recBtn = edgedBox(1.2, 0.4, 1.2, recMat, edgeMat, { translateY: 0.2 });
    recBtn.position.set(2.9, TOP, 1.5);
    recBtn.userData.act = "rec";
    scene.add(recBtn);
    const recLbl = new CSS3DObject(recLblEl);
    recLbl.rotation.x = -Math.PI / 2;
    recLbl.scale.setScalar(0.01);
    cssScene.add(recLbl);

    let theme = readSpatialTheme();
    function applyTheme() {
      theme = readSpatialTheme();
      scene.background = theme.bg.clone();
      edgeMat.color.copy(theme.border);
      recessMat.color.copy(theme.panel2);
    }
    applyTheme();
    const unwatch = watchScheme(applyTheme);

    // ---- per-item scene objects ----
    const itemGeo = new THREE.BoxGeometry(1.15, 0.26, 0.75).translate(0, 0.13, 0);
    const slotGeo = new THREE.PlaneGeometry(1.35, 0.95);
    function mkItemObj(item) {
      const slotMat = new THREE.MeshBasicMaterial({ transparent: true });
      slotMat.color.copy(theme.panel2);
      const slotEdge = new THREE.LineBasicMaterial({ transparent: true });
      slotEdge.color.copy(theme.border);
      const slot = new THREE.Mesh(slotGeo, slotMat);
      slot.add(new THREE.LineSegments(new THREE.EdgesGeometry(slotGeo), slotEdge));
      slot.rotation.x = -Math.PI / 2;
      slot.position.set(item.dx, TOP + 0.015, item.dz);
      scene.add(slot);

      const boxMat = new THREE.MeshLambertMaterial();
      boxMat.color.copy(theme.raised);
      const box = new THREE.Mesh(itemGeo, boxMat);
      box.add(new THREE.LineSegments(new THREE.EdgesGeometry(itemGeo), edgeMat));
      box.position.set(item.dx, TOP + 0.6, item.dz);
      scene.add(box);

      const card = document.createElement("div");
      card.className = "desk-card";
      card.innerHTML =
        '<div class="dc-head"><span class="dc-label"></span><span class="dc-kind"></span></div>' +
        '<div class="dc-bar"><div class="dc-fill"></div></div>' +
        '<div class="dc-status"></div>';
      card.querySelector(".dc-label").textContent = item.label;
      card.querySelector(".dc-kind").textContent =
        item.mode === "capture" ? "audio" : item.mode;
      const cardObj = new CSS3DObject(card);
      cardObj.rotation.x = -Math.PI / 2;
      cardObj.scale.setScalar(0.008);
      cardObj.position.set(item.dx, TOP + 0.01, item.dz + 0.95);
      cssScene.add(cardObj);

      return {
        slot,
        slotMat,
        slotEdge,
        box,
        boxMat,
        cardObj,
        card,
        fill: card.querySelector(".dc-fill"),
        statusEl: card.querySelector(".dc-status"),
        removeFromScene() {
          scene.remove(slot);
          scene.remove(box);
          cssScene.remove(cardObj);
          slotMat.dispose();
          slotEdge.dispose();
          boxMat.dispose();
          card.remove();
        },
      };
    }

    // A job that was already running when the scene mounted (tab switch,
    // graph switch, tray recording that just stopped) gets its desk item
    // back, mid-sink. untrack: reading tracker runes here must not make the
    // whole scene rebuild on job changes.
    untrack(() => {
      for (const mode of ["capture", "document"]) {
        const tracker = trackerFor(mode);
        if (tracker.running && tracker.job?.status !== "recording") {
          if (!items.some((it) => it.mode === mode)) {
            spawnItem(
              (tracker.label || "item").replace(/^Processing\s+/i, ""),
              mode,
              undefined,
              undefined,
              tracker.job?.percent ?? 0
            );
          }
        }
      }
    });

    // ---- interaction ----
    function hitDesk(e) {
      const h = shell.pick(e, [pod]);
      return h && Math.abs(h.point.y - TOP) < 0.05 ? h.point : null;
    }
    function pickBtn(e) {
      const h = shell.pick(e, [saveBtn, recBtn]);
      return h ? h.object.userData.act : null;
    }
    let pendingDropPoint = null;
    function onClick(e) {
      const act = pickBtn(e);
      if (act === "save") return pressNote();
      if (act === "rec") return pressRec();
      const p = hitDesk(e);
      if (p && p.x > -0.7) {
        // clicking the open desk = choose a file to drop there
        pendingDropPoint = { x: p.x, z: p.z };
        fileInput?.click();
      }
    }
    function onMove(e) {
      const act = pickBtn(e);
      shell.canvas.style.cursor = act ? "pointer" : hitDesk(e) ? "copy" : "";
    }
    function onDragOver(e) {
      e.preventDefault();
      dragOver = true;
    }
    function onDragLeave() {
      dragOver = false;
    }
    function onDrop(e) {
      e.preventDefault();
      dragOver = false;
      const f = e.dataTransfer?.files?.[0];
      if (!f) return;
      const p = hitDesk(e);
      routeFile(f, p?.x, p?.z);
    }
    wrapEl.addEventListener("click", onClick);
    wrapEl.addEventListener("pointermove", onMove);
    wrapEl.addEventListener("dragover", onDragOver);
    wrapEl.addEventListener("dragleave", onDragLeave);
    wrapEl.addEventListener("drop", onDrop);
    const onPick = (e) => {
      const f = e.currentTarget.files?.[0];
      if (f) routeFile(f, pendingDropPoint?.x, pendingDropPoint?.z);
      pendingDropPoint = null;
      e.currentTarget.value = "";
    };
    fileInput.addEventListener("change", onPick);

    let wasRecording = false;
    shell.start((dt) => {
      const recording = captureJob.job?.status === "recording";

      // a recording that just stopped becomes a desk item following the
      // processing half of the same job
      if (wasRecording && !recording && captureJob.running) {
        if (!items.some((it) => it.mode === "capture")) {
          spawnItem("recording", "capture");
        }
      }
      wasRecording = recording;

      // items
      const now = performance.now();
      for (const item of items) {
        const o = item.obj || (item.obj = mkItemObj(item));
        const tracker = trackerFor(item.mode);
        if (item.phase === "processing") {
          if (tracker) {
            if (tracker.running) {
              const p = tracker.job?.percent;
              // no reported percent (early stages) — creep toward 90
              item.pct = p != null ? p : Math.min(90, item.pct + dt * 6);
              const stage = tracker.job?.stage;
              o.statusEl.textContent = stage
                ? `${stage.toLowerCase()} — ${Math.round(item.pct)}%`
                : `sinking in — ${Math.round(item.pct)}%`;
            } else if (tracker.err) {
              item.phase = "error";
              item.errText = tracker.err;
            } else if (tracker.result) {
              item.pct = 100;
              item.phase = "done";
              const n = Array.isArray(tracker.result.units)
                ? tracker.result.units.length
                : tracker.result.units;
              o.statusEl.textContent = `done — ${n ?? 0} unit(s) extracted, filed to knowledge`;
              item.fadeAt = now + 900;
              item.removeAt = now + 1700;
            } else {
              // tracker idle with nothing to show (cancelled) — retire the item
              item.phase = "done";
              o.statusEl.textContent = "stopped";
              item.fadeAt = now + 400;
              item.removeAt = now + 1200;
            }
          } else {
            // note item — near-instant, animate the sink and wait on the promise
            item.pct = Math.min(100, item.pct + dt * 170);
            if (item.pct >= 100 && item.resolved) {
              item.phase = "done";
              o.statusEl.textContent = "done — filed to knowledge";
              item.fadeAt = now + 900;
              item.removeAt = now + 1700;
            } else {
              o.statusEl.textContent = `sinking in — ${Math.round(item.pct)}%`;
            }
          }
        }
        if (item.phase === "error") {
          o.statusEl.textContent = (item.errText || "failed").toLowerCase();
          o.card.classList.add("failed");
          if (!item.removeAt) {
            item.fadeAt = now + 5200;
            item.removeAt = now + 6000;
          }
        }
        o.box.position.y = THREE.MathUtils.damp(
          o.box.position.y,
          TOP + 0.28 - (item.pct / 100) * 0.8,
          5,
          dt
        );
        o.fill.style.width = Math.round(item.pct) + "%";
        const gone = item.fadeAt && now > item.fadeAt;
        o.card.style.opacity = gone ? 0 : 1;
        o.slotMat.opacity = THREE.MathUtils.damp(o.slotMat.opacity, gone ? 0 : 1, 5, dt);
        o.slotEdge.opacity = o.slotMat.opacity;
        o.box.visible = !gone;
      }
      items = items.filter((item) => {
        if (item.removeAt && now > item.removeAt) {
          item.obj?.removeFromScene();
          return false;
        }
        return true;
      });
      if (hasItems !== items.length > 0) hasItems = items.length > 0;

      // podium brightens toward the accent while a drop hovers
      podMat.color.copy(theme.machine);
      if (dragOver) podMat.color.lerp(theme.accent, 0.18);

      // save-note button press
      const savePressed = savePress.active;
      saveBtn.scale.y = THREE.MathUtils.damp(saveBtn.scale.y, savePressed ? 0.28 : 1, 16, dt);
      saveMat.color.copy(savePressed ? theme.border : theme.control);
      saveLblEl.classList.toggle("pressed", savePressed);
      saveLbl.position.set(-2.2, TOP + 0.33 * saveBtn.scale.y, 1.55);

      // record button: depressed + pulsing while recording
      recBtn.scale.y = THREE.MathUtils.damp(
        recBtn.scale.y,
        recording ? 0.4 : recPress.active ? 0.5 : 1,
        16,
        dt
      );
      recMat.color.copy(recPress.active ? theme.border : theme.control);
      recMat.emissive.setScalar(
        recording ? 0.25 + 0.2 * Math.sin(performance.now() / 160) : 0
      );
      recLblEl.textContent = recording
        ? `stop · ${fmtElapsed(Date.now() - (captureJob.startedAt ?? Date.now()))}`
        : "record";
      recLblEl.classList.toggle("pressed", recPress.active);
      recLbl.position.set(2.9, TOP + 0.41 * recBtn.scale.y, 1.5);
    });

    return () => {
      unwatch();
      wrapEl.removeEventListener("click", onClick);
      wrapEl.removeEventListener("pointermove", onMove);
      wrapEl.removeEventListener("dragover", onDragOver);
      wrapEl.removeEventListener("dragleave", onDragLeave);
      wrapEl.removeEventListener("drop", onDrop);
      fileInput?.removeEventListener("change", onPick);
      for (const item of items) {
        item.obj?.removeFromScene();
        item.obj = null;
      }
      itemGeo.dispose();
      slotGeo.dispose();
      shell.dispose();
    };
  });
</script>

<div class="spatial-wrap" bind:this={wrapEl}>
  <div class="spatial-caption">
    the podium — drop a file anywhere on it; the item sinks into the desk as it's processed
    {#if sharedActive}· everything lands in {activeGraph.name.toLowerCase()}{/if}
  </div>
  {#if !hasItems && !dragOver}
    <div class="spatial-hint">drop a file on the podium · or click it to choose one</div>
  {/if}
  {#if dragOver}
    <div class="spatial-hint">drop a file</div>
  {/if}
  {#if meetingDetected}
    <button
      class="spatial-chip"
      onclick={(e) => {
        e.stopPropagation();
        pressRec();
      }}
    >
      {meeting.candidate.name.toLowerCase()} is using the microphone — press record to
      capture the call
    </button>
  {/if}
</div>

<input
  bind:this={fileInput}
  type="file"
  accept=".wav,.mp3,.m4a,.flac,.ogg,.aac,.mp4,.pdf,.docx,.md,.txt,audio/*"
  style="display:none"
/>

<!-- CSS3D surfaces: rendered here, adopted into the 3D css layer on first
     frame. The pool stays hidden so nothing flashes before adoption. -->
<div class="css3d-pool">
  <div
    bind:this={noteEl}
    class="note-screen"
    onpointerdown={(e) => e.stopPropagation()}
  >
    <div class="screen-label">quick note — no ai</div>
    <textarea
      bind:value={noteText}
      placeholder="type, then cmd/ctrl+enter"
      onkeydown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) pressNote();
      }}
    ></textarea>
  </div>
  <!-- stopPropagation: the wrap's click handler raycasts the same 3D button —
       without it a label click fires the action twice -->
  <button
    bind:this={saveLblEl}
    class="btn-label"
    onclick={(e) => {
      e.stopPropagation();
      pressNote();
    }}>save note</button
  >
  <button
    bind:this={recLblEl}
    class="btn-label"
    onclick={(e) => {
      e.stopPropagation();
      pressRec();
    }}>record</button
  >
</div>

<style>
  .note-screen {
    width: 250px;
    height: 200px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    pointer-events: auto;
    font-family: var(--font-mono);
  }
  .note-screen textarea {
    flex: 1;
    width: 100%;
    resize: none;
    font: inherit;
    font-size: 13px;
    padding: 6px;
    background: var(--panel-2);
    border: 1px solid var(--muted);
    color: var(--text);
  }
  .screen-label {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
</style>
