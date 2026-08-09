// Vetromar launch page: the desktop app's 3D stacked-strata knowledge graph
// (Graph.svelte, M9c) ported to vanilla JS as the site's navigation surface.
// One clickable node — "Get in contact" — everything else is decorative.
//
// Rendering: one-time DOM pool, per-frame attribute updates + depth reorder
// (painter's algorithm), driven by a dirty-flag + requestAnimationFrame. When
// the sim has settled and nothing moves, zero frames render.

import {
  basis,
  project,
  clampPitch,
  axisView,
  angleDelta,
  dot,
  FOV,
} from "./camera3d.js";
import { EMAIL, prepare } from "./data.js";

const { forceSimulation, forceLink, forceManyBody, forceCollide, forceX, forceY } =
  window.d3;

// -- constants shared with the app (Graph.svelte) -----------------------------

const ALT = { entity: 160, unit: 0, episode: -160, evidence: -160 };
const UNIT_COLORS = {
  decision: "#4ade80",
  claim: "#6ea8fe",
  commitment: "#fbbf24",
  question: "#f472b6",
  metric: "#2dd4bf",
};
const LINK_DISTANCE = { evidence: 30, from: 70, mentions: 60, about: 60, "co-mentioned": 100 };
const W = 960; // world-frame center the sim gathers around (as in the app)
const SIM_CY = 300;

function color(n) {
  if (n.kind === "entity") return "#c084fc";
  if (n.kind === "unit") return UNIT_COLORS[n.type] ?? "#9aa3b2";
  if (n.kind === "episode") return "#94a3b8";
  return "#64748b";
}

function radius(n) {
  if (n.contact) return 16;
  if (n.kind === "entity") return 11 + 2.4 * Math.sqrt(n.degree ?? 0);
  if (n.kind === "episode") return 17;
  if (n.kind === "evidence") return 4;
  return 9;
}

function short(text, n = 30) {
  return text.length > n ? text.slice(0, n - 1) + "…" : text;
}

// -- state --------------------------------------------------------------------

const svg = document.getElementById("scene");
const gizmoEl = document.getElementById("gizmo");
const card = document.getElementById("contact-card");

const VP = { w: window.innerWidth, h: window.innerHeight };
const cam = { yaw: 0.35, pitch: -0.5, dist: 1100, target: { x: W / 2, y: 0, z: SIM_CY } };

const { nodes, links } = prepare();
let userMoved = false;
let idle = true;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// -- DOM pool -----------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";
function el(tag, cls) {
  const e = document.createElementNS(SVG_NS, tag);
  if (cls) e.setAttribute("class", cls);
  return e;
}

const linkEls = links.map((l) => {
  const line = el("line", "glink");
  svg.append(line);
  return { l, line };
});

const nodeEls = nodes.map((n) => {
  const g = el("g", "gnode" + (n.contact ? " contact" : ""));
  g.dataset.id = n.id;
  if (n.contact) g.dataset.contact = "1";

  let halo = null;
  let hit = null;
  const circle = el("circle");
  if (n.contact) {
    halo = el("circle", "contact-halo");
    circle.setAttribute("class", "contact-core");
    hit = el("circle", "contact-hit");
    g.append(halo, circle, hit);
  } else {
    circle.setAttribute("fill", color(n));
    if (n.kind === "episode") {
      circle.setAttribute("stroke", color(n));
      circle.setAttribute("stroke-width", "1.6");
    }
    g.append(circle);
  }
  let text = null;
  if (n.contact) {
    text = el("text");
    text.textContent = short(n.label);
    g.append(text);
  }
  svg.append(g);
  return { n, g, circle, halo, hit, text };
});

// -- projection + render ------------------------------------------------------

function world(n) {
  return { x: n.x, y: ALT[n.kind], z: n.y };
}

function fitCamera() {
  if (!nodes.length) return;
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (const n of nodes) {
    if (n.x < minX) minX = n.x;
    if (n.x > maxX) maxX = n.x;
    if (n.y < minZ) minZ = n.y;
    if (n.y > maxZ) maxZ = n.y;
  }
  const c = { x: (minX + maxX) / 2, y: 0, z: (minZ + maxZ) / 2 };
  let r = 0;
  for (const n of nodes) {
    const w = world(n);
    r = Math.max(r, Math.hypot(w.x - c.x, w.y - c.y, w.z - c.z));
  }
  cam.target = c;
  cam.dist = Math.min(3000, Math.max(420, r * 2.2 + 140));
}

function render() {
  const b = basis(cam);
  const ordered = [];

  for (const { l, line } of linkEls) {
    const a = project(world(l.source), b, VP);
    const c = project(world(l.target), b, VP);
    if (!a || !c) {
      line.setAttribute("display", "none");
      continue;
    }
    line.removeAttribute("display");
    line.setAttribute("x1", a.sx);
    line.setAttribute("y1", a.sy);
    line.setAttribute("x2", c.sx);
    line.setAttribute("y2", c.sy);
    line.setAttribute("opacity", l.kind === "evidence" ? 0.3 : 0.5);
    ordered.push({ el: line, depth: (a.depth + c.depth) / 2 + 4 });
  }

  for (const it of nodeEls) {
    const n = it.n;
    const p = project(world(n), b, VP);
    if (!p) {
      it.g.setAttribute("display", "none");
      continue;
    }
    it.g.removeAttribute("display");
    const r = Math.max(1.4, radius(n) * p.scale);
    const fade = Math.max(0.4, Math.min(1, (1.5 * cam.dist) / p.depth - 0.5));
    it.g.setAttribute("transform", `translate(${p.sx},${p.sy})`);
    it.circle.setAttribute("r", r);
    if (!n.contact) {
      it.circle.setAttribute(
        "fill-opacity", (n.kind === "episode" ? 0.25 : 0.92) * fade
      );
    }
    if (it.halo) it.halo.setAttribute("r", r);
    if (it.hit) it.hit.setAttribute("r", r + 12);
    if (it.text) it.text.setAttribute("y", r + 11);
    ordered.push({ el: it.g, depth: p.depth });
  }

  ordered.sort((a, b2) => b2.depth - a.depth);
  for (const o of ordered) svg.appendChild(o.el); // reorder: farthest first

  renderGizmo(b);
  document.body.dataset.ready = "1";
}

let framePending = false;
function markDirty() {
  if (framePending) return;
  framePending = true;
  requestAnimationFrame(() => {
    framePending = false;
    render();
  });
}

// -- physics ------------------------------------------------------------------

const contactNode = nodes.find((n) => n.contact);
for (const n of nodes) {
  n.x = W / 2 + (Math.random() - 0.5) * 80;
  n.y = SIM_CY + (Math.random() - 0.5) * 80;
}
contactNode.fx = W / 2;
contactNode.fy = SIM_CY;

forceSimulation(nodes)
  .force(
    "link",
    forceLink(links).id((n) => n.id).distance((l) => LINK_DISTANCE[l.kind] ?? 80)
  )
  .force("charge", forceManyBody().strength((n) => (n.kind === "evidence" ? -20 : -140)))
  .force("collide", forceCollide().radius((n) => radius(n) + 6))
  .force("x", forceX(W / 2).strength(0.06))
  .force("y", forceY(SIM_CY).strength(0.06))
  .on("tick", () => {
    if (!userMoved) fitCamera();
    markDirty();
  });

// -- camera controls ----------------------------------------------------------

let drag = null; // {type:'orbit'|'pan', last:{x,y}, moved, startTarget}
const pointers = new Map();
let pinchDist = null;

function toView(e) {
  const rect = svg.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function interact() {
  userMoved = true;
  idle = false;
}

svg.addEventListener("pointerdown", (e) => {
  interact();
  closeCard();
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinchDist = Math.hypot(a.x - b.x, a.y - b.y);
    drag = null;
    return;
  }
  drag = {
    type: e.shiftKey ? "pan" : "orbit",
    last: toView(e),
    moved: 0,
    startTarget: e.target,
  };
  svg.setPointerCapture(e.pointerId);
});

svg.addEventListener("pointermove", (e) => {
  if (pointers.has(e.pointerId)) {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  }
  if (pinchDist !== null && pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    if (d > 0) {
      cam.dist = Math.min(3500, Math.max(250, (cam.dist * pinchDist) / d));
      pinchDist = d;
      markDirty();
    }
    return;
  }
  if (!drag) return;
  const v = toView(e);
  const dx = v.x - drag.last.x;
  const dy = v.y - drag.last.y;
  drag.moved += Math.abs(dx) + Math.abs(dy);
  drag.last = v;
  if (drag.type === "orbit") {
    cam.yaw -= dx * 0.005;
    cam.pitch = clampPitch(cam.pitch + dy * 0.005);
  } else {
    const b = basis(cam);
    const s = VP.h / 2 / Math.tan(FOV / 2) / cam.dist; // px per world unit at target
    cam.target = {
      x: cam.target.x - (b.r.x * dx) / s + (b.u.x * dy) / s,
      y: cam.target.y - (b.r.y * dx) / s + (b.u.y * dy) / s,
      z: cam.target.z - (b.r.z * dx) / s + (b.u.z * dy) / s,
    };
  }
  markDirty();
});

function endPointer(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinchDist = null;
  if (!drag) return;
  if (drag.moved < 4 && drag.startTarget.closest?.("[data-contact]")) openCard();
  drag = null;
}
svg.addEventListener("pointerup", endPointer);
svg.addEventListener("pointercancel", endPointer);

svg.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    interact();
    cam.dist = Math.min(3500, Math.max(250, cam.dist * Math.exp(e.deltaY * 0.0015)));
    markDirty();
  },
  { passive: false }
);

// -- contact card -------------------------------------------------------------

const copyBtn = document.getElementById("copy-email");

function openCard() {
  card.hidden = false;
}
function closeCard() {
  card.hidden = true;
}

document.getElementById("card-close").addEventListener("click", closeCard);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCard();
});

copyBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(EMAIL);
    copyBtn.textContent = "Copied ✓";
    setTimeout(() => (copyBtn.textContent = "Copy address"), 1500);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("contact-mail"));
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
});

// -- axis gizmo ---------------------------------------------------------------

const GIZMO_AXES = [
  { id: "+x", v: { x: 1, y: 0, z: 0 }, color: "#f87171", label: "X" },
  { id: "+y", v: { x: 0, y: 1, z: 0 }, color: "#4ade80", label: "Y" },
  { id: "+z", v: { x: 0, y: 0, z: 1 }, color: "#6ea8fe", label: "Z" },
  { id: "-x", v: { x: -1, y: 0, z: 0 }, color: "#f87171" },
  { id: "-y", v: { x: 0, y: -1, z: 0 }, color: "#4ade80" },
  { id: "-z", v: { x: 0, y: 0, z: -1 }, color: "#6ea8fe" },
];

const gizmoBg = el("circle", "gizmo-bg");
gizmoBg.setAttribute("cx", 46);
gizmoBg.setAttribute("cy", 46);
gizmoBg.setAttribute("r", 44);
gizmoEl.append(gizmoBg);

const gizmoLines = el("g");
gizmoEl.append(gizmoLines);

const gizmoAxes = GIZMO_AXES.map((a) => {
  let line = null;
  if (a.label) {
    line = el("line");
    line.setAttribute("x1", 46);
    line.setAttribute("y1", 46);
    line.setAttribute("stroke", a.color);
    line.setAttribute("stroke-width", "1.8");
    gizmoLines.append(line);
  }
  const g = el("g");
  g.style.cursor = "pointer";
  if (a.label) {
    const ball = el("circle");
    ball.setAttribute("r", 8.5);
    ball.setAttribute("fill", a.color);
    const lbl = el("text", "gizmo-label");
    lbl.textContent = a.label;
    g.append(ball, lbl);
  } else {
    const ring = el("circle");
    ring.setAttribute("r", 6);
    ring.setAttribute("fill", "none");
    ring.setAttribute("stroke", a.color);
    ring.setAttribute("stroke-width", "1.5");
    const hit = el("circle");
    hit.setAttribute("r", 6);
    hit.setAttribute("fill", "var(--panel)");
    hit.setAttribute("opacity", "0.01");
    g.append(ring, hit);
  }
  g.addEventListener("pointerdown", (e) => e.stopPropagation());
  g.addEventListener("click", () => snapView(a.id));
  gizmoEl.append(g);
  return { a, g, line };
});

function renderGizmo(b) {
  const projected = gizmoAxes
    .map((it) => ({
      ...it,
      x: 46 + 32 * dot(it.a.v, b.r),
      y: 46 - 32 * dot(it.a.v, b.u),
      depth: dot(it.a.v, b.f),
    }))
    .sort((p, q) => q.depth - p.depth); // far first
  for (const it of projected) {
    if (it.line) {
      it.line.setAttribute("x2", it.x);
      it.line.setAttribute("y2", it.y);
      it.line.setAttribute("opacity", it.depth > 0 ? 0.55 : 0.95);
    }
    const [main] = it.g.children;
    main.setAttribute("cx", it.x);
    main.setAttribute("cy", it.y);
    if (it.a.label) {
      main.setAttribute("opacity", it.depth > 0 ? 0.5 : 1);
      it.g.children[1].setAttribute("x", it.x);
      it.g.children[1].setAttribute("y", it.y + 3.2);
    } else {
      main.setAttribute("opacity", it.depth > 0 ? 0.4 : 0.85);
      it.g.children[1].setAttribute("cx", it.x);
      it.g.children[1].setAttribute("cy", it.y);
    }
    gizmoEl.appendChild(it.g); // reorder: nearest painted last
  }
}

function snapView(id) {
  interact();
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
    markDirty();
    if (q < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

let gizmoDrag = null;
gizmoEl.addEventListener("pointerdown", (e) => {
  interact();
  gizmoDrag = { lastX: e.clientX, lastY: e.clientY };
  gizmoEl.setPointerCapture(e.pointerId);
});
gizmoEl.addEventListener("pointermove", (e) => {
  if (!gizmoDrag) return;
  cam.yaw -= (e.clientX - gizmoDrag.lastX) * 0.01;
  cam.pitch = clampPitch(cam.pitch + (e.clientY - gizmoDrag.lastY) * 0.01);
  gizmoDrag.lastX = e.clientX;
  gizmoDrag.lastY = e.clientY;
  markDirty();
});
gizmoEl.addEventListener("pointerup", () => (gizmoDrag = null));
gizmoEl.addEventListener("pointercancel", () => (gizmoDrag = null));

// -- viewport + idle motion ---------------------------------------------------

function resize() {
  VP.w = window.innerWidth;
  VP.h = window.innerHeight;
  svg.setAttribute("viewBox", `0 0 ${VP.w} ${VP.h}`);
  if (!userMoved) fitCamera();
  markDirty();
}
window.addEventListener("resize", resize);
resize();

if (!reducedMotion) {
  let lastT = null;
  function spin(t) {
    if (!idle) return;
    if (lastT !== null && !document.hidden) {
      cam.yaw += ((t - lastT) / 1000) * 0.06;
      markDirty();
    }
    lastT = t;
    requestAnimationFrame(spin);
  }
  requestAnimationFrame(spin);
}
