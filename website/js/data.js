// Decorative dataset for the launch-page graph: fake-but-plausible knowledge
// shaped like the real engine's three strata (entities / units / sources &
// evidence). Exactly one node is interactive: the "Get in contact" unit.

export const EMAIL = "leo@vetromar.com";

const NODES = [
  // -- entities (top stratum) --
  { id: "ent-meridian", kind: "entity", label: "Meridian Health", sub: "organization" },
  { id: "ent-pilot", kind: "entity", label: "Q3 pilot", sub: "project" },
  { id: "ent-platform", kind: "entity", label: "Data platform", sub: "product" },
  { id: "ent-sofia", kind: "entity", label: "Sofia Marques", sub: "person" },

  // -- units (middle stratum) --
  { id: "contact", kind: "unit", type: "contact", label: "Get in contact", contact: true },
  { id: "u-decision", kind: "unit", type: "decision", label: "Ship the pilot to two clinics first" },
  { id: "u-claim1", kind: "unit", type: "claim", label: "On-prem is the main blocker for Meridian" },
  { id: "u-claim2", kind: "unit", type: "claim", label: "Ingestion is ready for pilot load" },
  { id: "u-commit", kind: "unit", type: "commitment", label: "Sofia drafts the rollout memo by Friday" },
  { id: "u-question", kind: "unit", type: "question", label: "Do we need on-prem for Meridian?" },
  { id: "u-metric", kind: "unit", type: "metric", label: "Latency p95 down 38%" },

  // -- episodes + evidence (bottom stratum) --
  { id: "ep-sync", kind: "episode", label: "Weekly sync — Jul 14", sub: "meeting" },
  { id: "ep-kickoff", kind: "episode", label: "Meridian kickoff call", sub: "meeting" },

  { id: "ev-1", kind: "evidence", label: "“let's start with two clinics and expand”", sub: "quote", parent: "u-decision" },
  { id: "ev-2", kind: "evidence", label: "“full rollout after the pilot reads clean”", sub: "quote", parent: "u-decision" },
  { id: "ev-3", kind: "evidence", label: "“their infra team was firm on on-prem”", sub: "quote", parent: "u-claim1" },
  { id: "ev-4", kind: "evidence", label: "“ingestion held at 4× pilot volume”", sub: "quote", parent: "u-claim2" },
  { id: "ev-5", kind: "evidence", label: "“I'll have the memo out Friday”", sub: "quote", parent: "u-commit" },
  { id: "ev-6", kind: "evidence", label: "“memo covers clinics, staffing, timeline”", sub: "quote", parent: "u-commit" },
  { id: "ev-7", kind: "evidence", label: "“can we defer the on-prem question?”", sub: "quote", parent: "u-question" },
  { id: "ev-8", kind: "evidence", label: "p95 412ms → 256ms", sub: "datapoint", parent: "u-metric" },
];

const LINKS = [
  // units → entities
  { source: "u-decision", target: "ent-pilot", kind: "about" },
  { source: "u-decision", target: "ent-meridian", kind: "mentions" },
  { source: "u-claim1", target: "ent-meridian", kind: "about" },
  { source: "u-claim2", target: "ent-platform", kind: "about" },
  { source: "u-commit", target: "ent-sofia", kind: "mentions" },
  { source: "u-commit", target: "ent-pilot", kind: "about" },
  { source: "u-question", target: "ent-meridian", kind: "about" },
  { source: "u-metric", target: "ent-platform", kind: "about" },
  { source: "contact", target: "ent-pilot", kind: "about" },

  // entity co-mentions (top stratum structure)
  { source: "ent-meridian", target: "ent-pilot", kind: "co-mentioned" },
  { source: "ent-pilot", target: "ent-sofia", kind: "co-mentioned" },
  { source: "ent-meridian", target: "ent-platform", kind: "co-mentioned" },

  // units → episodes
  { source: "u-decision", target: "ep-sync", kind: "from" },
  { source: "u-claim1", target: "ep-kickoff", kind: "from" },
  { source: "u-claim2", target: "ep-sync", kind: "from" },
  { source: "u-commit", target: "ep-sync", kind: "from" },
  { source: "u-question", target: "ep-kickoff", kind: "from" },
  { source: "u-metric", target: "ep-sync", kind: "from" },
  { source: "contact", target: "ep-sync", kind: "from" },

  // evidence → units
  { source: "ev-1", target: "u-decision", kind: "evidence" },
  { source: "ev-2", target: "u-decision", kind: "evidence" },
  { source: "ev-3", target: "u-claim1", kind: "evidence" },
  { source: "ev-4", target: "u-claim2", kind: "evidence" },
  { source: "ev-5", target: "u-commit", kind: "evidence" },
  { source: "ev-6", target: "u-commit", kind: "evidence" },
  { source: "ev-7", target: "u-question", kind: "evidence" },
  { source: "ev-8", target: "u-metric", kind: "evidence" },
];

/** Degree + adjacency over the raw id-based links (before d3 resolves them). */
export function prepare() {
  const adj = {};
  for (const n of NODES) n.degree = 0;
  const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));
  for (const l of LINKS) {
    byId[l.source].degree++;
    byId[l.target].degree++;
    (adj[l.source] ??= new Set()).add(l.target);
    (adj[l.target] ??= new Set()).add(l.source);
  }
  return { nodes: NODES, links: LINKS.map((l) => ({ ...l })), adj };
}
