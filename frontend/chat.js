// Chat panel for a notebook: persistent thread (SQLite), code-written progress narration, streamed answers with source chips,
// attachments with upload progress, "Ask about this" context, unread dot. The thread is delivered by cursor (messages?after=id),
// so a dropped stream or a reload never loses a message.
import { esc, ICON } from "./util.js";

const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
const STATUS = { reading: "Reading…", ready: "Read", partly_readable: "Partly readable", unreadable: "Couldn't read" };

export function createChat(ctx) {
  // ctx: { nid, learner, api, toast, onChip(chip), getChapterId(), getSources(), isOpen(), notifyUnread(), wireMic(btn, onText) }
  const el = { msgs: document.getElementById("msgs"), input: document.getElementById("chatinput"), send: document.getElementById("chatsend"),
               atts: document.getElementById("chatatts"), ctx: document.getElementById("ctxrow"), file: document.getElementById("chatfile"),
               pane: document.getElementById("chatpane") };
  const st = { lastId: 0, seen: new Set(), files: [], context: [], busy: false, stick: true, seq: 0 };

  // ------------------------------------------------------------------ rendering
  const attHtml = (list) => (list || []).map((a) => {
    const live = a.kind !== "url" && ctx.getSources().find((s) => s.title === a.name);
    const status = a.pending ? (a.progress != null && a.progress < 100 ? `Uploading ${a.progress}%` : "Sending…") : live ? STATUS[live.status] || "" : "";
    return `<span class="attchip" data-n="${esc(a.name)}">${a.kind === "image" ? ICON.doc : ICON.doc}<span class="nm">${esc(String(a.name).slice(0, 36))}</span>
      ${a.size ? `<span class="sz">${fmtSize(a.size)}</span>` : ""}${status ? `<span class="st">${status}</span>` : ""}</span>`;
  }).join("");
  const chipsHtml = (refs) => (refs || []).length ? `<div class="chips">${refs.map((r, i) => `<button class="srcchip" data-chip="${i}" type="button">${r.kind === "chapter" ? "Chapter · " : r.kind === "resource" ? "" : "Web · "}${esc(r.label)}</button>`).join("")}</div>` : "";

  function bubble(m) {
    const row = document.createElement("div");
    row.className = "msgrow " + (m.role === "user" ? "me" : m.kind === "system" ? "sysrow" : "bot");
    row.dataset.id = m.id || "";
    const avatar = m.role === "user" ? "" : `<span class="pul" aria-hidden="true">${ICON.logo}</span>`;
    row.innerHTML = `${avatar}<div class="msgcol"><div class="msg ${m.role === "user" ? "user" : m.kind === "system" ? "sys" : "bot"}">${esc(m.content).replace(/\n/g, "<br>")}${m.role === "user" ? attHtml(m.attachments) : ""}</div>${chipsHtml(m.refs)}</div>`;
    row.querySelectorAll(".srcchip").forEach((b) => b.onclick = () => ctx.onChip(m.refs[Number(b.dataset.chip)]));
    return row;
  }
  const scrollEnd = () => { if (st.stick) el.msgs.scrollTop = el.msgs.scrollHeight; };
  el.msgs.addEventListener("scroll", () => { st.stick = el.msgs.scrollHeight - el.msgs.scrollTop - el.msgs.clientHeight < 60; });

  function add(m, quiet) {
    if (m.id) { if (st.seen.has(m.id)) return; st.seen.add(m.id); st.lastId = Math.max(st.lastId, m.id); }
    el.msgs.appendChild(bubble(m)); scrollEnd();
    if (!quiet && m.role === "assistant" && !ctx.isOpen()) ctx.notifyUnread();
  }

  async function sync() {
    if (st.busy) return;   // the open turn renders its own messages; this runs again when it ends
    try {
      const r = await ctx.api(`notebooks/${ctx.nid}/messages?after=${st.lastId}`);
      const quiet = st.lastId === 0;   // the first load is history, not news
      r.messages.forEach((m) => add(m, quiet));
    } catch { /* the next snapshot retries */ }
  }
  function refreshStatuses() {   // upload chips follow the resource status in the notebook snapshot
    el.msgs.querySelectorAll(".attchip").forEach((c) => {
      const live = ctx.getSources().find((s) => s.title === c.dataset.n);
      const s = c.querySelector(".st");
      if (live && STATUS[live.status]) { if (s) s.textContent = STATUS[live.status]; else c.insertAdjacentHTML("beforeend", `<span class="st">${STATUS[live.status]}</span>`); }
    });
  }

  // ------------------------------------------------------------------ composer
  const autosize = () => { el.input.style.height = "auto"; el.input.style.height = Math.min(el.input.scrollHeight, 160) + "px"; };
  function drawAtts() {
    el.atts.innerHTML = st.files.map((f, i) => `<span class="attchip"><span class="nm">${esc(f.name.slice(0, 30))}</span><span class="sz">${fmtSize(f.size)}</span>
      <button class="x" data-i="${i}" type="button" aria-label="Remove ${esc(f.name)}">×</button></span>`).join("");
    el.atts.querySelectorAll(".x").forEach((b) => b.onclick = () => { st.files.splice(Number(b.dataset.i), 1); drawAtts(); sync2(); });
    el.ctx.innerHTML = st.context.map((c, i) => `<span class="ctxchip">About: ${esc(c.label)}<button class="x" data-i="${i}" type="button" aria-label="Remove context">×</button></span>`).join("");
    el.ctx.querySelectorAll(".x").forEach((b) => b.onclick = () => { st.context.splice(Number(b.dataset.i), 1); drawAtts(); });
    sync2();
  }
  const sync2 = () => { el.send.disabled = st.busy || !(el.input.value.trim() || st.files.length); };
  const addFiles = (list) => { st.files = [...st.files, ...list].slice(0, 6); drawAtts(); };

  function ask(block) {   // "Ask about this"
    if (!st.context.find((c) => c.id === block.id)) st.context = [...st.context, block].slice(-3);
    drawAtts(); ctx.openChat(); el.input.focus();
  }

  // One turn: POST (multipart) and read the server-sent events as they arrive; upload progress for files.
  function sendTurn() {
    const text = el.input.value.trim(); if (st.busy || !(text || st.files.length)) return;
    st.busy = true; sync2();
    const form = new FormData();
    form.append("learner_id", ctx.learner); form.append("text", text);
    const ch = ctx.getChapterId(); if (ch) form.append("chapter_id", String(ch));
    if (st.context.length) form.append("block_ids", st.context.map((c) => c.id).join(","));
    st.files.forEach((f) => form.append("files", f));
    const pending = st.files.map((f) => ({ name: f.name, size: f.size, kind: f.name.toLowerCase().endsWith(".pdf") ? "pdf" : f.type.startsWith("image/") ? "image" : "text", pending: true, progress: 0 }));
    const mine = bubble({ role: "user", content: text, attachments: pending, id: "" }); el.msgs.appendChild(mine); st.stick = true; scrollEnd();
    el.input.value = ""; st.files = []; st.context = []; autosize(); drawAtts();
    let typing = null, botText = "", botEl = null, done = false, read = 0, errored = false;
    const showTyping = (t) => { if (!typing) { typing = bubble({ role: "assistant", kind: "chat", content: t }); typing.classList.add("typing"); el.msgs.appendChild(typing); } else typing.querySelector(".msg").textContent = t; scrollEnd(); };

    const handle = (name, d) => {
      if (name === "user") { mine.dataset.id = d.id; st.seen.add(d.id); st.lastId = Math.max(st.lastId, d.id); mine.querySelectorAll(".attchip .st").forEach((s) => (s.textContent = "Reading…")); }
      else if (name === "status") showTyping(d.text);
      else if (name === "delta") { if (!botEl) { typing?.remove(); typing = null; botEl = bubble({ role: "assistant", kind: "chat", content: "" }); el.msgs.appendChild(botEl); } botText += d.text; botEl.querySelector(".msg").innerHTML = esc(botText).replace(/\n/g, "<br>"); scrollEnd(); }
      else if (name === "done") { done = true; if (botEl && d.id) { st.seen.add(d.id); st.lastId = Math.max(st.lastId, d.id); botEl.dataset.id = d.id; const col = botEl.querySelector(".msgcol"); col.insertAdjacentHTML("beforeend", chipsHtml(d.refs)); col.querySelectorAll(".srcchip").forEach((b) => b.onclick = () => ctx.onChip(d.refs[Number(b.dataset.chip)])); } scrollEnd(); }
      else if (name === "error") { errored = true; typing?.remove(); typing = null; add({ role: "assistant", kind: "chat", content: d.content, id: d.id }); }
    };
    const finish = () => { st.busy = false; typing?.remove(); sync2(); sync(); el.input.focus(); };

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/notebooks/${ctx.nid}/chat`);
    xhr.upload.onprogress = (e) => { if (!e.lengthComputable) return; const p = Math.round((100 * e.loaded) / e.total); mine.querySelectorAll(".attchip .st").forEach((s) => (s.textContent = p < 100 ? `Uploading ${p}%` : "Sending…")); };
    xhr.onprogress = () => {
      const chunk = xhr.responseText.slice(read); const cut = chunk.lastIndexOf("\n\n"); if (cut < 0) return;
      read += cut + 2;
      chunk.slice(0, cut).split("\n\n").forEach((blk) => { const l = blk.split("\n"); if (l[0]?.startsWith("event: ") && l[1]) { try { handle(l[0].slice(7), JSON.parse(l[1].slice(6))); } catch { /* partial */ } } });
    };
    xhr.onload = () => {
      if (xhr.status >= 400) { let m = "That didn't go through."; try { m = JSON.parse(xhr.responseText).detail || m; } catch { /* keep */ } mine.remove(); st.files = []; ctx.toast(m); el.input.value = text; autosize(); }
      else xhr.onprogress();
      finish();
    };
    xhr.onerror = () => { ctx.toast("Couldn't reach Aasaan. Please try again."); mine.remove(); el.input.value = text; autosize(); finish(); };
    xhr.send(form);
  }

  // ------------------------------------------------------------------ wiring
  el.input.oninput = () => { autosize(); sync2(); };
  el.input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendTurn(); } };
  el.send.onclick = sendTurn;
  document.getElementById("chatattach").onclick = () => el.file.click();
  el.file.onchange = () => { addFiles([...el.file.files]); el.file.value = ""; };
  el.input.addEventListener("paste", (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } });
  ["dragenter", "dragover"].forEach((t) => el.pane.addEventListener(t, (e) => { e.preventDefault(); el.pane.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => el.pane.addEventListener(t, (e) => { e.preventDefault(); el.pane.classList.remove("drag"); }));
  el.pane.addEventListener("drop", (e) => addFiles([...e.dataTransfer.files]));
  const mic = document.getElementById("chatmic");
  if (mic && ctx.wireMic) ctx.wireMic(mic, (t) => { el.input.value = (el.input.value ? el.input.value + " " : "") + t; autosize(); sync2(); el.input.focus(); });
  drawAtts(); sync();

  return { sync, refreshStatuses, ask, addFiles, onSnapshot(nb) { if (nb.message_seq > st.lastId) sync(); refreshStatuses(); }, focus() { el.input.focus(); } };
}
