// Which UI the machine views render as: the spatial 3D "machines" or the
// classic 2D tabs. Module scope + localStorage for the same reason as
// graphs.svelte.js — the choice must survive tab switches and restarts.
// Home is spatial in both modes (HomeCity), Settings always classic.

const STORAGE_KEY = "vetromar.uiMode";

let value = $state("spatial");

try {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === "classic" || saved === "spatial") value = saved;
} catch {
  // storage unavailable — session-only choice
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, value);
  } catch {}
}

export const uiMode = {
  get spatial() {
    return value === "spatial";
  },
  get value() {
    return value;
  },
  set(v) {
    value = v === "spatial" ? "spatial" : "classic";
    persist();
  },
  toggle() {
    this.set(value === "spatial" ? "classic" : "spatial");
  },
};
