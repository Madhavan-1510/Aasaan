// Prerequisite graph: chapters as nodes, prerequisites as edges, coloured by state. Layered layout (no dependencies).
import { esc } from "./util.js";

export const STATE_LABEL = { not_started: "Not started", in_progress: "In progress", done: "Done", needs_review: "Needs review", generating: "Being prepared", failed: "Failed" };
const W = 132, H = 40, GX = 18, GY = 40, PAD = 16, MAX_PER_ROW = 3;

export function layout(chapters, edges) {
  const byId = new Map(chapters.map((c) => [c.id, c]));
  const prereqs = new Map(chapters.map((c) => [c.id, c.prereqs.filter((p) => byId.has(p))]));
  const depth = new Map();
  const d = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0;  // cycles cannot happen (validated server-side) but never loop forever
    seen.add(id);
    const v = prereqs.get(id).length ? 1 + Math.max(...prereqs.get(id).map((p) => d(p, seen))) : 0;
    depth.set(id, v);
    return v;
  };
  chapters.forEach((c) => d(c.id));
  const layers = [];
  chapters.forEach((c) => { (layers[depth.get(c.id)] ||= []).push(c); });
  // wide layers wrap into sub-rows so the graph stays narrow enough for the side panel
  const rows = [];
  layers.forEach((layer) => { for (let i = 0; i < layer.length; i += MAX_PER_ROW) rows.push(layer.slice(i, i + MAX_PER_ROW)); });
  const widest = Math.max(...rows.map((l) => l.length));
  const width = PAD * 2 + widest * W + (widest - 1) * GX;
  const pos = new Map();
  rows.forEach((layer, row) => {
    const rowW = layer.length * W + (layer.length - 1) * GX;
    layer.forEach((c, i) => pos.set(c.id, { x: (width - rowW) / 2 + i * (W + GX), y: PAD + row * (H + GY) }));
  });
  return { pos, width, height: PAD * 2 + rows.length * H + (rows.length - 1) * GY };
}

export function renderGraph(nb, currentId) {
  const { chapters, edges } = nb;
  if (!chapters.length) return "";
  const { pos, width, height } = layout(chapters, edges);
  const paths = edges.filter(([a, b]) => pos.has(a) && pos.has(b)).map(([a, b]) => {
    const p = pos.get(a), q = pos.get(b), x1 = p.x + W / 2, y1 = p.y + H, x2 = q.x + W / 2, y2 = q.y, my = (y1 + y2) / 2;
    return `<path class="edge" marker-end="url(#arr)" d="M${x1} ${y1} C${x1} ${my} ${x2} ${my} ${x2} ${y2}" />`;
  }).join("");
  const nodes = chapters.map((c, i) => {
    const p = pos.get(c.id), title = c.title.length > 20 ? c.title.slice(0, 19) + "…" : c.title;
    return `<g class="gnode n-${c.state}${c.id === currentId ? " current" : ""}" data-id="${c.id}" tabindex="0" role="button"
        aria-label="${esc(c.title)}: ${STATE_LABEL[c.state]}" transform="translate(${p.x} ${p.y})">
      <title>${esc(c.title)} (${STATE_LABEL[c.state]})</title><rect width="${W}" height="${H}" rx="10"/>
      <text x="${W / 2}" y="${H / 2 + 4}" text-anchor="middle">${esc(title)}</text></g>`;
  }).join("");
  return `<svg class="graph" viewBox="0 0 ${width} ${height}" role="group" aria-label="Chapter prerequisite graph">
    <defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" class="arrow"/></marker></defs>
    ${paths}${nodes}</svg>`;
}

export function legend() {
  return Object.entries(STATE_LABEL).map(([k, v]) => `<span class="lg"><i class="sw n-${k}"></i>${v}</span>`).join("");
}
