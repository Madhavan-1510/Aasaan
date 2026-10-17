"""Aasaan API + static frontend.

Run from the repo root:  python -m uvicorn app.main:create_app --factory --app-dir backend
Mode: AASAAN_MODE=mock (clearly labelled demo mode, no keys) or live (needs GEMINI_API_KEY; automatic when the key is set).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Store
from .engine import Engine, UserError
from .resilience import mask_key
from .router import ModelRouter
from .tools import ToolRunner
from . import visuals
from .visuals.cover import cover_svg
from .worker import Worker

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def load_env(path: Path = ROOT / ".env") -> None:
    """Tiny .env reader (KEY=value per line) so keys can be added without extra dependencies."""
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
    pass


class SessionIn(BaseModel):
    learner_id: str


class AnswerIn(BaseModel):
    learner_id: str
    session_id: str
    question_id: str
    answer: str
    attempt: int = 1


class FinishIn(BaseModel):
    learner_id: str
    session_id: str


class CloseIn(BaseModel):
    learner_id: str
    session_id: str
    notebook_id: Optional[str] = None


class BlockIn(BaseModel):
    markdown: str


class TtsIn(BaseModel):
    text: str
    language: str = "en"


class KillIn(BaseModel):
    model: Optional[str] = None


class ChaosIn(BaseModel):
    spec: str = ""


def create_app(db_path: Optional[str] = None, mode: Optional[str] = None, self_test_on_start: Optional[bool] = None,
               start_worker: bool = True, upload_dir: Optional[str] = None) -> FastAPI:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(db_path or os.environ.get("AASAAN_DB") or str(ROOT / "data" / "aasaan.db"))
    router = ModelRouter(store=store, mode=mode)
    tools = ToolRunner(store, router.sink, router.chaos)
    visuals.register(tools)
    engine = Engine(store, router, upload_dir=upload_dir or os.environ.get("AASAAN_UPLOADS") or str(ROOT / "data" / "uploads"), tools=tools)
    worker = Worker(engine)
    nb_per_hour = int(os.environ.get("AASAAN_NB_PER_HOUR", "12"))
    creations: dict[str, list[float]] = {}
    log_ = logging.getLogger("aasaan")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if router.mode == "mock":
            log_.warning("[aasaan] DEMO MODE: no model is called; set GEMINI_API_KEY for real generation")
        if self_test_on_start if self_test_on_start is not None else router.mode == "live":
            threading.Thread(target=router.log_self_test, name="aasaan-self-test", daemon=True).start()
        task = asyncio.create_task(worker.run()) if start_worker else None
        yield
        worker.stop()
        if task:
            task.cancel()

    app = FastAPI(title="Aasaan", lifespan=lifespan)
    app.state.engine, app.state.router, app.state.store, app.state.worker, app.state.tools = engine, router, store, worker, tools

    @app.middleware("http")
    async def no_cache_ui(request, call_next):  # always serve the latest UI files
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.exception_handler(UserError)
    async def user_error(_, exc: UserError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    def require_admin(request: Request) -> None:
        """Internal endpoints: need AASAAN_ADMIN_TOKEN (header X-Admin-Token) when set, else localhost only."""
        token = os.environ.get("AASAAN_ADMIN_TOKEN")
        if token:
            if request.headers.get("x-admin-token") != token:
                raise HTTPException(403, "admin token required")
        elif (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "internal endpoint: localhost only (set AASAAN_ADMIN_TOKEN to allow remote use)")

    # ================================================================ health
    @app.get("/api/health")
    @app.get("/health")
    def health():
        return {"ok": True, "app": "aasaan", "mode": router.mode, "killed": sorted(router.killed)}

    @app.get("/api/health/models")
    def health_models(refresh: bool = False):
        """Calls every configured model once; ok / failed (with the error) per model. Cached for 60 s."""
        return router.self_test(force=refresh)

    @app.get("/api/health/tools")
    def health_tools():
        """State of every tool: ok, degraded, open_circuit or disabled, with last error, last success and masked keys."""
        decision = {}
        for name, mc in router.cfg["models"].items():
            if mc.get("kind") != "decision":
                continue
            env = router.cfg["providers"].get(mc["provider"], {}).get("api_key_env")
            left = router.circuit_open(name)
            decision[name] = {"state": "disabled" if (env and not os.environ.get(env)) else ("open_circuit" if left > 0 else "ok"),
                              "model_id": mc["id"], "keys": [mask_key(env)] if env else [], "chaos": router.chaos.mode_for("model", name),
                              "killed": name in router.killed}
        return {"tools": tools.health(), "decision_models": decision, "chaos": router.chaos.describe(), "mode": router.mode}

    # ============================================================== learners
    @app.post("/api/learners")
    def create_learner(_: LearnerIn = LearnerIn()):
        import uuid
        return {"learner_id": str(uuid.uuid4())}

    @app.post("/api/session/open")
    def session_open(body: SessionIn):
        return {"session_id": engine.open_session(body.learner_id)}

    @app.post("/api/session/close")
    def session_close(body: CloseIn):
        return engine.close_session(body.learner_id, body.session_id, body.notebook_id)

    # ============================================================= notebooks
    @app.get("/api/notebooks")
    def notebooks(learner_id: str):
        return engine.list_notebooks(learner_id)

    @app.post("/api/notebooks")
    async def create_notebook(
        request: Request,
        learner_id: str = Form(...),
        text: str = Form(""),
        language: str = Form("en"),
        files: list[UploadFile] = File(default=[]),
    ):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in creations.get(ip, []) if now - t < 3600]
        if len(recent) >= nb_per_hour:
            raise HTTPException(429, "You have created a lot of notebooks this hour. Please try again later.")
        data = [(f.filename or "file", f.content_type or "", await f.read()) for f in files if f is not None and f.filename]
        nid = await run_in_threadpool(engine.create_notebook, learner_id, text, language, data)
        creations[ip] = recent + [now]
        return {"notebook_id": nid}

    @app.get("/api/notebooks/{nid}/cover.svg")
    def notebook_cover(nid: str):
        """Generated cover (deterministic from the title): no network, no model."""
        from fastapi.responses import Response
        nb = engine.notebook_view(nid)
        return Response(cover_svg(nb["title"], nb.get("goal") or ""), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/notebooks/{nid}")
    def notebook(nid: str):
        return engine.notebook_view(nid)

    @app.delete("/api/notebooks/{nid}")
    def delete_notebook(nid: str):
        engine.delete_notebook(nid)
        return {"ok": True}

    @app.post("/api/notebooks/{nid}/retry")
    def retry_notebook(nid: str):
        engine.retry_notebook(nid)
        return {"ok": True}

    @app.get("/api/notebooks/{nid}/stream")
    async def stream(nid: str, request: Request):
        """Server-sent events: a fresh notebook snapshot whenever anything changes (chapters filling in live)."""
        engine._nb(nid)

        async def gen():
            last, idle = None, 0
            while not await request.is_disconnected():
                view = await run_in_threadpool(engine.notebook_view, nid)
                payload = json.dumps(view)
                if payload != last:
                    last, idle = payload, 0
                    yield f"data: {payload}\n\n"
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": ping\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/notebooks/{nid}/companion")
    def companion_open(nid: str, learner_id: str):
        return engine.companion_open(learner_id, nid)

    # ============================================================== chapters
    @app.get("/api/chapters/{cid}")
    def chapter(cid: int):
        return engine.chapter_view(cid)

    @app.post("/api/chapters/{cid}/open")
    def chapter_open(cid: int):
        engine.open_chapter(cid)
        return {"ok": True}

    @app.post("/api/chapters/{cid}/prioritize")
    def chapter_prioritize(cid: int):
        return engine.prioritize(cid)

    @app.post("/api/chapters/{cid}/retry")
    def chapter_retry(cid: int):
        engine.retry_chapter(cid)
        return {"ok": True}

    @app.post("/api/chapters/{cid}/recheck")
    async def chapter_recheck(cid: int):
        return await run_in_threadpool(engine.recheck_chapter, cid)

    @app.put("/api/blocks/{bid}")
    def edit_block(bid: int, body: BlockIn):
        engine.edit_block(bid, {"markdown": body.markdown})
        return {"ok": True}

    @app.post("/api/chapters/{cid}/answer")
    def answer(cid: int, body: AnswerIn):
        return engine.answer(body.learner_id, body.session_id, cid, body.question_id, body.answer, body.attempt)

    @app.post("/api/chapters/{cid}/finish")
    def finish(cid: int, body: FinishIn):
        return engine.finish_chapter(body.learner_id, body.session_id, cid)

    @app.post("/api/chapters/{cid}/doubt")
    async def doubt(cid: int, learner_id: str = Form(...), text: Optional[str] = Form(None), audio: Optional[UploadFile] = File(None)):
        data = await audio.read() if audio is not None else None
        return await run_in_threadpool(engine.doubt, learner_id, cid, text, data)

    # ================================================================= voice
    @app.post("/api/voice/stt")
    async def stt(language: str = Form("en"), audio: UploadFile = File(...)):
        data = await audio.read()
        if len(data) > 2_000_000:
            raise HTTPException(400, "Recording too long. Keep it under 30 seconds.")
        return await run_in_threadpool(engine.transcribe, data, language)

    @app.post("/api/voice/tts")
    async def tts(body: TtsIn):
        return await run_in_threadpool(engine.speak, body.text, body.language)

    # ======================================== internal / demo helpers (not in the UI)
    @app.get("/api/events")
    def events(request: Request, limit: int = 100):
        require_admin(request)
        return store.events(limit)

    @app.post("/api/admin/kill")
    def kill(request: Request, body: KillIn):
        require_admin(request)
        if not body.model:
            raise HTTPException(400, "model required")
        router.kill(body.model)
        return {"killed": sorted(router.killed)}

    @app.get("/api/admin/chaos")
    def get_chaos(request: Request):
        require_admin(request)
        return {"spec": router.chaos.describe()}

    @app.post("/api/admin/chaos")
    def set_chaos(request: Request, body: ChaosIn):
        """e.g. {"spec": "tool:commons=fail,model:jev=fail"}; empty string clears it."""
        require_admin(request)
        router.chaos.set(body.spec)
        return {"spec": router.chaos.describe()}

    @app.post("/api/admin/revive")
    def revive(request: Request, body: KillIn):
        require_admin(request)
        router.revive(body.model)
        return {"killed": sorted(router.killed)}

    # ================================================================ frontend
    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

        @app.get("/")
        def index():
            return FileResponse(FRONTEND / "index.html")

    return app
