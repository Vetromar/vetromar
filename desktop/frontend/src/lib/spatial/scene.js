// Shared scaffold for the spatial machine scenes: WebGL renderer + optional
// CSS3D layer, resize handling, one RAF loop, raycaster picking, and deep
// disposal. Cameras, geometry and interactions stay in each scene — the
// tuned per-scene values ARE the design spec.
import * as THREE from "three";
import { CSS3DRenderer } from "three/addons/renderers/CSS3DRenderer.js";

export function createSceneShell(wrapEl, { fov = 40, near = 0.1, far = 100, cssLayer = true } = {}) {
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "position:absolute;inset:0;display:block;width:100%;height:100%";
  wrapEl.appendChild(canvas);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  let css = null;
  let cssScene = null;
  if (cssLayer) {
    css = new CSS3DRenderer();
    // The layer itself never eats events; interactive screens opt back in
    // with pointer-events:auto on their root element.
    css.domElement.style.cssText = "position:absolute;inset:0;pointer-events:none";
    wrapEl.appendChild(css.domElement);
    cssScene = new THREE.Scene();
  }

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(fov, 1, near, far);

  const ro = new ResizeObserver(() => {
    const w = wrapEl.clientWidth;
    const h = wrapEl.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    css?.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  });
  ro.observe(wrapEl);

  const clock = new THREE.Clock();
  let raf = 0;
  function start(frameFn) {
    const loop = () => {
      const dt = Math.min(clock.getDelta(), 0.1);
      frameFn(dt, clock.elapsedTime);
      renderer.render(scene, camera);
      if (css) css.render(cssScene, camera);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
  }

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  function toNdc(e) {
    const r = wrapEl.getBoundingClientRect();
    ndc.set(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -(((e.clientY - r.top) / r.height) * 2 - 1)
    );
    return ndc;
  }
  /** First intersect of the pointer event against `objects`, or null. */
  function pick(e, objects) {
    raycaster.setFromCamera(toNdc(e), camera);
    return raycaster.intersectObjects(objects, false)[0] || null;
  }

  function dispose() {
    cancelAnimationFrame(raf);
    ro.disconnect();
    scene.traverse((o) => {
      o.geometry?.dispose?.();
      const m = o.material;
      if (Array.isArray(m)) m.forEach((x) => x?.dispose?.());
      else m?.dispose?.();
    });
    renderer.dispose();
    canvas.remove();
    css?.domElement.remove();
  }

  return { canvas, renderer, css, scene, cssScene, camera, start, toNdc, pick, raycaster, dispose };
}

/** Box mesh with the machines' edge-line outline. Base stays at y=0 when translateY is h/2. */
export function edgedBox(w, h, d, mat, edgeMat, { translateY = 0 } = {}) {
  const geo = new THREE.BoxGeometry(w, h, d);
  if (translateY) geo.translate(0, translateY, 0);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo), edgeMat));
  return mesh;
}

/** Plane mesh with an edge outline (recessed screens, bays, slots). */
export function edgedPlane(w, h, mat, edgeMat) {
  const geo = new THREE.PlaneGeometry(w, h);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo), edgeMat));
  return mesh;
}
