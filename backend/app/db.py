"""SQLite memory store (PRD section 8, decision 5).

Holds the learner profile, mistake labels, path history, lessons/quizzes,
answers and the internal event log (F12). Everything stays local.
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
CREATE TABLE IF NOT EXISTS learners(
  id TEXT PRIMARY KEY, grade INTEGER, language TEXT, goal TEXT,
  notes TEXT, last_node TEXT, last_path TEXT, created REAL);
CREATE TABLE IF NOT EXISTS mistakes(
  learner_id TEXT, label TEXT, count INTEGER, last_seen REAL,
  PRIMARY KEY(learner_id, label));
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY, learner_id TEXT, intake_json TEXT);
CREATE TABLE IF NOT EXISTS paths(
  path_id TEXT PRIMARY KEY, learner_id TEXT, source_id TEXT, path_json TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS path_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path_id TEXT, patch_json TEXT, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS node_status(
  path_id TEXT, node_id TEXT, status TEXT, PRIMARY KEY(path_id, node_id));
CREATE TABLE IF NOT EXISTS lessons(
  path_id TEXT, node_id TEXT, lesson_json TEXT, verified INTEGER,
  PRIMARY KEY(path_id, node_id));
CREATE TABLE IF NOT EXISTS quizzes(
  path_id TEXT, node_id TEXT, quiz_json TEXT, PRIMARY KEY(path_id, node_id));
CREATE TABLE IF NOT EXISTS answers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, learner_id TEXT, session_id TEXT, path_id TEXT,
  node_id TEXT, question_id TEXT, concept TEXT, answer TEXT, result TEXT,
  misconception TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, learner_id TEXT, started REAL, ended REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, agent TEXT, model_used TEXT,
  fallback_step INTEGER, status TEXT, latency_ms INTEGER, validation TEXT, summary TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        try:  # migrate databases created before the error column existed
            self.conn.execute("ALTER TABLE events ADD COLUMN error TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    # -- low level -----------------------------------------------------------
    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    # -- learners & memory ---------------------------------------------------
    def create_learner(self, grade: int, language: str, goal: Optional[str]) -> str:
        lid = str(uuid.uuid4())
        self._run(
            "INSERT INTO learners(id,grade,language,goal,created) VALUES(?,?,?,?,?)",
            (lid, grade, language, goal, time.time()),
        )
        return lid

    def get_learner(self, learner_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM learners WHERE id=?", (learner_id,))
        if not row:
            return None
        d = dict(row)
        d["mistake_labels"] = self.mistake_labels(learner_id)
        return d

    def update_learner(self, learner_id: str, **fields: Any) -> None:
        allowed = {"grade", "language", "goal", "notes", "last_node", "last_path"}
        for k, v in fields.items():
            if k in allowed and v is not None:
                self._run(f"UPDATE learners SET {k}=? WHERE id=?", (v, learner_id))

    def add_mistake(self, learner_id: str, label: str) -> None:
        label = label.strip()
        if not label:
            return
        self._run(
            """INSERT INTO mistakes(learner_id,label,count,last_seen) VALUES(?,?,1,?)
               ON CONFLICT(learner_id,label) DO UPDATE SET count=count+1,last_seen=excluded.last_seen""",
            (learner_id, label, time.time()),
        )

    def mistake_labels(self, learner_id: str) -> list[str]:
        rows = self._all(
            "SELECT label FROM mistakes WHERE learner_id=? ORDER BY count DESC, last_seen DESC",
            (learner_id,),
        )
        return [r["label"] for r in rows]

    def mistake_counts(self, learner_id: str) -> dict[str, int]:
        rows = self._all("SELECT label,count FROM mistakes WHERE learner_id=?", (learner_id,))
        return {r["label"]: r["count"] for r in rows}

    # -- sessions ------------------------------------------------------------
    def open_session(self, learner_id: str) -> str:
        sid = str(uuid.uuid4())
        self._run("INSERT INTO sessions(id,learner_id,started) VALUES(?,?,?)", (sid, learner_id, time.time()))
        return sid

    def close_session(self, session_id: str, summary: str) -> None:
        self._run("UPDATE sessions SET ended=?, summary=? WHERE id=?", (time.time(), summary, session_id))

    def last_summary(self, learner_id: str) -> Optional[str]:
        row = self._one(
            "SELECT summary FROM sessions WHERE learner_id=? AND summary IS NOT NULL ORDER BY ended DESC LIMIT 1",
            (learner_id,),
        )
        return row["summary"] if row else None

    def session_count(self, learner_id: str) -> int:
        return self._one("SELECT COUNT(*) c FROM sessions WHERE learner_id=?", (learner_id,))["c"]

    # -- sources, paths ------------------------------------------------------
    def save_source(self, source_id: str, learner_id: str, intake: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO sources VALUES(?,?,?)", (source_id, learner_id, json.dumps(intake))
        )

    def get_source(self, source_id: str) -> Optional[dict]:
        row = self._one("SELECT intake_json FROM sources WHERE source_id=?", (source_id,))
        return json.loads(row["intake_json"]) if row else None

    def save_path(self, path_id: str, learner_id: str, source_id: str, path: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO paths VALUES(?,?,?,?,?)",
            (path_id, learner_id, source_id, json.dumps(path), time.time()),
        )

    def get_path(self, path_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM paths WHERE path_id=?", (path_id,))
        if not row:
            return None
        return {"source_id": row["source_id"], "learner_id": row["learner_id"], "path": json.loads(row["path_json"])}

    def learner_paths(self, learner_id: str) -> list[dict]:
        rows = self._all("SELECT path_id,path_json,updated FROM paths WHERE learner_id=? ORDER BY updated DESC", (learner_id,))
        return [{"path_id": r["path_id"], "path": json.loads(r["path_json"]), "updated": r["updated"]} for r in rows]

    def add_path_history(self, path_id: str, patch: dict, reason: str) -> None:
        self._run(
            "INSERT INTO path_history(path_id,patch_json,reason,ts) VALUES(?,?,?,?)",
            (path_id, json.dumps(patch), reason, time.time()),
        )

    def path_history(self, path_id: str) -> list[dict]:
        rows = self._all("SELECT patch_json,reason,ts FROM path_history WHERE path_id=? ORDER BY id", (path_id,))
        return [{"patch": json.loads(r["patch_json"]), "reason": r["reason"], "ts": r["ts"]} for r in rows]

    def set_node_status(self, path_id: str, node_id: str, status: str) -> None:
        self._run("INSERT OR REPLACE INTO node_status VALUES(?,?,?)", (path_id, node_id, status))

    def node_statuses(self, path_id: str) -> dict[str, str]:
        rows = self._all("SELECT node_id,status FROM node_status WHERE path_id=?", (path_id,))
        return {r["node_id"]: r["status"] for r in rows}

    # -- lessons & quizzes ---------------------------------------------------
    def save_lesson(self, path_id: str, node_id: str, lesson: dict, verified: bool) -> None:
        self._run(
            "INSERT OR REPLACE INTO lessons VALUES(?,?,?,?)", (path_id, node_id, json.dumps(lesson), int(verified))
        )

    def get_lesson(self, path_id: str, node_id: str) -> Optional[dict]:
        row = self._one("SELECT lesson_json,verified FROM lessons WHERE path_id=? AND node_id=?", (path_id, node_id))
        if not row:
            return None
        return {"lesson": json.loads(row["lesson_json"]), "verified": bool(row["verified"])}

    def save_quiz(self, path_id: str, node_id: str, quiz: dict) -> None:
        self._run("INSERT OR REPLACE INTO quizzes VALUES(?,?,?)", (path_id, node_id, json.dumps(quiz)))

    def get_quiz(self, path_id: str, node_id: str) -> Optional[dict]:
        row = self._one("SELECT quiz_json FROM quizzes WHERE path_id=? AND node_id=?", (path_id, node_id))
        return json.loads(row["quiz_json"]) if row else None

    # -- answers -------------------------------------------------------------
    def save_answer(self, **a: Any) -> None:
        self._run(
            """INSERT INTO answers(learner_id,session_id,path_id,node_id,question_id,concept,answer,result,misconception,ts)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (a["learner_id"], a["session_id"], a["path_id"], a["node_id"], a["question_id"], a["concept"],
             a["answer"], a["result"], a.get("misconception"), time.time()),
        )

    def session_answers(self, session_id: str, node_id: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM answers WHERE session_id=?"
        args: tuple = (session_id,)
        if node_id:
            sql += " AND node_id=?"
            args = (session_id, node_id)
        return [dict(r) for r in self._all(sql + " ORDER BY id", args)]

    # -- events (F12) --------------------------------------------------------
    def log_event(self, e: dict) -> None:
        self._run(
            """INSERT INTO events(run_id,ts,agent,model_used,fallback_step,status,latency_ms,validation,summary,error)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (e["run_id"], e["ts"], e["agent"], e["model_used"], e["fallback_step"], e["status"],
             e["latency_ms"], e["validation"], e["summary"], e.get("error", "")),
        )

    def events(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # -- generic cache -------------------------------------------------------
    def cache_get(self, key: str) -> Optional[Any]:
        row = self._one("SELECT value FROM cache WHERE key=?", (key,))
        return json.loads(row["value"]) if row else None

    def cache_set(self, key: str, value: Any) -> None:
        self._run("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, json.dumps(value)))

    def cache_dump(self, prefix: str = "") -> dict:
        rows = self._all("SELECT key,value FROM cache WHERE key LIKE ?", (prefix + "%",))
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def cache_load(self, entries: dict) -> int:
        for k, v in entries.items():
            self.cache_set(k, v)
        return len(entries)
