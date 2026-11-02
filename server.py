"""LectureNoter: serwer API (FastAPI) dla interfejsu. Nasluchuje tylko na 127.0.0.1."""
import argparse
import asyncio
import json
import logging
from pathlib import Path

import contextlib
import shutil
import subprocess
import tempfile
import threading

import uvicorn
import yaml
from loguru import logger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from diagnostics import hint_for
from gpu_lock import GpuLock
from io_utils import read_json
from main import STAGES
from ollama_client import OllamaClient, pick_profile, total_vram_gb, vram_used_gb
from ui import materials, notes as notes_mod
from ui.chat import ChatError, ChatService, ChatStore
from ui.jobs import JobError, JobManager
from ui.library import Library, LibraryError
from run_state import is_live, read_run_state

ROOT = Path(__file__).resolve().parent


class NoCacheStatic(StaticFiles):
    """Pliki aplikacji zawsze rewalidowane (service worker i tak trzyma kopie na tryb offline)."""

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


class AddVideo(BaseModel):
    files: list[str]
    enqueue: bool = True
    profile: str = "auto"
    language: str | None = None


class RunRequest(BaseModel):
    from_stage: str | None = None
    only_stage: str | None = None
    force: bool = False


class NotesBody(BaseModel):
    md: str


class ChatCreate(BaseModel):
    title: str | None = None


class ChatMessage(BaseModel):
    content: str
    model: str | None = None


class MoveRequest(BaseModel):
    position: int


def tail_lines(path, n=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:], size
    except OSError:
        return [], 0


def create_app(cfg, videos_dir=None, work_dir=None, jobs=None, start_jobs=True, extra_hosts=(), ollama=None):
    videos_dir = Path(videos_dir or ROOT / cfg["paths"]["videos_dir"])
    work_dir = Path(work_dir or ROOT / cfg["paths"]["work_dir"])
    lib = Library(videos_dir, work_dir, cfg)
    gpu = GpuLock(work_dir / ".gpu.lock")
    jm = jobs or JobManager(lib, gpu)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        if start_jobs:
            jm.start()
        yield
        jm.stop()

    app = FastAPI(title="LectureNoter", lifespan=lifespan)
    ollama = ollama or OllamaClient(timeout=cfg.get("chat", {}).get("timeout_s", 600))
    chat = ChatService(lib, gpu, cfg, ollama)
    jm.set_preempt(chat.preempt)  # zadanie czekajace na GPU zwalnia model czatu (gdy ten nie generuje)
    app.state.chat = chat
    # ochrona przed DNS rebinding: tylko lokalne nazwy hosta
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["localhost", "127.0.0.1", "[::1]", *extra_hosts])
    app.state.lib, app.state.jobs, app.state.gpu = lib, jm, gpu

    def guard(fn):
        try:
            return fn()
        except (LibraryError, JobError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/system")
    def system():
        client = OllamaClient(timeout=3, retries=1)
        ollama = {"ok": False, "loaded": [], "installed": []}
        try:
            import requests
            tags = requests.get(f"{client.host}/api/tags", timeout=3).json()
            ollama.update(ok=True, installed=[m["name"] for m in tags.get("models", [])],
                          loaded=client.loaded_models())
        except Exception:
            pass
        return {"ollama": ollama, "gpu_holder": gpu.holder(), "profile": pick_profile(cfg),
                "pandoc": bool(shutil.which("pandoc")),
                "paths": {"videos_dir": str(lib.videos_dir.resolve()), "work_dir": str(lib.work_dir.resolve())},
                "vram": {"total_gb": total_vram_gb(), "used_gb": vram_used_gb()},
                "models": {"vlm": cfg["vlm"]["model"],
                           "llm": cfg["profiles"][pick_profile(cfg)]["llm_model"]}}

    @app.get("/api/library/files")
    def files():
        return lib.list_files()

    @app.get("/api/videos")
    def videos():
        return lib.list_videos()

    @app.post("/api/videos")
    def add(req: AddVideo):
        def go():
            ids = []
            for f in req.files:
                vid = lib.add_video(f, req.profile, req.language)
                ids.append(vid)
                if req.enqueue:
                    try:
                        jm.enqueue(vid)
                    except JobError:
                        pass  # juz w kolejce
            return {"ids": ids}
        return guard(go)

    @app.get("/api/videos/{vid}")
    def video(vid: str):
        return guard(lambda: lib.summary(vid))

    def ensure_deletable(vid):
        wd = guard(lambda: lib.workdir(vid))
        if jm.has_active(vid) or is_live(read_run_state(wd)):
            raise HTTPException(409, "Film jest w kolejce lub trwa jego przetwarzanie. Najpierw anuluj zadanie.")
        if chat.is_generating_for(vid):
            raise HTTPException(409, "Trwa odpowiedz czatu dla tego wykladu. Zatrzymaj ja lub poczekaj.")

    @app.get("/api/videos/{vid}/delete-preview")
    def delete_preview(vid: str):
        ensure_deletable(vid)
        return guard(lambda: lib.delete_preview(vid))

    @app.delete("/api/videos/{vid}")
    def delete_video(vid: str, delete_video: bool = False):
        ensure_deletable(vid)
        res = guard(lambda: lib.delete(vid, delete_video))
        jm.forget(vid)
        logger.info(f"usunieto wyklad {vid} (plik wideo usuniety: {res['video_deleted']})")
        return res

    @app.get("/api/videos/{vid}/impact")
    def impact(vid: str, stage: str):
        if stage not in STAGES:
            raise HTTPException(400, "nieznany etap")
        removes = guard(lambda: lib.impact(vid, stage))
        edited = (lib.workdir(vid) / notes_mod.EDITED).exists()
        return {"stage": stage, "removes": removes, "keeps_edited_notes": edited and "llm" in removes}

    @app.post("/api/videos/{vid}/run")
    def run(vid: str, req: RunRequest):
        return guard(lambda: jm.enqueue(vid, req.from_stage, req.only_stage, req.force))

    @app.get("/api/queue")
    def queue():
        return jm.snapshot()

    @app.post("/api/queue/pause")
    def pause():
        jm.set_paused(True)
        return jm.snapshot()

    @app.post("/api/queue/resume")
    def resume():
        jm.set_paused(False)
        return jm.snapshot()

    @app.post("/api/queue/{job_id}/cancel")
    def cancel(job_id: str):
        return guard(lambda: jm.cancel(job_id))

    @app.post("/api/queue/{job_id}/move")
    def move(job_id: str, req: MoveRequest):
        guard(lambda: jm.move(job_id, req.position))
        return jm.snapshot()

    # --- notatki ---
    def wd_of(vid):
        return guard(lambda: lib.workdir(vid))

    def notes_guard(fn):
        try:
            return fn()
        except notes_mod.NotesMissing as e:
            raise HTTPException(404, str(e))
        except notes_mod.NotesError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/videos/{vid}/notes")
    def get_notes(vid: str):
        return notes_guard(lambda: notes_mod.get_active(wd_of(vid)))

    @app.put("/api/videos/{vid}/notes")
    def put_notes(vid: str, body: NotesBody):
        return notes_guard(lambda: notes_mod.save_edited(wd_of(vid), body.md))

    @app.delete("/api/videos/{vid}/notes/edited")
    def revert_notes(vid: str):
        return notes_guard(lambda: notes_mod.revert_edited(wd_of(vid)))

    @app.get("/api/videos/{vid}/notes/versions")
    def versions(vid: str):
        return notes_mod.list_versions(wd_of(vid))

    @app.get("/api/videos/{vid}/notes/versions/{ver}")
    def version(vid: str, ver: str):
        return {"id": ver, "md": notes_guard(lambda: notes_mod.read_version(wd_of(vid), ver))}

    @app.get("/api/videos/{vid}/notes/export")
    def export(vid: str, format: str = "md"):
        data = notes_guard(lambda: notes_mod.get_active(wd_of(vid)))
        if format == "md":
            return Response(data["md"], media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{vid}.md"'})
        if format == "docx":
            if not shutil.which("pandoc"):
                raise HTTPException(501, "Eksport do Worda wymaga programu pandoc (np. sudo apt install pandoc).")
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "notes.docx"
                r = subprocess.run(["pandoc", "-f", "markdown+tex_math_dollars", "-t", "docx", "-o", str(out)],
                                   input=data["md"].encode("utf-8"), capture_output=True)
                if r.returncode != 0:
                    raise HTTPException(500, f"pandoc: {r.stderr.decode('utf-8', 'replace')[:300]}")
                return Response(out.read_bytes(),
                                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                headers={"Content-Disposition": f'attachment; filename="{vid}.docx"'})
        raise HTTPException(400, "format: md lub docx (PDF: drukowanie z przegladarki)")

    # --- materialy zrodlowe ---
    @app.get("/api/videos/{vid}/transcript")
    def get_transcript(vid: str):
        return materials.transcript(wd_of(vid))

    @app.get("/api/videos/{vid}/frames")
    def get_frames(vid: str):
        return materials.frames(wd_of(vid))

    @app.get("/api/videos/{vid}/frames/{name}")
    def get_frame(vid: str, name: str):
        p = materials.frame_path(wd_of(vid), name)
        if not p:
            raise HTTPException(404, "nie ma takiej klatki")
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/videos/{vid}/context")
    def get_context(vid: str, t: float):
        return materials.context_at(wd_of(vid), t)

    # --- czat ---
    def chat_guard(fn):
        try:
            return fn()
        except ChatError as e:
            raise HTTPException(404 if "nie ma takiej" in str(e) else 400, str(e))

    @app.get("/api/chat/status")
    def chat_status():
        st = chat.status()
        st["default_model"] = chat.default_model()
        return st

    @app.post("/api/chat/release")
    def chat_release():
        if not chat.release():
            raise HTTPException(409, "Trwa generowanie odpowiedzi.")
        return chat.status()

    @app.get("/api/videos/{vid}/chats")
    def chats(vid: str):
        return ChatStore(wd_of(vid)).list()

    @app.post("/api/videos/{vid}/chats")
    def chat_new(vid: str, req: ChatCreate):
        return ChatStore(wd_of(vid)).create(req.title)

    @app.get("/api/videos/{vid}/chats/{cid}")
    def chat_get(vid: str, cid: str):
        return chat_guard(lambda: ChatStore(wd_of(vid)).get(cid))

    @app.delete("/api/videos/{vid}/chats/{cid}")
    def chat_delete(vid: str, cid: str):
        chat_guard(lambda: ChatStore(wd_of(vid)).delete(cid))
        return {"ok": True}

    @app.post("/api/videos/{vid}/chats/{cid}/messages")
    async def chat_message(vid: str, cid: str, req: ChatMessage, request: Request):
        wd_of(vid)
        chat_guard(lambda: ChatStore(wd_of(vid)).get(cid))

        def sse(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async def gen():
            cancel = threading.Event()
            it = chat.stream_answer(vid, cid, req.content, req.model, cancel)
            try:
                while True:
                    nxt = asyncio.ensure_future(asyncio.to_thread(next, it, None))
                    while True:
                        # Starlette nie nasluchuje rozlaczenia w trakcie odpowiedzi, wiec sprawdzamy je sami co sekunde:
                        # inaczej Stop w czasie ladowania modelu/przetwarzania promptu nic by nie zatrzymal
                        done, _ = await asyncio.wait({nxt}, timeout=1.0)
                        if done:
                            break
                        if not cancel.is_set() and await request.is_disconnected():
                            cancel.set()
                    item = await nxt
                    if item is None:
                        break
                    yield sse(*item)
                    if cancel.is_set():
                        break
            finally:
                cancel.set()  # przerywa petle tokenow (takze gdy generator wlasnie dziala w watku)
                try:
                    it.close()  # zachowuje czesciowa odpowiedz i zwalnia stan generowania
                except ValueError:
                    pass  # generator jeszcze w trakcie: zakonczy sie sam po zobaczeniu `cancel`
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def build_state():
        return {"queue": jm.snapshot(), "videos": lib.list_videos(), "gpu_holder": gpu.holder()}
    app.state.build_state = build_state

    @app.get("/api/stream")
    async def stream(request: Request, once: bool = False):
        async def gen():
            last, idle = None, 0
            while not await request.is_disconnected():
                payload = json.dumps(build_state(), sort_keys=True, ensure_ascii=False)
                if payload != last:
                    yield f"event: state\ndata: {payload}\n\n"
                    last, idle = payload, 0
                elif idle >= 15:
                    yield ": ping\n\n"
                    idle = 0
                if once:
                    return
                idle += 1
                await asyncio.sleep(1)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/videos/{vid}/logs")
    async def logs(vid: str, request: Request, once: bool = False):
        wd = guard(lambda: lib.workdir(vid))
        path = wd / "pipeline.log"

        async def gen():
            lines, pos = tail_lines(path)
            for l in lines:
                yield f"data: {json.dumps(l, ensure_ascii=False)}\n\n"
            while not once and not await request.is_disconnected():
                await asyncio.sleep(1)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < pos:
                    pos = 0
                if size > pos:
                    with open(path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(size - pos).decode("utf-8", "replace")
                    pos = size
                    for l in chunk.splitlines():
                        yield f"data: {json.dumps(l, ensure_ascii=False)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- logi i diagnostyka ---
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception(f"nieobsluzony wyjatek: {request.method} {request.url.path}")
        return JSONResponse({"detail": f"Blad wewnetrzny serwera: {type(exc).__name__}: {exc} (szczegoly w logu serwera)"}, status_code=500)

    @app.get("/api/server-log")
    def server_log(lines: int = 300):
        text, _ = tail_lines(work_dir / "server.log", min(max(lines, 1), 2000))
        return {"path": str(work_dir / "server.log"), "lines": text}

    @app.get("/api/videos/{vid}/diagnostics")
    def diagnostics(vid: str):
        wd = guard(lambda: lib.workdir(vid))
        rs = read_run_state(wd)
        stage = rs.get("error_stage") or rs.get("current_stage")
        err = rs.get("error")

        def tail(name, n):
            return tail_lines(wd / name, n)[0]
        return {"status": rs.get("status"), "stage": stage, "error": err, "hint": hint_for(err or ""),
                "worker_log": tail(f"worker_{stage}.log", 80) if stage else [],
                "pipeline_log": tail("pipeline.log", 80), "job_output": tail("job.out", 60)}

    # statyczny front (PWA) na koncu, zeby nie przykrywal tras /api
    app.mount("/", NoCacheStatic(directory=ROOT / "ui" / "static", html=True), name="static")
    return app


class InterceptHandler(logging.Handler):
    """Przekierowuje logi biblioteki `logging` (uvicorn, fastapi) do loguru, czyli tez do server.log."""

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(exception=record.exc_info).log(level, record.getMessage())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    work_dir = ROOT / cfg["paths"]["work_dir"]
    work_dir.mkdir(parents=True, exist_ok=True)
    logger.add(work_dir / "server.log", rotation="5 MB", retention=3, encoding="utf-8", enqueue=True)
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    logger.info(f"LectureNoter: start serwera na {args.host}:{args.port}")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info", log_config=None, access_log=False,
                timeout_graceful_shutdown=2)  # nie czekaj w nieskonczonosc na otwarte strumienie SSE


if __name__ == "__main__":
    main()
