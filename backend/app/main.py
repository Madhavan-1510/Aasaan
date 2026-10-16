"""FastAPI app: JSON API for the five-screen UI + static frontend.

Run from the repo root:  python -m uvicorn app.main:create_app --factory --app-dir backend --reload
Mode: AASAAN_MODE=mock (offline, no keys) or live (needs GEMINI_API_KEY). Default: live if a key is set.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Store
from .orchestrator import Engine, UserError
from .router import ModelRouter

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def load_env(path: Path = ROOT / ".env") -> None:
    """Tiny .env reader (KEY=value per line) so API keys can be added last without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---- request bodies (module level so FastAPI can resolve annotations)
class LearnerIn(BaseModel):
    grade: int = 9
    language: str = "en"
    goal: Optional[str] = None

class NodeIn(BaseModel):
    learner_id: str
    path_id: str
    node_id: str

class AnswerIn(NodeIn):
    session_id: str
    question_id: str
    answer: str
    attempt: int = 1

class FinishIn(NodeIn):
    session_id: str

class CloseIn(BaseModel):
    learner_id: str
    session_id: str
    path_id: Optional[str] = None

class SessionIn(BaseModel):
    learner_id: str

class KillIn(BaseModel):
    model: Optional[str] = None  # None on revive = revive all


def create_app(db_path: Optional[str] = None, mode: Optional[str] = None, self_test_on_start: Optional[bool] = None) -> FastAPI:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(db_path or os.environ.get("AASAAN_DB", str(ROOT / "data" / "aasaan.db")))
    router = ModelRouter(store=store, mode=mode)
    demo_cache = ROOT / "data" / "demo_cache.json"  # pre-warmed lessons/quizzes (scripts/warm_cache.py)
    if demo_cache.exists():
        store.cache_load(json.loads(demo_cache.read_text(encoding="utf-8")))
    engine = Engine(store, router)
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Live mode: test every model at startup so a wrong id or key is loud, not a silent fallback.
        if self_test_on_start if self_test_on_start is not None else router.mode == "live":
            threading.Thread(target=router.log_self_test, name="aasaan-self-test", daemon=True).start()
        yield

    app = FastAPI(title="Aasaan", lifespan=lifespan)
    app.state.engine, app.state.router, app.state.store = engine, router, store

    @app.middleware("http")
    async def no_cache_ui(request, call_next):  # always serve the latest UI files during development
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.exception_handler(UserError)
    async def user_error(_, exc: UserError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # ---- endpoints
    @app.get("/api/health")
    @app.get("/health")
    def health():
        return {"ok": True, "mode": router.mode, "killed": sorted(router.killed)}

    @app.get("/api/health/models")
    def health_models(refresh: bool = False):
        """Calls every configured model once; ok / failed (with the error) per model. Cached for 60 s."""
        return router.self_test(force=refresh)

    @app.post("/api/learners")
    def create_learner(body: LearnerIn):
        return {"learner_id": engine.create_learner(body.grade, body.language, body.goal)}

    @app.post("/api/session/open")
    def session_open(body: SessionIn):
        return engine.open_session(body.learner_id)

    @app.post("/api/start")
    async def start(
        learner_id: str = Form(...),
        session_id: Optional[str] = Form(None),
        language: Optional[str] = Form(None),
        goal: Optional[str] = Form(None),
        text: Optional[str] = Form(None),
        file: Optional[UploadFile] = File(None),
    ):
        pdf, images = None, None
        if file is not None:
            data = await file.read()
            ctype = file.content_type or ""
            if ctype == "application/pdf" or (file.filename or "").lower().endswith(".pdf"):
                pdf = data
            elif ctype.startswith("image/"):
                images = [(ctype, data)]
            else:
                raise HTTPException(400, "Upload a photo, a PDF, or paste text.")
        return engine.start(learner_id, session_id, text, pdf, images, language, goal)

    @app.get("/api/courses")
    def courses(learner_id: str):
        return engine.list_courses(learner_id)

    @app.get("/api/path/{path_id}")
    def get_path(path_id: str):
        return engine.get_path(path_id)

    @app.post("/api/lesson")
    def lesson(body: NodeIn):
        return engine.lesson(body.learner_id, body.path_id, body.node_id)

    @app.post("/api/quiz")
    def quiz(body: NodeIn):
        return engine.quiz(body.learner_id, body.path_id, body.node_id)

    @app.post("/api/answer")
    def answer(body: AnswerIn):
        return engine.answer(body.learner_id, body.session_id, body.path_id, body.node_id, body.question_id,
                            body.answer, body.attempt)

    @app.post("/api/finish-node")
    def finish_node(body: FinishIn):
        return engine.finish_node(body.learner_id, body.session_id, body.path_id, body.node_id)

    @app.post("/api/session/close")
    def session_close(body: CloseIn):
        return engine.close_session(body.learner_id, body.session_id, body.path_id)

    @app.post("/api/doubt")
    async def doubt(
        learner_id: str = Form(...),
        path_id: str = Form(...),
        node_id: Optional[str] = Form(None),
        text: Optional[str] = Form(None),
        audio: Optional[UploadFile] = File(None),
    ):
        data = await audio.read() if audio is not None else None
        return engine.doubt(learner_id, path_id, node_id, text, data)

    # ---- internal / demo helpers (no UI exposure; events kept for a future engine view)
    @app.get("/api/events")
    def events(limit: int = 100):
        return store.events(limit)

    @app.post("/api/admin/kill")
    def kill(body: KillIn):
        if not body.model:
            raise HTTPException(400, "model required")
        router.kill(body.model)
        return {"killed": sorted(router.killed)}

    @app.post("/api/admin/revive")
    def revive(body: KillIn):
        router.revive(body.model)
        return {"killed": sorted(router.killed)}


    # ---- frontend
    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

        @app.get("/")
        def index():
            return FileResponse(FRONTEND / "index.html")

    return app

