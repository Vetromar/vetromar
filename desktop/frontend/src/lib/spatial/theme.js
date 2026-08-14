// Token → three.js colors for the spatial machines. One place so every scene
// themes identically in light and dark (the greybox palette maps onto the
// sea-glass tokens; CSS3D screens theme themselves with var(--…) directly).
import * as THREE from "three";

export function readSpatialTheme() {
  const s = getComputedStyle(document.documentElement);
  const c = (name) => new THREE.Color(s.getPropertyValue(name).trim());
  return {
    bg: c("--bg"),
    panel: c("--panel"),
    panel2: c("--panel-2"),
    border: c("--border"),
    muted: c("--muted"),
    text: c("--text"),
    machine: c("--machine"),
    raised: c("--machine-raised"),
    control: c("--machine-control"),
    accent: c("--accent"),
    bad: c("--bad"),
    g: {
      entity: c("--g-entity"),
      decision: c("--g-decision"),
      claim: c("--g-claim"),
      commitment: c("--g-commitment"),
      question: c("--g-question"),
      metric: c("--g-metric"),
      episode: c("--g-episode"),
    },
  };
}

/** Re-apply theme colors when the scheme flips. Returns an unsubscribe. */
export function watchScheme(apply) {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", apply);
  return () => mq.removeEventListener("change", apply);
}
