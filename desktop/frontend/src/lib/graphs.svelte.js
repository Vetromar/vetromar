// The graph switcher's state: which graphs exist and which one the app is
// looking at. Module scope for the same reason as jobs.svelte.js — the
// active graph must survive tab switches. api.js carries the active graph
// into every scoped call; App.svelte remounts the view on switch so every
// tab re-fetches against the new graph.
import { api, setApiGraph } from "../api.js";

const STORAGE_KEY = "vetromar.activeGraph";

let list = $state([{ id: "private", name: "My graph", kind: "private" }]);
let activeId = $state("private");

try {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {
    activeId = saved;
    setApiGraph(saved);
  }
} catch {
  // storage unavailable (private-mode webview) — session-only switching
}

function setActive(id) {
  activeId = id;
  setApiGraph(id);
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {}
}

export const graphsStore = {
  get list() {
    return list;
  },
  get activeId() {
    return activeId;
  },
  get active() {
    return list.find((g) => g.id === activeId) || list[0];
  },
  setActive,

  async refresh() {
    list = await api.graphsList();
    // The active graph can disappear underneath us (removed on disk, another
    // device) — never leave the app pointed at a graph that isn't listed.
    if (!list.some((g) => g.id === activeId)) setActive("private");
  },

  async create(name) {
    const created = await api.graphsCreate(name);
    await this.refresh();
    setActive(created.id);
    return created;
  },

  async remove(id, deleteFiles = false) {
    await api.graphsRemove(id, deleteFiles);
    await this.refresh();
  },
};
