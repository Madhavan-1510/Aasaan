"""Aasaan SQLite store (WAL mode, one app instance). Notebooks, chapters, blocks, jobs, memory, events.

Everything stays local: no external database, no accounts. `learner_id` is a random id kept in the browser.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks(
  id TEXT PRIMARY KEY, learner_id TEXT, title TEXT, goal TEXT, language TEXT, level TEXT, description TEXT,
  status TEXT, error TEXT, created_at REAL, updated_at REAL, last_opened REAL);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, notebook_id TEXT, kind TEXT, raw_ref TEXT, title TEXT, extracted_text TEXT, chunks TEXT);
CREATE TABLE IF NOT EXISTS chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT, notebook_id TEXT, position INTEGER, title TEXT, summary TEXT,
  objectives TEXT, prereqs TEXT, status TEXT, priority INTEGER DEFAULT 0, source_refs TEXT,
  kind TEXT DEFAULT 'core', needs_review INTEGER DEFAULT 0, completed INTEGER DEFAULT 0, started INTEGER DEFAULT 0,
  meta TEXT, error TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_chapters_nb ON chapters(notebook_id, position);
CREATE TABLE IF NOT EXISTS blocks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chapter_id INTEGER, position INTEGER, type TEXT, content TEXT,
  verified INTEGER DEFAULT 0, meta TEXT);
CREATE INDEX IF NOT EXISTS ix_blocks_ch ON blocks(chapter_id, position);
CREATE TABLE IF NOT EXISTS quiz_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, learner_id TEXT, notebook_id TEXT, chapter_id INTEGER, session_id TEXT,
  question_id TEXT, concept TEXT, answer TEXT, result TEXT, misconception TEXT, confidence REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS memory(
  learner_id TEXT, kind TEXT, key TEXT, value TEXT, count INTEGER DEFAULT 1, updated REAL,
  PRIMARY KEY(learner_id, kind, key));
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, learner_id TEXT, started REAL, ended REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, agent TEXT, model_used TEXT,
  fallback_step INTEGER, status TEXT, latency_ms INTEGER, validation TEXT, summary TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, notebook_id TEXT, chapter_id INTEGER, kind TEXT, state TEXT,
  attempts INTEGER DEFAULT 0, last_error TEXT, scheduled_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state, scheduled_at);
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, notebook_id TEXT, role TEXT, kind TEXT DEFAULT 'chat', content TEXT,
  attachments TEXT, refs TEXT, key TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_messages_nb ON messages(notebook_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_key ON messages(notebook_id, key) WHERE key IS NOT NULL;
"""


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _l(s: Optional[str], default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        mem = str(path) == ":memory:"
        if not mem:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if not mem:
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for databases created by an earlier version."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(sources)")}
        if "meta" not in have:
            self.conn.execute("ALTER TABLE sources ADD COLUMN meta TEXT")
        jobs = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "payload" not in jobs:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN payload TEXT")

    # -- low level (autocommit; transaction() opens an explicit BEGIN) -------
    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def transaction(self):
        """Context manager: several writes commit together (a chapter's blocks and its `ready` status)."""
        store = self

        class _Tx:
            def __enter__(self_inner):
                store._lock.acquire()
                store.conn.execute("BEGIN")
                return store

            def __exit__(self_inner, et, ev, tb):
                try:
                    store.conn.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store._lock.release()
                return False
        return _Tx()

    # ================================================================ notebooks
    def create_notebook(self, learner_id: str, goal: Optional[str], language: str, title: str = "New notebook") -> str:
        nid = "nb_" + uuid.uuid4().hex[:10]
        now = time.time()
        self._run("INSERT INTO notebooks(id,learner_id,title,goal,language,level,description,status,created_at,updated_at,last_opened)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (nid, learner_id, title, goal, language, "beginner", "", "outlining", now, now, now))
        return nid

    def get_notebook(self, nid: str) -> Optional[dict]:
        r = self._one("SELECT * FROM notebooks WHERE id=?", (nid,))
        return dict(r) if r else None

    def list_notebooks(self, learner_id: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM notebooks WHERE learner_id=? ORDER BY last_opened DESC", (learner_id,))]

    def update_notebook(self, nid: str, **fields: Any) -> None:
        allowed = {"title", "goal", "language", "level", "description", "status", "error", "last_opened"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in sets)
        self._run(f"UPDATE notebooks SET {cols} WHERE id=?", (*sets.values(), nid))

    def delete_notebook(self, nid: str) -> None:
        with self.transaction():
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM chapters WHERE notebook_id=?", (nid,))]
            for cid in ids:
                self.conn.execute("DELETE FROM blocks WHERE chapter_id=?", (cid,))
            for t in ("chapters", "sources", "jobs", "quiz_attempts", "messages"):
                self.conn.execute(f"DELETE FROM {t} WHERE notebook_id=?", (nid,))
            self.conn.execute("DELETE FROM notebooks WHERE id=?", (nid,))

    # ================================================================== sources
    def add_source(self, nid: str, kind: str, raw_ref: str, title: str, text: str, chunks: list[dict]) -> str:
        sid = "s" + uuid.uuid4().hex[:6]
        self._run("INSERT INTO sources(id,notebook_id,kind,raw_ref,title,extracted_text,chunks) VALUES(?,?,?,?,?,?,?)",
                  (sid, nid, kind, raw_ref, title, text, _j(chunks)))
        return sid

    def set_source_text(self, sid: str, title: str, text: str) -> None:
        self._run("UPDATE sources SET title=?, extracted_text=? WHERE id=?", (title[:200], text, sid))

    def update_source_chunks(self, sid: str, chunks: list[dict]) -> None:
        self._run("UPDATE sources SET chunks=? WHERE id=?", (_j(chunks), sid))

    def delete_source(self, sid: str) -> None:
        self._run("DELETE FROM sources WHERE id=?", (sid,))

    def set_chapter_refs(self, cid: int, refs: list) -> None:
        self._run("UPDATE chapters SET source_refs=? WHERE id=?", (_j(refs), cid))

    def set_source_meta(self, sid: str, meta: dict) -> None:
        self._run("UPDATE sources SET meta=? WHERE id=?", (_j(meta), sid))

    def has_history(self, learner_id: str) -> bool:
        """Does memory show an earlier session? A session that was closed, or any quiz attempt or recorded mistake. Merely opening the app
        (every page load opens a session) does not count."""
        if self._one("SELECT 1 FROM sessions WHERE learner_id=? AND ended IS NOT NULL LIMIT 1", (learner_id,)):
            return True
        if self._one("SELECT 1 FROM quiz_attempts WHERE learner_id=? LIMIT 1", (learner_id,)):
            return True
        return bool(self.mistake_labels(learner_id))

    def sources(self, nid: str) -> list[dict]:
        out = []
        for r in self._all("SELECT * FROM sources WHERE notebook_id=? ORDER BY rowid", (nid,)):
            d = dict(r)
            d["chunks"] = _l(d["chunks"], [])
            d["meta"] = _l(d.get("meta"), {})
            out.append(d)
        return out

    # ================================================================= chapters
    def add_chapter(self, nid: str, position: int, title: str, summary: str, objectives: list, prereqs: list,
                    source_refs: list, kind: str = "core", priority: int = 0, meta: Optional[dict] = None) -> int:
        cur = self._run(
            "INSERT INTO chapters(notebook_id,position,title,summary,objectives,prereqs,status,priority,source_refs,kind,meta,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, position, title, summary, _j(objectives), _j(prereqs), "queued", priority, _j(source_refs), kind,
             _j(meta or {}), time.time()))
        return int(cur.lastrowid)

    @staticmethod
    def _chapter(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k, dflt in (("objectives", []), ("prereqs", []), ("source_refs", []), ("meta", {})):
            d[k] = _l(d.get(k), dflt)
        for k in ("needs_review", "completed", "started"):
            d[k] = bool(d.get(k))
        return d

    def get_chapter(self, cid: int) -> Optional[dict]:
        r = self._one("SELECT * FROM chapters WHERE id=?", (cid,))
        return self._chapter(r) if r else None

    def chapters(self, nid: str) -> list[dict]:
        return [self._chapter(r) for r in self._all("SELECT * FROM chapters WHERE notebook_id=? ORDER BY position, id", (nid,))]

    def set_chapter_prereqs(self, cid: int, prereqs: list[int]) -> None:
        self._run("UPDATE chapters SET prereqs=? WHERE id=?", (_j(prereqs), cid))

    def set_chapter_status(self, cid: int, status: str, error: str = "") -> None:
        self._run("UPDATE chapters SET status=?, error=?, updated_at=? WHERE id=?", (status, error, time.time(), cid))

    def update_chapter(self, cid: int, **fields: Any) -> None:
        allowed = {"title", "summary", "objectives", "priority", "needs_review", "completed", "started", "meta", "position"}
        sets: dict[str, Any] = {}
        for k, v in fields.items():
            if k in allowed:
                sets[k] = _j(v) if k in ("objectives", "meta") else (int(v) if isinstance(v, bool) else v)
        if sets:
            sets["updated_at"] = time.time()
            cols = ", ".join(f"{k}=?" for k in sets)
            self._run(f"UPDATE chapters SET {cols} WHERE id=?", (*sets.values(), cid))

    def shift_positions(self, nid: str, after_position: int, by: int = 1) -> None:
        self._run("UPDATE chapters SET position=position+? WHERE notebook_id=? AND position>?", (by, nid, after_position))

    # =================================================================== blocks
    def replace_blocks(self, cid: int, blocks: list[dict], status: str = "ready", meta: Optional[dict] = None) -> None:
        """Write a whole chapter and flip its status in ONE transaction, so a chapter is never half built."""
        with self.transaction():
            self.conn.execute("DELETE FROM blocks WHERE chapter_id=?", (cid,))
            for i, b in enumerate(blocks):
                self.conn.execute("INSERT INTO blocks(chapter_id,position,type,content,verified,meta) VALUES(?,?,?,?,?,?)",
                                  (cid, i, b["type"], _j(b["content"]), int(bool(b.get("verified"))), _j(b.get("meta") or {})))
            if meta is not None:
                self.conn.execute("UPDATE chapters SET meta=? WHERE id=?", (_j(meta), cid))
            self.conn.execute("UPDATE chapters SET status=?, error='', updated_at=? WHERE id=?", (status, time.time(), cid))

    def blocks(self, cid: int) -> list[dict]:
        out = []
        for r in self._all("SELECT * FROM blocks WHERE chapter_id=? ORDER BY position, id", (cid,)):
            d = dict(r)
            d["content"] = _l(d["content"], {})
            d["meta"] = _l(d["meta"], {})
            d["verified"] = bool(d["verified"])
            out.append(d)
        return out

    def get_block(self, bid: int) -> Optional[dict]:
        r = self._one("SELECT * FROM blocks WHERE id=?", (bid,))
        if not r:
            return None
        d = dict(r)
        d["content"], d["meta"], d["verified"] = _l(d["content"], {}), _l(d["meta"], {}), bool(d["verified"])
        return d

    def update_block_content(self, bid: int, content: Any) -> None:
        self._run("UPDATE blocks SET content=? WHERE id=?", (_j(content), bid))

    # ============================================================= quiz attempts
    def add_attempt(self, **a: Any) -> None:
        self._run("INSERT INTO quiz_attempts(learner_id,notebook_id,chapter_id,session_id,question_id,concept,answer,result,"
                  "misconception,confidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (a["learner_id"], a["notebook_id"], a["chapter_id"], a["session_id"], a["question_id"], a["concept"],
                   a["answer"], a["result"], a.get("misconception"), a.get("confidence"), time.time()))

    def attempts(self, chapter_id: int, session_id: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM quiz_attempts WHERE chapter_id=?", [chapter_id]
        if session_id:
            sql += " AND session_id=?"
            args.append(session_id)
        return [dict(r) for r in self._all(sql + " ORDER BY id", tuple(args))]

    def session_attempts(self, session_id: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM quiz_attempts WHERE session_id=? ORDER BY id", (session_id,))]

    # ================================================================== memory
    def add_mistake(self, learner_id: str, label: str) -> None:
        label = label.strip()
        if not label:
            return
        self._run("""INSERT INTO memory(learner_id,kind,key,value,count,updated) VALUES(?,'mistake',?,?,1,?)
                     ON CONFLICT(learner_id,kind,key) DO UPDATE SET count=count+1, updated=excluded.updated""",
                  (learner_id, label, "", time.time()))

    def mistake_labels(self, learner_id: str) -> list[str]:
        return [r["key"] for r in self._all("SELECT key FROM memory WHERE learner_id=? AND kind='mistake' ORDER BY count DESC, updated DESC",
                                            (learner_id,))]

    def mistake_counts(self, learner_id: str) -> dict[str, int]:
        return {r["key"]: r["count"] for r in self._all("SELECT key,count FROM memory WHERE learner_id=? AND kind='mistake'", (learner_id,))}

    def set_memory(self, learner_id: str, kind: str, key: str, value: Any) -> None:
        self._run("INSERT OR REPLACE INTO memory(learner_id,kind,key,value,count,updated) VALUES(?,?,?,?,1,?)",
                  (learner_id, kind, key, _j(value), time.time()))

    def get_memory(self, learner_id: str, kind: str, key: str, default: Any = None) -> Any:
        r = self._one("SELECT value FROM memory WHERE learner_id=? AND kind=? AND key=?", (learner_id, kind, key))
        return _l(r["value"], default) if r else default

    # ================================================================= sessions
    def open_session(self, learner_id: str) -> str:
        sid = str(uuid.uuid4())
        self._run("INSERT INTO sessions(id,learner_id,started) VALUES(?,?,?)", (sid, learner_id, time.time()))
        return sid

    def session_started(self, session_id: str) -> float:
        r = self._one("SELECT started FROM sessions WHERE id=?", (session_id,))
        return r["started"] if r else 0.0

    def close_session(self, session_id: str, summary: str) -> None:
        self._run("UPDATE sessions SET ended=?, summary=? WHERE id=?", (time.time(), summary, session_id))

    def last_summary(self, learner_id: str) -> Optional[str]:
        r = self._one("SELECT summary FROM sessions WHERE learner_id=? AND summary IS NOT NULL ORDER BY ended DESC LIMIT 1", (learner_id,))
        return r["summary"] if r else None

    def session_count(self, learner_id: str) -> int:
        return self._one("SELECT COUNT(*) c FROM sessions WHERE learner_id=?", (learner_id,))["c"]

    # =================================================================== events
    def log_event(self, e: dict) -> None:
        self._run("INSERT INTO events(run_id,ts,agent,model_used,fallback_step,status,latency_ms,validation,summary,error)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (e["run_id"], e["ts"], e["agent"], e["model_used"], e["fallback_step"], e["status"],
                   e["latency_ms"], e["validation"], e["summary"], e.get("error", "")))

    def events(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # ===================================================================== jobs
    def add_job(self, nid: str, chapter_id: Optional[int], kind: str, payload: Optional[dict] = None) -> int:
        now = time.time()
        cur = self._run("INSERT INTO jobs(notebook_id,chapter_id,kind,state,attempts,scheduled_at,updated_at,payload) VALUES(?,?,?,?,0,?,?,?)",
                        (nid, chapter_id, kind, "queued", now, now, _j(payload) if payload is not None else None))
        return int(cur.lastrowid)

    # ================================================================ messages
    def add_message(self, nid: str, role: str, content: str, kind: str = "chat", attachments: Optional[list] = None,
                    refs: Optional[list] = None, key: Optional[str] = None) -> Optional[int]:
        """Append to the notebook's chat thread. A `key` makes it idempotent (a retried job never narrates twice); returns None for a duplicate."""
        try:
            cur = self._run("INSERT INTO messages(notebook_id,role,kind,content,attachments,refs,key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (nid, role, kind, content, _j(attachments or []), _j(refs or []), key, time.time()))
        except sqlite3.IntegrityError:
            return None
        return int(cur.lastrowid)

    def messages(self, nid: str, after: int = 0, limit: int = 200) -> list[dict]:
        out = []
        for r in self._all("SELECT * FROM messages WHERE notebook_id=? AND id>? ORDER BY id LIMIT ?", (nid, after, limit)):
            d = dict(r)
            d["attachments"], d["refs"] = _l(d["attachments"], []), _l(d["refs"], [])
            out.append(d)
        return out

    def last_message_id(self, nid: str) -> int:
        r = self._one("SELECT COALESCE(MAX(id),0) m FROM messages WHERE notebook_id=?", (nid,))
        return int(r["m"])

    def pending_jobs(self) -> list[dict]:
        """Queued jobs that are due, most urgent first: chapter priority, then notebook order."""
        rows = self._all(
            "SELECT j.*, CASE WHEN j.kind='notebook' THEN 90 ELSE COALESCE(c.priority,0) END AS prio, COALESCE(c.position,0) AS pos FROM jobs j "
            "LEFT JOIN chapters c ON c.id=j.chapter_id WHERE j.state='queued' AND j.scheduled_at<=? "
            "ORDER BY prio DESC, pos ASC, j.id ASC", (time.time(),))
        return [dict(r) for r in rows]

    def get_job(self, jid: int) -> Optional[dict]:
        r = self._one("SELECT * FROM jobs WHERE id=?", (jid,))
        return dict(r) if r else None

    def set_job(self, jid: int, state: str, error: str = "", attempts: Optional[int] = None, delay_s: float = 0) -> None:
        now = time.time()
        if attempts is None:
            self._run("UPDATE jobs SET state=?, last_error=?, updated_at=?, scheduled_at=? WHERE id=?", (state, error, now, now + delay_s, jid))
        else:
            self._run("UPDATE jobs SET state=?, last_error=?, attempts=?, updated_at=?, scheduled_at=? WHERE id=?",
                      (state, error, attempts, now, now + delay_s, jid))

    def job_for_chapter(self, cid: int) -> Optional[dict]:
        r = self._one("SELECT * FROM jobs WHERE chapter_id=? ORDER BY id DESC LIMIT 1", (cid,))
        return dict(r) if r else None

    def active_jobs(self, nid: Optional[str] = None) -> int:
        sql, args = "SELECT COUNT(*) c FROM jobs WHERE state IN ('queued','running')", ()
        if nid:
            sql, args = sql + " AND notebook_id=?", (nid,)
        return self._one(sql, args)["c"]

    def recover_after_restart(self) -> int:
        """Jobs/chapters caught mid-flight by a restart go back to the queue (resumable generation)."""
        with self.transaction():
            n = self.conn.execute("UPDATE jobs SET state='queued', scheduled_at=? WHERE state='running'", (time.time(),)).rowcount
            self.conn.execute("UPDATE chapters SET status='queued' WHERE status IN ('drafting','checking')")
        return n

    # ==================================================================== cache
    def cache_get(self, key: str) -> Optional[Any]:
        r = self._one("SELECT value FROM cache WHERE key=?", (key,))
        return _l(r["value"]) if r else None

    def cache_set(self, key: str, value: Any) -> None:
        self._run("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, _j(value)))
