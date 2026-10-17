// Aasaan UI: Home -> Loading -> Notebook (roadmap + graph + chapter page). The agents are never shown.
import { esc, store, LANGS, lang, api, ICON, timeAgo, speak, startRecording } from "./util.js";
import { renderMarkdown } from "./markdown.js";
import { renderGraph, legend } from "./graph.js";

const $app = document.getElementById("app");
const $bar = document.getElementById("bar");
const S = { learner: null, session: null, mode: "live", nb: null, chapter: null, files: [], side: store.get("side") || "roadmap",
            slide: {}, quiz: null, poll: null, es: null, companion: "" };
const PLACEHOLDERS = ["What do you want to learn? e.g. I want to learn DSA", "Paste notes, links, or a chapter...", "Attach a textbook photo or PDF..."];
const STATUS_TEXT = { queued: "Queued", drafting: "Writing notes", checking: "Checking facts", ready: "Ready", failed: "Failed" };

// ------------------------------------------------------------------------ shell
function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.setAttribute("role", "alert"); document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 5000);
}
const fail = (e) => toast(e.message || String(e));

function cleanup() {
  clearInterval(S.poll); S.poll = null;
  if (S.es) { S.es.close(); S.es = null; }
}

function topbar(right = "") {
  $bar.innerHTML = `${S.mode === "mock" ? `<div class="demo" role="status">Demo mode: no AI model is connected, so content is a placeholder. Add GEMINI_API_KEY to generate real notebooks.</div>` : ""}
    <div class="bar"><a class="brand" href="#/" aria-label="Aasaan home">${ICON.logo}<span>Aasaan</span><small lang="ta">ஆசான்</small></a><div class="bar-right">${right}</div></div>`;
}

async function init() {
  let id = store.get("learner_id");
  if (!id) { id = (await api("learners", {})).learner_id; store.set("learner_id", id); }
  S.learner = id;
  try { S.session = (await api("session/open", { learner_id: id })).session_id; } catch { /* offline start is fine */ }
  try { S.mode = (await api("health")).mode; } catch { /* ignore */ }
  window.addEventListener("hashchange", route);
  route();
}

function route() {
  cleanup();
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/loading\/(\w+)/))) return loading(m[1]);
  if ((m = h.match(/^#\/nb\/(\w+)(?:\/ch\/(\d+))?/))) return notebook(m[1], m[2] ? Number(m[2]) : null);
  return home();
}

// ------------------------------------------------------------------------- home
async function home() {
  topbar();
  $app.className = "";
  let nbs = [];
  try { nbs = await api("notebooks?learner_id=" + S.learner); } catch { /* none yet */ }
  $app.innerHTML = `<div class="hero"><h1>What do you want to learn today?</h1><p>Learning made aasaan. Type a goal, paste notes or links, or attach a photo or PDF.</p></div>
    <div class="composer" id="chat">
      <div class="att-row" id="atts"></div>
      <textarea id="msg" rows="1" placeholder="${PLACEHOLDERS[0]}" aria-label="Your goal, notes or links"></textarea>
      <div class="toolbar">
        <button class="pill" id="attach" type="button" aria-label="Attach photos or PDFs">${ICON.clip}<span>Attach</span></button>
        <div class="menu-wrap"><button class="pill" id="langbtn" type="button" aria-haspopup="listbox" aria-label="Select language">
          ${ICON.globe}<span id="langlabel">${esc(LANGS[lang()] || "English")}</span>${ICON.chev}</button>
          <div class="menu" id="langmenu" role="listbox" hidden>${Object.entries(LANGS).map(([k, v]) =>
            `<button role="option" data-l="${k}" class="${k === lang() ? "on" : ""}">${esc(v)}</button>`).join("")}</div></div>
        <span class="spacer"></span>
        <button class="iconbtn" id="mic" type="button" aria-label="Speak your goal">${ICON.mic}</button>
        <button class="send" id="send" aria-label="Build my notebook" disabled>${ICON.up}</button>
        <input id="file" type="file" accept="image/*,application/pdf" multiple hidden>
      </div></div>
    <p class="help">Enter to send · Shift+Enter for a new line · drop files on the box</p><div id="err" class="err" role="alert"></div>
    ${nbs.length ? `<h2 class="section">Notebooks</h2><div class="grid">${nbs.map(nbCard).join("")}</div>` : ""}`;
  wireComposer();
  document.querySelectorAll(".nb-card").forEach((b) => b.onclick = () => {
    location.hash = b.dataset.ready === "1" ? `#/nb/${b.dataset.id}` : `#/loading/${b.dataset.id}`;
  });
}

function nbCard(n) {
  const pct = n.progress.total ? Math.round(100 * n.progress.done / n.progress.total) : 0;
  const busy = n.status !== "complete" && n.status !== "failed";
  return `<button class="nb-card" data-id="${n.id}" data-ready="${n.open_ready ? 1 : 0}">
    <div class="nb-title">${esc(n.title)}</div>
    <div class="meter" aria-hidden="true"><i style="width:${pct}%"></i></div>
    <div class="nb-meta"><span>${n.progress.done} of ${n.progress.total} chapters done</span>
      <span>${n.status === "failed" ? "Needs attention" : busy ? `Generating ${n.readiness}%` : timeAgo(n.last_opened)}</span></div></button>`;
}

const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

function wireComposer() {
  const msg = document.getElementById("msg"), send = document.getElementById("send"), box = document.getElementById("chat");
  const file = document.getElementById("file"), menu = document.getElementById("langmenu"), mic = document.getElementById("mic");
  const sync = () => {
    send.disabled = !(msg.value.trim() || S.files.length);
    msg.style.height = "auto"; msg.style.height = Math.min(msg.scrollHeight, 260) + "px";
    document.getElementById("atts").innerHTML = S.files.map((f, i) => f.type.startsWith("image/")
      ? `<div class="att img"><img src="${URL.createObjectURL(f)}" alt="${esc(f.name)}"><button class="x" data-i="${i}" aria-label="Remove ${esc(f.name)}">×</button></div>`
      : `<div class="att file">${ICON.doc}<span class="nm">${esc(f.name)}</span><span class="sz">${fmtSize(f.size)}</span><button class="x" data-i="${i}" aria-label="Remove ${esc(f.name)}">×</button></div>`).join("");
    document.querySelectorAll(".att .x").forEach((b) => b.onclick = () => { S.files.splice(Number(b.dataset.i), 1); sync(); });
  };
  const add = (list) => { S.files = [...S.files, ...list].slice(0, 6); sync(); };
  msg.oninput = sync;
  msg.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!send.disabled) go(); } };
  document.getElementById("attach").onclick = () => file.click();
  file.onchange = () => { add([...file.files]); file.value = ""; };
  ["dragenter", "dragover"].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.remove("drag"); }));
  box.addEventListener("drop", (e) => add([...e.dataTransfer.files]));
  document.getElementById("langbtn").onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  menu.querySelectorAll("button").forEach((b) => b.onclick = () => {
    store.set("lang", b.dataset.l); document.getElementById("langlabel").textContent = LANGS[b.dataset.l];
    menu.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); menu.hidden = true; msg.focus();
  });
  document.addEventListener("click", () => { menu.hidden = true; });
  let i = 0; const ph = setInterval(() => { if (!document.body.contains(msg)) return clearInterval(ph); msg.placeholder = PLACEHOLDERS[++i % PLACEHOLDERS.length]; }, 3500);
  wireMic(mic, (text) => { msg.value = (msg.value ? msg.value + " " : "") + text; sync(); msg.focus(); }, () => lang());
  send.onclick = go; sync(); msg.focus();

  async function go() {
    const f = new FormData();
    f.append("learner_id", S.learner); f.append("language", lang()); f.append("text", msg.value.trim());
    S.files.forEach((x) => f.append("files", x));
    send.disabled = true;
    try {
      const r = await api("notebooks", null, f);
      S.files = [];
      location.hash = `#/loading/${r.notebook_id}`;
    } catch (e) { document.getElementById("err").textContent = e.message; send.disabled = false; }
  }
}

// microphone button: record, convert to WAV, transcribe with the voice service; typed input always stays available
function wireMic(btn, onText, getLang) {
  let rec = null;
  btn.onclick = async () => {
    if (rec) {
      const r = rec; rec = null; btn.classList.remove("rec"); btn.innerHTML = ICON.mic; btn.setAttribute("aria-label", "Speak");
      const wav = await r.stop();
      if (!wav) return toast("Could not read the recording. Please type instead.");
      const f = new FormData(); f.append("language", getLang()); f.append("audio", wav, "speech.wav");
      try {
        const out = await api("voice/stt", null, f);
        if (out.text) onText(out.text); else toast(out.message || "Voice is unavailable right now. Please type instead.");
      } catch (e) { fail(e); }
      return;
    }
    if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Voice input is not supported in this browser. Please type.");
    try { rec = await startRecording(); btn.classList.add("rec"); btn.innerHTML = ICON.stop; btn.setAttribute("aria-label", "Stop recording"); }
    catch { toast("Microphone permission was denied. Please type instead."); }
  };
}

// ---------------------------------------------------------------------- loading
function loading(nid) {
  topbar();
  $app.className = "";
  const dots = Array.from({ length: 9 }, (_, i) => `<i style="--i:${i}"></i>`).join("");
  $app.innerHTML = `<div class="loading"><div class="dots" aria-hidden="true">${dots}</div>
    <h1>Preparing your notebook</h1><p class="muted" id="lmsg" role="status"></p><div id="lact"></div></div>`;
  const msgs = ["Reading what you gave me...", "Planning the chapters...", "Writing the first chapter..."];
  let n = 0;
  const tick = async () => {
    let nb;
    try { nb = await api("notebooks/" + nid); } catch (e) { cleanup(); return fail(e); }
    if (nb.open_ready) { cleanup(); location.hash = `#/nb/${nid}`; return; }
    const msg = document.getElementById("lmsg"); if (!msg) return;
    if (nb.status === "failed") {
      cleanup(); document.querySelector(".dots")?.remove(); msg.textContent = nb.error || "Something went wrong while preparing your notebook.";
      document.getElementById("lact").innerHTML = `<button class="btn" id="retry">Try again</button> <a class="btn ghost" href="#/">Back home</a>`;
      document.getElementById("retry").onclick = async () => { try { await api(`notebooks/${nid}/retry`, {}); loading(nid); } catch (e) { fail(e); } };
      return;
    }
    msg.textContent = nb.chapters.length ? `${nb.chapters.length} chapters planned. Writing the first one...` : msgs[n++ % msgs.length];
  };
  tick(); S.poll = setInterval(tick, 1200);
}

// --------------------------------------------------------------------- notebook
async function notebook(nid, cid) {
  $app.className = "wide";
  topbar(`<button class="linkbtn" id="finish">Finish for today</button>`);
  $app.innerHTML = `<div class="nbhead"><div><h1 id="nbtitle"></h1><p class="muted" id="nbdesc"></p></div><div class="ready" id="ready"></div></div>
    <div id="note"></div>
    <div class="nb"><button class="chaps-toggle" id="chaps" aria-expanded="false">${ICON.list} Chapters</button>
      <aside class="side" id="side" aria-label="Chapters"></aside><section class="main" id="main" aria-live="polite"></section></div>`;
  document.getElementById("finish").onclick = finishSession;
  document.getElementById("chaps").onclick = (e) => { const o = document.querySelector(".nb").classList.toggle("side-open"); e.currentTarget.setAttribute("aria-expanded", o); };
  try { S.nb = await api("notebooks/" + nid); } catch (e) { fail(e); location.hash = "#/"; return; }
  renderHead(); renderSide();
  api(`notebooks/${nid}/companion?learner_id=${S.learner}`).then((c) => { S.companion = c.message; renderNote(); }).catch(() => {});
  const first = cid || S.nb.next_chapter || S.nb.chapters[0]?.id;
  if (first) await loadChapter(first);
  connect(nid);
}

function connect(nid) {
  const apply = (nb) => onSnapshot(nb);
  const poll = () => { if (!S.poll) S.poll = setInterval(async () => { try { apply(await api("notebooks/" + nid)); } catch { /* retry */ } }, 3000); };
  if (window.EventSource) {
    S.es = new EventSource(`/api/notebooks/${nid}/stream`);
    S.es.onmessage = (e) => apply(JSON.parse(e.data));
    S.es.onerror = poll;  // fall back to polling when the stream drops
  } else poll();
}

function onSnapshot(nb) {
  const cur = S.chapter;
  S.nb = nb; renderHead(); renderSide();
  const now = cur && nb.chapters.find((c) => c.id === cur.id);
  if (!now) return;
  const becameReady = cur.status !== "ready" && now.status === "ready";
  const statusChanged = cur.status !== now.status && now.status !== "ready";
  if (becameReady) { if (!(S.quiz && S.quiz.cid === cur.id)) loadChapter(cur.id, true); }
  else if (statusChanged) { cur.status = now.status; cur.error = now.error; renderChapter(); }
}

function renderNote() {
  const n = document.getElementById("note"); if (!n) return;
  n.innerHTML = S.companion ? `<div class="notice">${esc(S.companion)}<button class="x2" aria-label="Dismiss">×</button></div>` : "";
  const x = n.querySelector(".x2"); if (x) x.onclick = () => { S.companion = ""; renderNote(); };
}

function renderHead() {
  const nb = S.nb; if (!document.getElementById("nbtitle")) return;
  document.getElementById("nbtitle").textContent = nb.title;
  document.getElementById("nbdesc").innerHTML = esc(nb.description || "") + ((nb.sources || []).length
    ? `<span class="srcs">Based on: ${nb.sources.map((x) => x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc((x.title || x.url).slice(0, 40))}</a>` : `<span>${esc(x.title.slice(0, 40))}</span>`).join(" · ")}</span>` : "")
    + ((nb.warnings || []).length ? `<span class="srcs warn">${nb.warnings.map(esc).join(" · ")}</span>` : "");
  const r = nb.readiness;
  document.getElementById("ready").innerHTML = `<span>${r >= 100 ? "Notebook ready" : `Notebook ${r}% ready`}</span>
    <div class="meter" role="progressbar" aria-valuenow="${r}" aria-valuemin="0" aria-valuemax="100"><i style="width:${r}%"></i></div>
    <span class="muted">${nb.progress.done} of ${nb.progress.total} chapters done</span>`;
}

function chip(c) {
  if (c.state === "generating") return `<span class="chip gen">${STATUS_TEXT[c.status] || "Generating"}${c.status === "queued" ? "" : "…"}</span>`;
  const t = { ready: "Ready", in_progress: "In progress", done: "Done", needs_review: "Needs review", failed: "Failed" }[c.state];
  return `<span class="chip s-${c.state}">${t}</span>`;
}

function renderSide() {
  const side = document.getElementById("side"); if (!side) return;
  const nb = S.nb, cur = S.chapter?.id;
  const body = S.side === "graph"
    ? `<div class="graph-wrap">${renderGraph(nb, cur)}</div><div class="legend">${legend()}</div>`
    : `<ol class="roadmap">${nb.chapters.map((c, i) => `<li><button class="rm n-${c.state}${c.id === cur ? " cur" : ""}" data-id="${c.id}">
        <span class="num">${i + 1}</span><span class="rt">${esc(c.title)}${c.kind === "review" ? ` <em>review</em>` : ""}</span>${chip(c)}</button></li>`).join("")}</ol>`;
  document.querySelector(".nb")?.classList.toggle("gmode", S.side === "graph");
  side.innerHTML = `<div class="seg" role="tablist"><button role="tab" id="t-roadmap" aria-selected="${S.side !== "graph"}">${ICON.list} Roadmap</button>
      <button role="tab" id="t-graph" aria-selected="${S.side === "graph"}">${ICON.graph} Graph</button></div>${body}`;
  side.querySelector("#t-roadmap").onclick = () => { S.side = "roadmap"; store.set("side", "roadmap"); renderSide(); };
  side.querySelector("#t-graph").onclick = () => { S.side = "graph"; store.set("side", "graph"); renderSide(); };
  const open = (id) => { document.querySelector(".nb")?.classList.remove("side-open"); loadChapter(Number(id)); };
  side.querySelectorAll(".rm").forEach((b) => b.onclick = () => open(b.dataset.id));
  side.querySelectorAll(".gnode").forEach((g) => { g.onclick = () => open(g.dataset.id); g.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(g.dataset.id); } }; });
}

// ---------------------------------------------------------------------- chapter
async function loadChapter(cid, keepScroll = false) {
  const main = document.getElementById("main"); if (!main) return;
  try {
    const ch = await api("chapters/" + cid);
    S.chapter = ch; S.quiz = null;
    history.replaceState(null, "", `#/nb/${ch.notebook_id}/ch/${cid}`);
    if (ch.status !== "ready") api(`chapters/${cid}/prioritize`, {}).catch(() => {});  // jump the queue
    else api(`chapters/${cid}/open`, {}).catch(() => {});
    renderSide(); renderChapter();
    if (!keepScroll) window.scrollTo({ top: document.querySelector(".nbhead")?.offsetHeight || 0, behavior: "smooth" });
  } catch (e) { fail(e); }
}

function renderChapter() {
  const main = document.getElementById("main"), ch = S.chapter; if (!main || !ch) return;
  const order = S.nb.chapters, i = order.findIndex((c) => c.id === ch.id), prev = order[i - 1], next = order[i + 1];
  const head = `<header class="ch-head"><span class="crumb">Chapter ${i + 1} of ${order.length}${ch.kind === "review" ? " · review" : ""}</span>
    <h2>${esc(ch.title)}</h2>${ch.summary ? `<p class="sum">${esc(ch.summary)}</p>` : ""}
    ${ch.objectives?.length ? `<ul class="objs">${ch.objectives.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>` : ""}</header>`;
  const nav = `<nav class="ch-nav">${prev ? `<button class="btn ghost" data-go="${prev.id}">${ICON.left} ${esc(prev.title.slice(0, 28))}</button>` : "<span></span>"}
    ${next ? `<button class="btn ghost" data-go="${next.id}">${esc(next.title.slice(0, 28))} ${ICON.right}</button>` : ""}</nav>`;
  if (ch.status !== "ready") { main.innerHTML = head + skeleton(ch) + nav; wireNav(main); return; }
  const notices = [ch.unverified ? `<p class="flag">Not fully checked: the fact-checker still had questions about this chapter.</p>` : "",
                   !ch.grounded ? `<p class="flag">Written from general knowledge; no source material was supplied.</p>` : "",
                   ...(ch.curator_notes || []).map((n) => `<p class="flag">${esc(n)}</p>`)].join("");
  main.innerHTML = head + notices + ch.blocks.map(blockHtml).join("") + doubtHtml() + nav;
  wireNav(main); wireBlocks(main);
}

function skeleton(ch) {
  if (ch.status === "failed") return `<div class="skel failed"><p><strong>This chapter could not be built.</strong></p><p class="muted">${esc(ch.error || "Unknown error")}</p>
    <button class="btn" id="retry-ch">Try again</button></div>`;
  return `<div class="skel" role="status"><p class="muted"><span class="spin"></span>${esc(STATUS_TEXT[ch.status] || "Queued")}. This chapter fills in on its own; you can read others meanwhile.</p>
    <div class="bar-s w80"></div><div class="bar-s"></div><div class="bar-s w60"></div><div class="bar-s w90"></div></div>`;
}

function blockHtml(b) {
  const c = b.content;
  switch (b.type) {
    case "notes": return `<section class="blk" data-b="${b.id}"><div class="blk-h"><h3>Notes</h3><button class="linkbtn" data-edit="${b.id}">${ICON.edit} Edit</button></div>
      <div class="md" id="md-${b.id}">${renderMarkdown(c.markdown)}</div></section>`;
    case "slides": return `<section class="blk" data-b="${b.id}"><div class="blk-h"><h3>Slides</h3></div><div class="slides" id="sl-${b.id}" tabindex="0" aria-label="Slides"></div></section>`;
    case "video": return `<section class="blk"><figure class="video"><div class="ratio"><iframe src="https://www.youtube.com/embed/${encodeURIComponent(c.video_id)}${Number(c.start) > 0 ? "?start=" + Math.floor(Number(c.start)) : ""}" title="${esc(c.title)}"
      loading="lazy" allow="accelerometer; encrypted-media; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
      <figcaption>${esc(c.title)} <span class="muted">· ${esc(c.channel)}</span></figcaption></figure></section>`;
    case "image": { const a = c.attribution || {}; return `<section class="blk"><figure class="img"><img src="${esc(c.url)}" alt="${esc(c.title)}" loading="lazy" referrerpolicy="no-referrer">
      <figcaption>${esc(c.title)} · ${esc(a.author || "Unknown")} · ${esc(a.license || "")} · <a href="${esc(a.page_url || c.url)}" target="_blank" rel="noopener noreferrer">${esc(a.source || "source")}</a></figcaption></figure></section>`; }
    case "visual": {
      const a = c.attribution, p = c.payload || {};
      const body = p.svg ? `<div class="vz-wrap" role="img" aria-label="${esc(c.alt_text)}">${p.svg}</div>`
        : `<img src="${esc(p.url)}" alt="${esc(c.alt_text)}" loading="lazy" referrerpolicy="no-referrer">`;
      const credit = a ? ` · ${esc(a.author || "Unknown")} · ${esc(a.license || "")} · <a href="${esc(a.page_url || p.url)}" target="_blank" rel="noopener noreferrer">${esc(a.source || "source")}</a>` : "";
      return `<section class="blk"><figure class="img visual">${body}<figcaption>${esc(c.caption)}${credit}</figcaption></figure></section>`; }
    case "link": return `<section class="blk"><a class="linkcard" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer"><span class="lt">${esc(c.title)}</span>
      <span class="ls">${esc(c.source)} ${ICON.ext}</span><span class="lr">${esc(c.reason)}</span></a></section>`;
    case "quiz": return `<section class="blk" id="quizblk"><div class="blk-h"><h3>Check yourself</h3></div><div id="quiz"></div></section>`;
    case "summary": return `<section class="blk summary"><h3>Summary</h3><p>${esc(c.text)}</p>${(c.key_points || []).length ? `<ul>${c.key_points.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}</section>`;
    default: return "";
  }
}

function doubtHtml() {
  return `<section class="blk"><div class="blk-h"><h3>Ask a doubt</h3></div><div class="doubt-row"><input id="dq" placeholder="Type your doubt and press Enter" aria-label="Your doubt">
    <button class="iconbtn" id="dmic" aria-label="Ask by voice">${ICON.mic}</button></div><div id="dans"></div></section>`;
}

function wireNav(main) {
  main.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => loadChapter(Number(b.dataset.go)));
  const r = main.querySelector("#retry-ch");
  if (r) r.onclick = async () => { try { await api(`chapters/${S.chapter.id}/retry`, {}); toast("Trying again..."); } catch (e) { fail(e); } };
}

function wireBlocks(main) {
  main.querySelectorAll("[data-edit]").forEach((btn) => btn.onclick = () => editNotes(Number(btn.dataset.edit)));
  S.chapter.blocks.filter((b) => b.type === "slides").forEach((b) => renderSlides(b));
  const quiz = S.chapter.blocks.find((b) => b.type === "quiz");
  if (quiz) startQuiz(quiz.content.questions);
  const dq = main.querySelector("#dq");
  if (dq) dq.onkeydown = (e) => { if (e.key === "Enter" && dq.value.trim()) sendDoubt(dq.value.trim()); };
  const dm = main.querySelector("#dmic");
  if (dm) wireMic(dm, (t) => { dq.value = t; sendDoubt(t); }, () => S.nb.language || lang());
}

// --- notes editing (markdown textarea with save/cancel)
function editNotes(bid) {
  const b = S.chapter.blocks.find((x) => x.id === bid), el = document.getElementById("md-" + bid);
  if (!b || !el) return;
  el.innerHTML = `<textarea class="md-edit" id="ed-${bid}" aria-label="Edit notes (markdown)">${esc(b.content.markdown)}</textarea>
    <div class="actions"><button class="btn" id="save-${bid}">Save</button><button class="btn ghost" id="cancel-${bid}">Cancel</button></div>`;
  const ta = el.querySelector("textarea"); ta.style.height = Math.min(500, ta.scrollHeight + 10) + "px"; ta.focus();
  el.querySelector(`#cancel-${bid}`).onclick = () => { el.innerHTML = renderMarkdown(b.content.markdown); };
  el.querySelector(`#save-${bid}`).onclick = async () => {
    try { await api(`blocks/${bid}`, { markdown: ta.value }, undefined, "PUT"); b.content.markdown = ta.value; el.innerHTML = renderMarkdown(ta.value); toast("Saved"); }
    catch (e) { fail(e); }
  };
}

// --- slides carousel
function renderSlides(b) {
  const el = document.getElementById("sl-" + b.id); if (!el) return;
  const cards = b.content.cards, i = Math.min(S.slide[b.id] || 0, cards.length - 1), c = cards[i];
  el.innerHTML = `<div class="slide"><span class="badge">Slide ${i + 1} of ${cards.length}</span><h4>${esc(c.title)}</h4>
    <ul>${c.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>
    <div class="slide-nav"><button class="iconbtn" data-d="-1" aria-label="Previous slide" ${i === 0 ? "disabled" : ""}>${ICON.left}</button>
      <span class="dotrow">${cards.map((_, k) => `<i class="${k === i ? "on" : ""}"></i>`).join("")}</span>
      <button class="iconbtn" data-d="1" aria-label="Next slide" ${i === cards.length - 1 ? "disabled" : ""}>${ICON.right}</button></div>`;
  const go = (d) => { S.slide[b.id] = Math.max(0, Math.min(cards.length - 1, i + d)); renderSlides(b); document.getElementById("sl-" + b.id)?.focus(); };
  el.querySelectorAll("[data-d]").forEach((x) => x.onclick = () => go(Number(x.dataset.d)));
  el.onkeydown = (e) => { if (e.key === "ArrowRight") go(1); else if (e.key === "ArrowLeft") go(-1); };
}

// --- quiz: one question at a time, hint first, misconception feedback, then the course may change
function startQuiz(questions) {
  S.quiz = { cid: S.chapter.id, qs: questions, i: 0, right: 0 };
  renderQuestion(1, "");
}

function renderQuestion(attempt, hint) {
  const el = document.getElementById("quiz"); if (!el || !S.quiz) return;
  const { qs, i } = S.quiz, q = qs[i];
  el.innerHTML = `<div class="crumb">Question ${i + 1} of ${qs.length}</div><h4 class="q-title">${esc(q.text)}</h4>
    <div id="opts">${q.options.map((o, k) => `<button class="option" data-k="${k}">${esc(o)}</button>`).join("")}</div>
    <div id="fb">${hint ? `<div class="feedback bad">Hint: ${esc(hint)}</div>` : ""}</div>`;
  el.querySelectorAll(".option").forEach((b) => b.onclick = () => submitAnswer(q, b, attempt));
}

async function submitAnswer(q, btn, attempt) {
  document.querySelectorAll(".option").forEach((b) => (b.disabled = true));
  btn.classList.add("picked");
  try {
    const r = await api(`chapters/${S.chapter.id}/answer`, { learner_id: S.learner, session_id: S.session, question_id: q.id, answer: btn.textContent, attempt });
    if (r.follow_up) return renderQuestion(attempt + 1, r.hint);
    const fb = document.getElementById("fb");
    if (r.result === "correct") {
      if (attempt === 1) S.quiz.right++;
      btn.classList.add("right"); fb.innerHTML = `<div class="feedback ok">Correct!</div>`; return nextQuestion();
    }
    btn.classList.add("wrong");
    const why = r.misconception ? `<p>It looks like: <strong>${esc(r.misconception)}</strong></p>` : "";
    if (attempt === 1) {
      fb.innerHTML = `<div class="feedback bad">${why}<p>Hint: ${esc(r.hint)}</p></div><button class="btn block" id="retry">Try again</button>`;
      document.getElementById("retry").onclick = () => renderQuestion(2, r.hint);
    } else {
      document.querySelectorAll(".option").forEach((b) => { if (b.textContent === r.correct_answer) b.classList.add("right"); });
      fb.innerHTML = `<div class="feedback bad">${why}<p>Answer: ${esc(r.correct_answer)}</p></div>`; nextQuestion();
    }
  } catch (e) { fail(e); }
}

function nextQuestion() {
  const last = S.quiz.i === S.quiz.qs.length - 1;
  document.getElementById("fb").insertAdjacentHTML("beforeend", `<button class="btn block" id="nq">${last ? "Finish chapter" : "Next question"}</button>`);
  document.getElementById("nq").onclick = () => { if (last) finishQuiz(); else { S.quiz.i++; renderQuestion(1, ""); } };
}

async function finishQuiz() {
  const el = document.getElementById("quiz");
  el.innerHTML = `<p class="muted"><span class="spin"></span>Checking how it went...</p>`;
  try {
    const r = await api(`chapters/${S.chapter.id}/finish`, { learner_id: S.learner, session_id: S.session });
    S.nb = r.notebook; renderHead(); renderSide();
    const added = r.added.length ? `<p>Your course changed: ${esc(r.reason)}</p><ul>${r.added.map((a) => `<li><button class="linkbtn" data-go="${a.id}">${esc(a.title)}</button></li>`).join("")}</ul>` : "<p>Your course stays the same.</p>";
    el.innerHTML = `<div class="card"><div class="score">${r.score.correct} of ${r.score.total} right first time</div>${added}</div>`;
    el.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => loadChapter(Number(b.dataset.go)));
  } catch (e) { fail(e); }
}

// --- doubts
async function sendDoubt(text) {
  const box = document.getElementById("dans");
  box.innerHTML = `<p class="muted"><span class="spin"></span>Thinking...</p>`;
  const f = new FormData(); f.append("learner_id", S.learner); f.append("text", text);
  try {
    const r = await api(`chapters/${S.chapter.id}/doubt`, null, f);
    if (r.needs_text) { box.innerHTML = `<p class="muted">${esc(r.message)}</p>`; return; }
    box.innerHTML = `<div class="card"><p style="margin:0 0 10px">${esc(r.answer)}</p><button class="btn ghost" id="hear">${ICON.play} Listen</button></div>`;
    document.getElementById("hear").onclick = () => speak(r.answer, r.audio_b64);
    if (r.audio_b64) speak(r.answer, r.audio_b64);
  } catch (e) { box.innerHTML = ""; fail(e); }
}

async function finishSession() {
  try {
    const r = await api("session/close", { learner_id: S.learner, session_id: S.session, notebook_id: S.nb?.id });
    S.session = (await api("session/open", { learner_id: S.learner })).session_id;
    S.companion = r.message + (r.revisit.length ? ` To revisit: ${r.revisit.join(", ")}.` : ""); renderNote();
  } catch (e) { fail(e); }
}

init();
