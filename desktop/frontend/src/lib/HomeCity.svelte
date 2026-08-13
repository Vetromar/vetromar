<script>
  // The spatial home screen — a "city" of placeholder prisms, one per
  // destination. Prototype only: geometry, hover-lift and navigation are
  // real; all art/worldbuilding replaces the grey boxes later.
  import * as THREE from "three";

  let { onNavigate } = $props();

  // Placeholder blocks. w/d/h are footprint + height in world units,
  // x/z the ground position — asymmetric on purpose so it reads as a city.
  const PRISMS = [
    { id: "capture", label: "Capture", w: 1.4, d: 1.4, h: 2.6, x: -1.8, z: -0.6 },
    { id: "knowledge", label: "Knowledge", w: 2.6, d: 1.6, h: 1.2, x: 1.4, z: -2.2 },
    { id: "sources", label: "Sources", w: 1.2, d: 2.2, h: 1.7, x: 3.4, z: 0.6 },
    { id: "graphs", label: "Graphs", w: 1.8, d: 1.2, h: 0.9, x: -0.6, z: 1.8 },
    { id: "setup", label: "Settings", w: 0.9, d: 0.9, h: 0.7, x: 1.6, z: 2.6 },
  ];

  const LIFT = 0.5; // how far a hovered prism floats above the ground

  let wrapEl = $state(null);
  let canvasEl = $state(null);
  let labelEls = {};
  let hoveredId = $state(null);
  // Hovering a label counts as hovering its prism; plain (non-rune) because
  // the rAF loop polls it every frame anyway.
  let labelHoverId = null;

  // Scene colors live in app.css tokens so light/dark stay in one place.
  // Prism grey has no token yet (placeholder art), so it's a per-scheme constant.
  function readTheme() {
    const s = getComputedStyle(document.documentElement);
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    return {
      bg: s.getPropertyValue("--bg").trim(),
      ink: s.getPropertyValue("--border").trim(),
      grey: dark ? 0x4a463d : 0xb0aca3,
    };
  }

  $effect(() => {
    if (!wrapEl || !canvasEl) return;

    const renderer = new THREE.WebGLRenderer({ canvas: canvasEl, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
    const CAM_BASE = new THREE.Vector3(7, 6, 10);
    const LOOK_AT = new THREE.Vector3(0, 0.6, 0);
    camera.position.copy(CAM_BASE);
    camera.lookAt(LOOK_AT);

    scene.add(new THREE.AmbientLight(0xffffff, 0.85));
    const sun = new THREE.DirectionalLight(0xffffff, 0.7);
    sun.position.set(5, 10, 4);
    scene.add(sun);

    const groundMat = new THREE.MeshLambertMaterial();
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), groundMat);
    ground.rotation.x = -Math.PI / 2;
    scene.add(ground);

    const prismMat = new THREE.MeshLambertMaterial();
    const edgeMat = new THREE.LineBasicMaterial();
    const meshes = [];
    for (const p of PRISMS) {
      // Base at y=0 so the hover lift is just mesh.position.y.
      const geo = new THREE.BoxGeometry(p.w, p.h, p.d).translate(0, p.h / 2, 0);
      const mesh = new THREE.Mesh(geo, prismMat);
      mesh.position.set(p.x, 0, p.z);
      mesh.userData.id = p.id;
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo), edgeMat);
      mesh.add(edges);
      scene.add(mesh);
      meshes.push(mesh);
    }

    function applyTheme() {
      const t = readTheme();
      scene.background = new THREE.Color(t.bg);
      groundMat.color.set(t.bg);
      prismMat.color.set(t.grey);
      edgeMat.color.set(t.ink);
    }
    applyTheme();
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", applyTheme);

    // Per-frame state stays out of runes — no reason to run Svelte 60x/sec.
    const mouse = new THREE.Vector2(0, 0);
    let mouseActive = false;
    const raycaster = new THREE.Raycaster();
    const clock = new THREE.Clock();
    let raf = 0;

    function toNdc(e) {
      const rect = wrapEl.getBoundingClientRect();
      return {
        x: ((e.clientX - rect.left) / rect.width) * 2 - 1,
        y: -(((e.clientY - rect.top) / rect.height) * 2 - 1),
      };
    }
    function onPointerMove(e) {
      const n = toNdc(e);
      mouse.set(n.x, n.y);
      mouseActive = true;
    }
    function onPointerLeave() {
      mouse.set(0, 0);
      mouseActive = false;
    }
    function pick(ndc) {
      raycaster.setFromCamera(ndc, camera);
      const hit = raycaster.intersectObjects(meshes, false)[0];
      return hit ? hit.object.userData.id : null;
    }
    function onClick(e) {
      const id = pick(toNdc(e));
      if (id) onNavigate?.(id);
    }
    wrapEl.addEventListener("pointermove", onPointerMove);
    wrapEl.addEventListener("pointerleave", onPointerLeave);
    wrapEl.addEventListener("click", onClick);

    const observer = new ResizeObserver(() => {
      const w = wrapEl.clientWidth;
      const h = wrapEl.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    });
    observer.observe(wrapEl);

    const proj = new THREE.Vector3();
    function frame() {
      const dt = Math.min(clock.getDelta(), 0.1);

      const hover = labelHoverId || (mouseActive ? pick(mouse) : null);
      if (hover !== hoveredId) {
        hoveredId = hover;
        canvasEl.style.cursor = hover ? "pointer" : "";
      }

      for (const mesh of meshes) {
        const target = mesh.userData.id === hoveredId ? LIFT : 0;
        mesh.position.y = THREE.MathUtils.damp(mesh.position.y, target, 8, dt);
      }

      // Parallax: the camera drifts a little with the pointer, always
      // looking at the same spot — the composed view never really changes.
      camera.position.x = THREE.MathUtils.damp(camera.position.x, CAM_BASE.x + mouse.x * 0.8, 3, dt);
      camera.position.y = THREE.MathUtils.damp(camera.position.y, CAM_BASE.y - mouse.y * 0.4, 3, dt);
      camera.lookAt(LOOK_AT);

      renderer.render(scene, camera);

      // Labels are DOM (crisp text, themed CSS) pinned above each prism.
      const w = wrapEl.clientWidth;
      const h = wrapEl.clientHeight;
      for (const [i, p] of PRISMS.entries()) {
        const el = labelEls[p.id];
        if (!el) continue;
        proj.set(p.x, meshes[i].position.y + p.h + 0.35, p.z).project(camera);
        const px = ((proj.x + 1) / 2) * w;
        const py = ((1 - proj.y) / 2) * h;
        el.style.transform = `translate(-50%, -100%) translate(${px}px, ${py}px)`;
      }

      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      mq.removeEventListener("change", applyTheme);
      wrapEl.removeEventListener("pointermove", onPointerMove);
      wrapEl.removeEventListener("pointerleave", onPointerLeave);
      wrapEl.removeEventListener("click", onClick);
      for (const mesh of meshes) {
        mesh.geometry.dispose();
        mesh.children[0]?.geometry.dispose();
      }
      ground.geometry.dispose();
      groundMat.dispose();
      prismMat.dispose();
      edgeMat.dispose();
      renderer.dispose();
    };
  });
</script>

<div class="home-wrap" bind:this={wrapEl}>
  <canvas bind:this={canvasEl}></canvas>
  {#each PRISMS as p (p.id)}
    <button
      class="home-label"
      class:hovered={hoveredId === p.id}
      data-prism={p.id}
      bind:this={labelEls[p.id]}
      onclick={(e) => {
        e.stopPropagation();
        onNavigate?.(p.id);
      }}
      onpointerenter={() => (labelHoverId = p.id)}
      onpointerleave={() => (labelHoverId = null)}
    >
      {p.label}
    </button>
  {/each}
</div>

<style>
  .home-wrap {
    position: fixed;
    inset: 0;
    z-index: 1;
  }
  .home-wrap canvas {
    display: block;
    width: 100%;
    height: 100%;
  }
  .home-label {
    position: absolute;
    top: 0;
    left: 0;
    padding: 3px 8px;
    border: none;
    background: transparent;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }
  .home-label.hovered {
    text-decoration: underline;
    text-underline-offset: 3px;
  }
</style>
