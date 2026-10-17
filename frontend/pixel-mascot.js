// PixelMascot: Aasaan the teacher as 48x48 pixel art (frontend/mascot/pixel-mascot-rows.json).
// The silhouette is found by flood-filling from the four edges through empty cells: reached cells are outside (transparent),
// everything else is the figure (white interior pixels included). Colours come from the theme tokens --mascot-ink / -fill / -shadow.
//   logoSvg()                 small static SVG for the header and chat (cropped to the figure)
//   mountHero(host)           animated canvas sticker for the landing page; returns { destroy() }
let DATA = { rows: [] };
try { DATA = await (await fetch("/static/mascot/pixel-mascot-rows.json")).json(); } catch { /* the page still works without the mascot */ }

const ROWS = DATA.rows || [];
const N = ROWS.length || 48;
const INK = 1, FILL = 2, OUT = 0;
const GRID = (() => {                       // N x N of OUT | INK | FILL
  const g = ROWS.map((r) => [...r].map((ch) => (ch === "1" ? INK : FILL)));   // provisional: empty = FILL
  const outside = new Set();
  const stack = [];
  const push = (r, c) => { if (r >= 0 && c >= 0 && r < N && c < N && ROWS[r][c] !== "1" && !outside.has(r * N + c)) { outside.add(r * N + c); stack.push([r, c]); } };
  for (let i = 0; i < N; i++) { push(0, i); push(N - 1, i); push(i, 0); push(i, N - 1); }
  while (stack.length) { const [r, c] = stack.pop(); push(r + 1, c); push(r - 1, c); push(r, c + 1); push(r, c - 1); }
  for (const k of outside) g[Math.floor(k / N)][k % N] = OUT;
  return g;
})();
const BOX = (() => {                        // bounding box of the figure
  let r0 = N, r1 = -1, c0 = N, c1 = -1;
  GRID.forEach((row, r) => row.forEach((v, c) => { if (v) { r0 = Math.min(r0, r); r1 = Math.max(r1, r); c0 = Math.min(c0, c); c1 = Math.max(c1, c); } }));
  return { r0, r1, c0, c1 };
})();

/** Rectangles of one colour as an SVG path (horizontal runs per row). */
function runs(kind) {
  let d = "";
  for (let r = BOX.r0; r <= BOX.r1; r++) {
    let c = BOX.c0;
    while (c <= BOX.c1) {
      if (GRID[r][c] === kind) { const s = c; while (c <= BOX.c1 && GRID[r][c] === kind) c++; d += `M${s - BOX.c0} ${r - BOX.r0}h${c - s}v1h${-(c - s)}z`; } else c++;
    }
  }
  return d;
}

/** Static header/chat mark: figure only, no frame, no circle, no ring. Empty alt: the wordmark (or the chat name) is beside it. */
export function logoSvg(cls = "") {
  if (!ROWS.length) return "";
  const w = BOX.c1 - BOX.c0 + 1, h = BOX.r1 - BOX.r0 + 1;
  return `<svg class="pm-logo ${cls}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" shape-rendering="crispEdges" aria-hidden="true" focusable="false"><path fill="var(--mascot-fill)" d="${runs(FILL)}"/><path fill="var(--mascot-ink)" d="${runs(INK)}"/></svg>`;
}

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/** The big animated sticker. 4 px cells (3 px on narrow screens). */
export function mountHero(host) {
  if (!ROWS.length) return { destroy() {} };
  host.classList.add("pm-hero");
  host.setAttribute("role", "img");
  host.setAttribute("aria-label", "Aasaan, the teacher mascot");
  const shadowCv = document.createElement("canvas"), figCv = document.createElement("canvas");
  shadowCv.className = "pm-shadow"; figCv.className = "pm-fig";
  for (const cv of [shadowCv, figCv]) cv.setAttribute("aria-hidden", "true");
  host.append(shadowCv, figCv);
  const still = reduced();
  let cell = 4, dpr = 1, colors = {}, raf = 0, visible = true, destroyed = false;
  const born = new Float32Array(N * N);      // ms after mount at which a cell appears
  const flipAt = new Float32Array(N * N).fill(-1e9);   // ms (absolute) at which a ripple reached the cell
  const t0 = performance.now();
  for (let i = 0; i < N * N; i++) born[i] = still ? 0 : Math.random() * 900 + (Math.floor(i / N) / N) * 300;   // shuffled, drifting down the figure
  const FLIP_MS = 600;

  const readColors = () => {
    const cs = getComputedStyle(host);
    colors = { ink: cs.getPropertyValue("--mascot-ink").trim() || "#111", fill: cs.getPropertyValue("--mascot-fill").trim() || "#fff", shadow: cs.getPropertyValue("--mascot-shadow").trim() || "rgba(0,0,0,.22)" };
  };
  const size = () => {
    cell = innerWidth < 480 ? 3 : 4;
    dpr = Math.max(1, Math.round(devicePixelRatio || 1));          // an integer device scale keeps every cell crisp
    for (const cv of [shadowCv, figCv]) { cv.width = N * cell * dpr; cv.height = N * cell * dpr; cv.style.width = cv.style.height = `${N * cell}px`; }
  };
  const draw = (now) => {
    const age = now - t0;
    const px = cell * dpr;
    const sc = shadowCv.getContext("2d"), fc = figCv.getContext("2d");
    sc.clearRect(0, 0, shadowCv.width, shadowCv.height); fc.clearRect(0, 0, figCv.width, figCv.height);
    sc.fillStyle = colors.shadow;
    let busy = age < 1300;
    for (let r = 0; r < N; r++) for (let c = 0; c < N; c++) {
      const v = GRID[r][c];
      if (!v || age < born[r * N + c]) continue;
      let kind = v;
      const since = now - flipAt[r * N + c];
      if (since >= 0 && since < FLIP_MS) { kind = v === INK ? FILL : INK; busy = true; }
      sc.fillRect((c * cell + 2) * dpr, (r * cell + 3) * dpr, px, px);        // the sticker lift: 2 px right, 3 px down
      fc.fillStyle = kind === INK ? colors.ink : colors.fill;
      fc.fillRect(c * px, r * px, px, px);
    }
    return busy;
  };
  const frame = (now) => {
    raf = 0;
    if (destroyed) return;
    if (draw(now) && visible && !document.hidden) raf = requestAnimationFrame(frame);
  };
  const kick = () => { if (!raf && !destroyed) raf = requestAnimationFrame(frame); };

  // ripple: a click flips the cells around it, spreading outward, each for 600 ms, then they restore themselves
  const onClick = (e) => {
    if (still) return;
    const b = figCv.getBoundingClientRect();
    const cx = (e.clientX - b.left) / cell, cy = (e.clientY - b.top) / cell, now = performance.now();
    for (let r = 0; r < N; r++) for (let c = 0; c < N; c++) {
      const d = Math.hypot(c + 0.5 - cx, r + 0.5 - cy);
      if (d <= 7) flipAt[r * N + c] = now + d * 25;
    }
    kick();
  };
  figCv.addEventListener("pointerdown", onClick);

  const sync = () => { host.classList.toggle("pm-paused", !visible || document.hidden); if (visible && !document.hidden) kick(); };
  const io = "IntersectionObserver" in window ? new IntersectionObserver((es) => { visible = es[es.length - 1].isIntersecting; sync(); }) : null;
  io?.observe(host);
  document.addEventListener("visibilitychange", sync);
  const mo = new MutationObserver(() => { readColors(); kick(); if (still) draw(performance.now()); });   // theme switch: recolour in place
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const onResize = () => { size(); draw(performance.now()); kick(); };
  addEventListener("resize", onResize);

  host.classList.toggle("pm-still", still);
  readColors(); size(); draw(t0);
  if (!still) kick(); else { draw(performance.now()); }
  return {
    destroy() { destroyed = true; cancelAnimationFrame(raf); io?.disconnect(); mo.disconnect(); document.removeEventListener("visibilitychange", sync); removeEventListener("resize", onResize); host.replaceChildren(); },
  };
}
