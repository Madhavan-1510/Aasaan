// Swarm Tutor UI. Chat-first home -> course page -> lesson -> quiz -> recap. No agent names are ever shown.
const $app = document.getElementById("app");
const $bar = document.getElementById("bar");
const S = { learner: null, session: null, path: null, node: null, lesson: null, quiz: null, qi: 0, step: 0, file: null, note: "" };
const LANGS = { en: "English", hi: "हिन्दी", kn: "ಕನ್ನಡ", ta: "தமிழ்" };
const PLACEHOLDERS = ["Paste your notes or a chapter...", "Drop in a textbook photo or PDF...", "Add a goal, e.g. exam on Friday..."];
const ICON = {
  logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="20" r="2"/><path d="M7 7l3 3M17 7l-3 3M12 15v3"/></svg>',
  clip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7L14 4.5a3.3 3.3 0 014.7 4.7l-8.6 8.6a1.7 1.7 0 01-2.4-2.4L15 8.6"/></svg>',
  globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/></svg>',
  chev: '<svg class="dim" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
};
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const lang = () => store.get("lang") || "en";

async function api(path, body, form) {
  const opts = form ? { method: "POST", body: form } :
    body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch("/api/" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

function cover(title) {  // deterministic gradient per course title
  let h = 0; for (const c of title) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `background:linear-gradient(135deg,hsl(${h},42%,36%),hsl(${(h + 45) % 360},48%,26%))`;
}

function topbar(back) {
  $bar.innerHTML = `<div class="bar">
    ${back ? `<button class="linkbtn" id="back">← ${esc(back.label)}</button>` : `<span></span>`}
    <button class="brand" id="home">${ICON.logo}Swarm Tutor</button><span></span></div>`;
  document.getElementById("home").onclick = () => { location.hash = ""; home(); };
  if (back) document.getElementById("back").onclick = back.fn;
}

// ------------------------------------------------------------------- init
async function init() {
  let id = store.get("learner_id");
  if (!id) {
    id = (await api("learners", { grade: 9, language: lang() })).learner_id;
    store.set("learner_id", id);
  }
  S.learner = id;
  try {
    const o = await api("session/open", { learner_id: id });
    S.session = o.session_id;
    S.note = o.returning && o.path ? o.message : "";
  } catch { /* offline start is fine */ }
  const m = location.hash.match(/^#course\/(\w+)/);
  if (m) { try { S.path = await api("path/" + m[1]); return course(); } catch { /* fall back to home */ } }
  home();
}

// ------------------------------------------------------------------- home
async function home() {
  topbar();
  let courses = [];
  try { courses = await api("courses?learner_id=" + S.learner); } catch { /* none */ }
  $app.className = courses.length ? "wide" : "";
  $app.innerHTML = `<div class="hero"><h1>What do you want to learn today?</h1>
      <p>Paste your notes or attach a page. I'll build a short course from it.</p></div>
    ${S.note ? `<p class="note">${esc(S.note)}</p>` : ""}
    <div class="composer" id="chat">
      <div class="att-row" id="atts"></div>
      <textarea id="msg" rows="1" placeholder="${PLACEHOLDERS[0]}" aria-label="Paste your material or a goal"></textarea>
      <div class="toolbar">
        <button class="pill" id="attach" type="button" aria-label="Attach a photo or PDF">${ICON.clip}<span>Attach</span></button>
        <div class="menu-wrap"><button class="pill" id="langbtn" type="button" aria-haspopup="listbox" aria-label="Select language">
          ${ICON.globe}<span id="langlabel">${esc(LANGS[lang()])}</span>${ICON.chev}</button>
          <div class="menu" id="langmenu" role="listbox" hidden>${Object.entries(LANGS).map(([k, v]) =>
            `<button role="option" data-l="${k}" class="${k === lang() ? "on" : ""}">${esc(v)}</button>`).join("")}</div></div>
        <span class="spacer"></span>
        <button class="send" id="send" aria-label="Build my course" disabled>${ICON.up}</button>
        <input id="file" type="file" accept="image/*,application/pdf" hidden>
      </div></div>
    <p class="help">Enter to send · Shift+Enter for a new line · drop a file anywhere on the box</p>
    <div id="err" class="err"></div>
    ${courses.length ? `<h2 class="section">Your courses</h2><div class="grid">${courses.map((c) => `
      <button class="course-card" data-id="${c.path_id}"><div class="cover" style="${cover(c.title)}">${esc(c.title)}</div>
        <div class="body"><div class="meter"><i style="width:${100 * c.progress.done / Math.max(1, c.progress.total)}%"></i></div>
        <span class="muted">${c.progress.done} of ${c.progress.total} lessons</span></div></button>`).join("")}</div>` : ""}`;
  wireChat();
  document.querySelectorAll(".course-card").forEach((b) => b.onclick = async () => {
    try { S.path = await api("path/" + b.dataset.id); location.hash = "course/" + b.dataset.id; course(); } catch (e) { fail(e); }
  });
}

const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

function wireChat() {
  const msg = document.getElementById("msg"), send = document.getElementById("send"), box = document.getElementById("chat");
  const file = document.getElementById("file"), menu = document.getElementById("langmenu");
  const sync = () => {
    send.disabled = !(msg.value.trim() || S.file);
    msg.style.height = "auto"; msg.style.height = Math.min(msg.scrollHeight, 260) + "px";
    const f = S.file, img = f && f.type.startsWith("image/");
    document.getElementById("atts").innerHTML = !f ? "" : img
      ? `<div class="att img"><img src="${URL.createObjectURL(f)}" alt="${esc(f.name)}"><button class="x" id="rm" aria-label="Remove ${esc(f.name)}">×</button></div>`
      : `<div class="att file">${ICON.doc}<span class="nm">${esc(f.name)}</span><span class="sz">${fmtSize(f.size)}</span><button class="x" id="rm" aria-label="Remove ${esc(f.name)}">×</button></div>`;
    const rm = document.getElementById("rm"); if (rm) rm.onclick = () => { S.file = null; file.value = ""; sync(); };
  };
  msg.oninput = sync;
  msg.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!send.disabled) go(); } };
  document.getElementById("attach").onclick = () => file.click();
  file.onchange = () => { S.file = file.files[0] || null; sync(); };
  ["dragenter", "dragover"].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.remove("drag"); }));
  box.addEventListener("drop", (e) => { S.file = e.dataTransfer.files[0] || null; sync(); });
  // language picker (the toolbar slot)
  document.getElementById("langbtn").onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  menu.querySelectorAll("button").forEach((b) => b.onclick = () => {
    store.set("lang", b.dataset.l); document.getElementById("langlabel").textContent = LANGS[b.dataset.l];
    menu.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); menu.hidden = true; msg.focus();
  });
  document.addEventListener("click", () => { menu.hidden = true; });
  let i = 0; const ph = setInterval(() => { if (!document.body.contains(msg)) return clearInterval(ph); msg.placeholder = PLACEHOLDERS[++i % PLACEHOLDERS.length]; }, 3500);
  send.onclick = go;
  sync(); msg.focus();

  async function go() {
    const text = msg.value.trim();
    const err = document.getElementById("err");
    // We teach from the student's own material, so a bare topic is not enough.
    if (!S.file && text.length < 80) { err.textContent = "I teach from your own material. Paste a few paragraphs of notes, or attach a photo or PDF."; return; }
    const f = new FormData();
    f.append("learner_id", S.learner); f.append("language", lang());
    if (S.session) f.append("session_id", S.session);
    if (S.file) { f.append("file", S.file); if (text) f.append("goal", text.slice(0, 120)); } else f.append("text", text);
    building();
    try {
        const o = await api("start", null, f);
        S.session = o.session_id; S.path = o.path; S.file = null; S.note = "";
        location.hash = "course/" + o.path.path_id;
        course(o.warnings);
    } catch (e) { home(); document.getElementById("err").textContent = e.message; }
  }
}

function building() {
  topbar();
  $app.className = "";
  const msgs = ["Reading your material...", "Finding the key ideas...", "Ordering them into a path..."];
  $app.innerHTML = `<div class="building"><div class="spinner"></div><h1>Building your course</h1><p class="muted" id="bm">${msgs[0]}</p></div>`;
  let i = 0; const t = setInterval(() => { const el = document.getElementById("bm"); if (!el) return clearInterval(t); el.textContent = msgs[++i % msgs.length]; }, 1800);
}

// ------------------------------------------------------------ course page
function course(warnings) {
  const p = S.path, c = p.course;
  topbar({ label: "Home", fn: () => { location.hash = ""; home(); } });
  $app.className = "";
  const mins = p.nodes.reduce((a, n) => a + n.est_minutes, 0);
  const warn = Array.isArray(warnings) && warnings.length ? `<p class="muted">${esc(warnings.join("; "))}</p>` : "";
  $app.innerHTML = `<div class="course-hero"><div class="cover" style="${cover(c.title)}">${esc(c.title)}</div>
      <div class="panel"><div class="facts"><span><b>${p.nodes.length}</b> lessons</span><span><b>~${mins}</b> min</span>
        <span><b>${p.progress.done}</b> of ${p.progress.total} done</span></div>
        <div class="meter"><i style="width:${100 * p.progress.done / Math.max(1, p.progress.total)}%"></i></div>
        ${c.description ? `<p>${esc(c.description)}</p>` : ""}${warn}</div></div>
    <h2 class="units-title">Lessons</h2>
    ${p.nodes.map((n, i) => {
      const review = n.title.startsWith("Review:");
      const st = n.status === "done" ? `<span class="status done">Done</span>` : n.id === p.current ? `<span class="status next">Up next</span>`
        : review ? `<span class="status review">Review</span>` : "";
      return `<button class="unit" data-id="${n.id}"><span class="u-num">${i + 1}</span>
        <span><span class="u-title">${esc(n.title)}</span><br><span class="u-sub">${n.est_minutes} min</span></span>${st}</button>`;
    }).join("")}
    <div class="dock"><button class="btn ghost" id="end">Finish for today</button>
      ${p.current ? `<button class="btn" id="study">${p.progress.done ? "Continue" : "Study"}</button>` : ""}</div>`;
  document.querySelectorAll(".unit").forEach((b) => b.onclick = () => openLesson(b.dataset.id));
  if (p.current) document.getElementById("study").onclick = () => openLesson(p.current);
  document.getElementById("end").onclick = closeSession;
}

// ------------------------------------------------------------------ lesson
async function openLesson(nodeId) {
  S.node = nodeId; S.step = 0;
  topbar(); $app.className = "";
  $app.innerHTML = `<div class="building"><div class="spinner"></div><h1>Preparing your lesson</h1></div>`;
  try {
    S.lesson = (await api("lesson", { learner_id: S.learner, path_id: S.path.path_id, node_id: nodeId })).lesson;
    renderStep();
  } catch (e) { course(); fail(e); }
}

function renderStep() {
  const L = S.lesson, st = L.steps[S.step], last = S.step === L.steps.length - 1;
  topbar({ label: "Course", fn: course });
  $app.innerHTML = `<div class="crumb"><span>${esc(L.title)}</span><span>${S.step + 1} / ${L.steps.length}</span></div>
    <div class="steps-bar">${L.steps.map((_, i) => `<i class="${i <= S.step ? "on" : ""}"></i>`).join("")}</div>
    <div class="card"><span class="badge">${esc(st.type)}</span><p class="lesson-text">${esc(st.text)}</p></div>
    <div class="actions"><button class="btn ghost" id="play">▶ Listen</button><button class="btn ghost" id="ask">🎤 Ask by voice</button></div>
    <div id="doubt" class="doubt"></div>
    <button class="btn block" id="next">${last ? "Take the quiz" : "Next"}</button>`;
  document.getElementById("play").onclick = () => speak(st.text);
  document.getElementById("ask").onclick = askByVoice;
  document.getElementById("next").onclick = () => { if (last) openQuiz(); else { S.step++; renderStep(); } };
}

function speak(text, audioB64) {
  if (audioB64) { new Audio("data:audio/wav;base64," + audioB64).play(); return; }
  if ("speechSynthesis" in window) { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text)); }
}

function typedDoubt() {
  const box = document.getElementById("doubt");
  box.innerHTML = `<input id="q" placeholder="Type your doubt and press Enter" aria-label="Your doubt">`;
  const q = document.getElementById("q"); q.focus();
  q.onkeydown = (e) => { if (e.key === "Enter" && q.value.trim()) sendDoubt(q.value, null); };
}

async function askByVoice() {
  const box = document.getElementById("doubt");
  if (!navigator.mediaDevices || !window.MediaRecorder) return typedDoubt();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const rec = new MediaRecorder(stream), chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = () => { stream.getTracks().forEach((t) => t.stop()); sendDoubt("", new Blob(chunks, { type: "audio/wav" })); };
    box.innerHTML = `<button class="btn block" id="stop">■ Done speaking</button>`;
    document.getElementById("stop").onclick = () => rec.stop();
    rec.start();
  } catch { typedDoubt(); }
}

async function sendDoubt(text, audio) {
  const box = document.getElementById("doubt");
  box.innerHTML = `<p class="muted">Thinking...</p>`;
  const f = new FormData();
  f.append("learner_id", S.learner); f.append("path_id", S.path.path_id); f.append("node_id", S.node);
  if (text) f.append("text", text);
  if (audio) f.append("audio", audio, "doubt.wav");
  try {
    const r = await api("doubt", null, f);
    if (r.needs_text) { typedDoubt(); box.insertAdjacentHTML("afterbegin", `<p class="muted">${esc(r.message)}</p>`); return; }
    box.innerHTML = `<div class="card"><p style="margin:0 0 10px">${esc(r.answer)}</p><button class="btn ghost" id="hear">▶ Listen</button></div>`;
    document.getElementById("hear").onclick = () => speak(r.answer, r.audio_b64);
    if (r.audio_b64) speak(r.answer, r.audio_b64);
  } catch (e) { box.innerHTML = ""; fail(e); }
}

// -------------------------------------------------------------------- quiz
async function openQuiz() {
  topbar(); $app.innerHTML = `<div class="building"><div class="spinner"></div><h1>Getting your questions ready</h1></div>`;
  try {
    S.quiz = (await api("quiz", { learner_id: S.learner, path_id: S.path.path_id, node_id: S.node })).questions;
    S.qi = 0; renderQuestion();
  } catch (e) { course(); fail(e); }
}

function renderQuestion(attempt = 1, hint = "") {
  const q = S.quiz[S.qi];
  topbar({ label: "Course", fn: course });
  $app.innerHTML = `<div class="crumb"><span>Question ${S.qi + 1} of ${S.quiz.length}</span></div>
    <div class="steps-bar">${S.quiz.map((_, i) => `<i class="${i <= S.qi ? "on" : ""}"></i>`).join("")}</div>
    <h2 class="q-title">${esc(q.text)}</h2>
    <div id="opts">${q.options.map((o, i) => `<button class="option" data-i="${i}">${esc(o)}</button>`).join("")}</div>
    <div id="fb">${hint ? `<div class="feedback bad">Hint: ${esc(hint)}</div>` : ""}</div>`;
  document.querySelectorAll(".option").forEach((b) => b.onclick = () => submit(q, b, attempt));
}

async function submit(q, btn, attempt) {
  document.querySelectorAll(".option").forEach((b) => (b.disabled = true));
  btn.classList.add("picked");
  try {
    const r = await api("answer", { learner_id: S.learner, session_id: S.session, path_id: S.path.path_id,
      node_id: S.node, question_id: q.id, answer: btn.textContent, attempt });
    if (r.follow_up) return renderQuestion(attempt + 1, r.hint);
    const fb = document.getElementById("fb");
    if (r.result === "correct") {
      btn.classList.add("right"); fb.innerHTML = `<div class="feedback ok">Correct!</div>`; return nextQ();
    }
    btn.classList.add("wrong");
    const why = r.misconception ? `<p>It looks like: <strong>${esc(r.misconception)}</strong></p>` : "";
    if (attempt === 1) {
      fb.innerHTML = `<div class="feedback bad">${why}<p>Hint: ${esc(r.hint)}</p></div><button class="btn block" id="retry">Try again</button>`;
      document.getElementById("retry").onclick = () => renderQuestion(2, r.hint);
    } else {
      document.querySelectorAll(".option").forEach((b) => { if (b.textContent === r.correct_answer) b.classList.add("right"); });
      fb.innerHTML = `<div class="feedback bad">${why}<p>Answer: ${esc(r.correct_answer)}</p></div>`; nextQ();
    }
  } catch (e) { fail(e); }
}

function nextQ() {
  const last = S.qi === S.quiz.length - 1;
  document.getElementById("fb").insertAdjacentHTML("beforeend", `<button class="btn block" id="n">${last ? "See recap" : "Next question"}</button>`);
  document.getElementById("n").onclick = () => { if (last) recapNode(); else { S.qi++; renderQuestion(); } };
}

// ------------------------------------------------------------------- recap
async function recapNode() {
  topbar(); $app.innerHTML = `<div class="building"><div class="spinner"></div><h1>Updating your path</h1></div>`;
  try {
    const r = await api("finish-node", { learner_id: S.learner, session_id: S.session, path_id: S.path.path_id, node_id: S.node });
    S.path = r.path;
    const changed = r.patch.insert.length || r.patch.skip.length || r.patch.reorder.length;
    topbar({ label: "Course", fn: course });
    $app.innerHTML = `<div class="card"><span class="badge">Recap</span><div class="score">${r.score.correct} of ${r.score.total} right</div>
      <p>${changed ? "Your path changed: " + esc(r.reason) : "Your path stays the same."}</p></div>
      <div class="actions"><button class="btn ghost" id="c">Back to course</button>
      ${r.next_node ? `<button class="btn" id="nx">Next lesson</button>` : ""}</div>`;
    document.getElementById("c").onclick = course;
    if (r.next_node) document.getElementById("nx").onclick = () => openLesson(r.next_node);
  } catch (e) { course(); fail(e); }
}

async function closeSession() {
  topbar(); $app.innerHTML = `<div class="building"><div class="spinner"></div><h1>Wrapping up</h1></div>`;
  try {
    const r = await api("session/close", { learner_id: S.learner, session_id: S.session, path_id: S.path && S.path.path_id });
    S.session = null; S.note = "";
    $app.innerHTML = `<div class="hero"><h1>See you next time</h1></div><div class="card"><p style="margin:0">${esc(r.message)}</p>
      ${r.revisit.length ? `<p class="muted" style="margin:10px 0 0">To revisit: ${esc(r.revisit.join(", "))}</p>` : ""}</div>
      <button class="btn block" id="again">Back to home</button>`;
    document.getElementById("again").onclick = () => { location.hash = ""; init(); };
  } catch (e) { course(); fail(e); }
}

function fail(e) { const d = document.createElement("p"); d.className = "err"; d.textContent = e.message; $app.appendChild(d); }

init();
