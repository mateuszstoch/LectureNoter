"""LectureNoter: serwer API (FastAPI) dla interfejsu. Nasluchuje tylko na 127.0.0.1."""
import argparse
import asyncio
import json
from pathlib import Path

import contextlib
import shutil
import subprocess
import tempfile

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from gpu_lock import GpuLock
from main import STAGES
from ollama_client import OllamaClient, pick_profile, total_vram_gb, vram_used_gb
from ui import materials, notes as notes_mod
from ui.jobs import JobError, JobManager
from ui.library import Library, LibraryError

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


def create_app(cfg, videos_dir=None, work_dir=None, jobs=None, start_jobs=True, extra_hosts=()):
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

    # statyczny front (PWA) na koncu, zeby nie przykrywal tras /api
    app.mount("/", NoCacheStatic(directory=ROOT / "ui" / "static", html=True), name="static")
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info",
                timeout_graceful_shutdown=2)  # nie czekaj w nieskonczonosc na otwarte strumienie SSE


if __name__ == "__main__":
    main()
