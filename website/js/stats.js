/* Download-stats page: live totals from the GitHub releases API, trend from
   the daily snapshot CSV in Vetromar/releases. Both endpoints are public and
   CORS-open; nothing about the visitor is sent anywhere. */

const RELEASES_API = "https://api.github.com/repos/Vetromar/releases/releases?per_page=100";
// ?csv=<relative path> swaps in a same-origin fixture for local testing.
const csvOverride = new URLSearchParams(location.search).get("csv");
const CSV_URL = csvOverride && !csvOverride.includes(":")
  ? csvOverride
  : "https://raw.githubusercontent.com/Vetromar/releases/main/stats/downloads.csv";

const SVG_NS = "http://www.w3.org/2000/svg";
const SERIES = [
  { csvKey: "dmg_downloads", name: "DMG downloads", cls: "dmg" },
  { csvKey: "updater_downloads", name: "Updater fetches", cls: "updater" },
];

const fmt = new Intl.NumberFormat("en-US");

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function fmtDate(iso, withYear) {
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  const opts = { month: "short", day: "numeric", timeZone: "UTC" };
  if (withYear) opts.year = "numeric";
  return date.toLocaleDateString("en-US", opts);
}

/* -- live totals + per-release table -------------------------------------- */

function isDmg(name) {
  return name.endsWith(".dmg");
}

async function loadReleases() {
  const res = await fetch(RELEASES_API, { headers: { Accept: "application/vnd.github+json" } });
  if (!res.ok) throw new Error(`releases API ${res.status}`);
  const releases = await res.json();

  let dmgTotal = 0;
  let updaterTotal = 0;
  const rows = [];
  for (const rel of releases) {
    let dmg = 0;
    let updater = 0;
    for (const asset of rel.assets) {
      if (isDmg(asset.name)) dmg += asset.download_count;
      else if (asset.name === "Vetromar.app.tar.gz") updater += asset.download_count;
    }
    dmgTotal += dmg;
    updaterTotal += updater;
    rows.push({ tag: rel.tag_name, published: rel.published_at, dmg, updater });
  }

  document.getElementById("stat-dmg").textContent = fmt.format(dmgTotal);
  document.getElementById("stat-updater").textContent = fmt.format(updaterTotal);
  document.getElementById("stat-releases").textContent = fmt.format(rows.length);
  document.getElementById("stat-version").textContent = rows[0] ? rows[0].tag : "—";

  const tbody = document.getElementById("release-rows");
  tbody.textContent = "";
  for (const row of rows) {
    const tr = el("tr");
    tr.append(el("td", null, row.tag));
    tr.append(el("td", null, row.published ? fmtDate(row.published.slice(0, 10), true) : "—"));
    tr.append(el("td", "num", fmt.format(row.dmg)));
    tr.append(el("td", "num", fmt.format(row.updater)));
    tbody.append(tr);
  }
}

/* -- trend chart ----------------------------------------------------------- */

function parseCsv(text) {
  const lines = text.trim().split("\n");
  const header = lines.shift().split(",");
  const dateIdx = header.indexOf("date");
  return lines.map((line) => {
    const cells = line.split(",");
    const point = { date: cells[dateIdx] };
    for (const s of SERIES) point[s.csvKey] = Number(cells[header.indexOf(s.csvKey)]) || 0;
    return point;
  });
}

function niceTicks(max) {
  if (max <= 0) return [0, 1];
  const step = [1, 2, 5]
    .map((m) => m * 10 ** Math.floor(Math.log10(Math.max(max / 4, 1))))
    .find((s) => Math.ceil(max / s) <= 5) || 10 ** Math.ceil(Math.log10(max));
  const ticks = [];
  for (let v = 0; v <= Math.ceil(max / step) * step; v += step) ticks.push(v);
  return ticks;
}

function drawChart(points) {
  const wrap = document.getElementById("chart-wrap");
  const tooltip = document.getElementById("chart-tooltip");
  document.getElementById("chart-note").remove();
  document.getElementById("chart-legend").hidden = false;

  const W = 720;
  const H = 300;
  const M = { top: 14, right: 62, bottom: 30, left: 46 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  const xs = points.map((p) => Date.parse(p.date));
  const xMin = xs[0];
  const xSpan = Math.max(xs[xs.length - 1] - xMin, 1);
  const yTicks = niceTicks(Math.max(...points.flatMap((p) => SERIES.map((s) => p[s.csvKey]))));
  const yMax = yTicks[yTicks.length - 1];
  const x = (t) => (points.length === 1 ? M.left + plotW / 2 : M.left + ((t - xMin) / xSpan) * plotW);
  const y = (v) => M.top + plotH - (v / yMax) * plotH;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0" });
  svg.setAttribute("aria-label", "Cumulative downloads per day; values also in the by-release table below");

  for (const v of yTicks) {
    svg.append(svgEl("line", { class: "chart-grid", x1: M.left, x2: W - M.right, y1: y(v), y2: y(v) }));
    const label = svgEl("text", { class: "chart-axis-text", x: M.left - 8, y: y(v) + 3.5, "text-anchor": "end" });
    label.textContent = fmt.format(v);
    svg.append(label);
  }

  const withYear = points[0].date.slice(0, 4) !== points[points.length - 1].date.slice(0, 4);
  const maxXTicks = Math.min(points.length, 6);
  const tickIdxs = [...new Set(
    Array.from({ length: maxXTicks }, (_, i) => Math.round((i * (points.length - 1)) / Math.max(maxXTicks - 1, 1)))
  )];
  for (const i of tickIdxs) {
    const label = svgEl("text", {
      class: "chart-axis-text", x: x(xs[i]), y: H - M.bottom + 18, "text-anchor": "middle",
    });
    label.textContent = fmtDate(points[i].date, withYear);
    svg.append(label);
  }

  const crosshair = svgEl("line", {
    class: "chart-crosshair", y1: M.top, y2: H - M.bottom, x1: 0, x2: 0, visibility: "hidden",
  });
  svg.append(crosshair);

  for (const s of SERIES) {
    if (points.length > 1) {
      const d = points.map((p, i) => `${i ? "L" : "M"}${x(xs[i])},${y(p[s.csvKey])}`).join("");
      svg.append(svgEl("path", { class: `chart-line line-${s.cls}`, d }));
    }
    const last = points[points.length - 1];
    svg.append(svgEl("circle", {
      class: `chart-dot dot-${s.cls}`, cx: x(xs[xs.length - 1]), cy: y(last[s.csvKey]), r: 4,
    }));
  }

  // Direct end labels — only while they don't collide; the legend + tooltip
  // carry identity once the lines converge.
  const last = points[points.length - 1];
  const endYs = SERIES.map((s) => y(last[s.csvKey]));
  const labelable = Math.abs(endYs[0] - endYs[1]) >= 15 ? SERIES : [SERIES[0]];
  for (const s of labelable) {
    const label = svgEl("text", {
      class: "chart-end-label", x: x(xs[xs.length - 1]) + 9, y: y(last[s.csvKey]) + 4,
    });
    label.textContent = fmt.format(last[s.csvKey]);
    svg.append(label);
  }

  const dots = SERIES.map((s) => {
    const dot = svgEl("circle", { class: `chart-dot dot-${s.cls}`, r: 4, visibility: "hidden" });
    svg.append(dot);
    return dot;
  });

  function showIndex(i, clientX) {
    const p = points[i];
    crosshair.setAttribute("x1", x(xs[i]));
    crosshair.setAttribute("x2", x(xs[i]));
    crosshair.setAttribute("visibility", "visible");
    SERIES.forEach((s, si) => {
      dots[si].setAttribute("cx", x(xs[i]));
      dots[si].setAttribute("cy", y(p[s.csvKey]));
      dots[si].setAttribute("visibility", "visible");
    });

    tooltip.textContent = "";
    tooltip.append(el("div", "tt-date", fmtDate(p.date, true)));
    for (const s of SERIES) {
      const row = el("div", "tt-row");
      const key = el("span", `tt-key key-${s.cls}`);
      row.append(key, el("span", "tt-value", fmt.format(p[s.csvKey])), el("span", "tt-name", s.name));
      tooltip.append(row);
    }
    tooltip.hidden = false;

    const cardRect = tooltip.offsetParent.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    const px = clientX !== undefined ? clientX - cardRect.left : wrapRect.left - cardRect.left + (x(xs[i]) / W) * wrapRect.width;
    const flip = px > cardRect.width - tooltip.offsetWidth - 24;
    tooltip.style.left = `${flip ? px - tooltip.offsetWidth - 14 : px + 14}px`;
    tooltip.style.top = `${wrapRect.top - cardRect.top + 10}px`;
  }

  function hide() {
    crosshair.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    tooltip.hidden = true;
  }

  let focusIdx = points.length - 1;
  svg.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const vx = ((ev.clientX - rect.left) / rect.width) * W;
    const t = xMin + ((vx - M.left) / plotW) * xSpan;
    let nearest = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - t) < Math.abs(xs[nearest] - t)) nearest = i;
    focusIdx = nearest;
    showIndex(nearest, ev.clientX);
  });
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => showIndex(focusIdx));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    focusIdx = Math.max(0, Math.min(points.length - 1, focusIdx + (ev.key === "ArrowRight" ? 1 : -1)));
    showIndex(focusIdx);
  });

  wrap.append(svg);
}

async function loadTrend() {
  const res = await fetch(CSV_URL, { cache: "no-cache" });
  if (!res.ok) throw new Error(`csv ${res.status}`);
  const points = parseCsv(await res.text());
  if (!points.length) throw new Error("csv empty");
  drawChart(points);
}

loadReleases().catch(() => {
  document.getElementById("release-rows").textContent = "";
  for (const id of ["stat-dmg", "stat-updater", "stat-releases", "stat-version"]) {
    document.getElementById(id).textContent = "?";
  }
  const tr = el("tr");
  tr.append(el("td", "loading-cell", "Couldn't reach the GitHub API — try again in a minute."));
  tr.firstChild.colSpan = 4;
  document.getElementById("release-rows").append(tr);
});

loadTrend().catch(() => {
  document.getElementById("chart-note").textContent =
    "Trend data isn't available right now — live totals above are still current.";
});
