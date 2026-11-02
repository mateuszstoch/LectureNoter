"""LectureNoter: serwer API (FastAPI) dla interfejsu. Nasluchuje tylko na 127.0.0.1."""
import argparse
import asyncio
import json
from pathlib import Path

import contextlib

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from gpu_lock import GpuLock
from main import STAGES
from ollama_client import OllamaClient, pick_profile, total_vram_gb, vram_used_gb
from ui.jobs import JobError, JobManager
from ui.library import Library, LibraryError

ROOT = Path(__file__).resolve().parent


class AddVideo(BaseModel):
    files: list[str]
    enqueue: bool = True
    profile: str = "auto"
    language: str | None = None


class RunRequest(BaseModel):
    from_stage: str | None = None
    only_stage: str | None = None
    force: bool = False


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
        return {"stage": stage, "removes": guard(lambda: lib.impact(vid, stage))}

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

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
