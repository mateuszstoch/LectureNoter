"""LectureNoter: serwer API (FastAPI) dla interfejsu. Nasluchuje tylko na 127.0.0.1."""
import argparse
import asyncio
import faulthandler
import json
import logging
import os
import re
import signal
from pathlib import Path

import contextlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import uvicorn
import yaml
from loguru import logger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config_loader import LiveConfig, resolve
from diagnostics import hint_for
from gpu_lock import GpuLock
from io_utils import read_json
from main import STAGES
from ollama_client import OllamaClient, apply_profile, has_cuda, pick_profile, total_vram_gb, vram_used_gb
from ui import materials, notes as notes_mod
from ui.chat import ChatBusy, ChatError, ChatService, ChatStore, export_markdown
from ui.quiz import QuizError, QuizService
from starlette.background import BackgroundTask

from ui import archive as archive_mod
from ui.folders import FolderError, FolderStore
from ui.jobs import JobError, JobManager
from ui.library import Library, LibraryError
from ui.lifecycle import AutoExit, ClientTracker, KeepAwake, Liveness, read_mem
from ui.search import SearchIndex
from ui.transfer import ConflictError, ExportManager, TransferError, free_bytes, import_lecture, staging_dir, summarize
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


class VideoPatch(BaseModel):
    title: str | None = None
    folder: str | None = None  # None (przy podanym polu) = przenies do korzenia


class FolderCreate(BaseModel):
    name: str
    parent: str | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent: str | None = None


class ExportRequest(BaseModel):
    include_video: bool = False


class ImportCommit(BaseModel):
    token: str
    folder: str | None = None
    folder_mode: str = "current"          # current | recreate
    on_conflict: str | None = None         # copy | replace | skip
    include_video: bool = True


class RelinkRequest(BaseModel):
    file: str


class NotesBody(BaseModel):
    md: str


class ChatCreate(BaseModel):
    title: str | None = None


class ChatMessage(BaseModel):
    content: str
    model: str | None = None


class QuizCreate(BaseModel):
    type: str
    count: int = 10
    sections: list[str] = []
    topic: str = ""
    model: str | None = None
    title: str | None = None


class QuizAnswer(BaseModel):
    question_id: str
    answer: str | list[int] | None = None
    self_score: float | None = None


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


def create_app(cfg, videos_dir=None, work_dir=None, jobs=None, start_jobs=True, extra_hosts=(), ollama=None,
               auto_exit=False, idle_grace=120.0, startup_grace=180.0):
    def C():
        return resolve(cfg)  # aktualna konfiguracja (przy LiveConfig przeladowana po zmianie pliku)

    videos_dir = Path(videos_dir or ROOT / C()["paths"]["videos_dir"])
    work_dir = Path(work_dir or ROOT / C()["paths"]["work_dir"])
    lib = Library(videos_dir, work_dir, cfg)
    gpu = GpuLock(work_dir / ".gpu.lock")
    jm = jobs or JobManager(lib, gpu)
    folders = FolderStore(work_dir)
    exports = ExportManager(work_dir)
    search_index = SearchIndex(lib, folders)
    tracker = ClientTracker()

    def is_busy():
        """Praca, ktorej nie wolno przerywac: zadanie w toku, oczekujace (gdy kolejka nie jest wstrzymana) albo odpowiedz czatu."""
        snap = jm.snapshot()
        jobs_busy = any(j["status"] == "running" or (j["status"] == "queued" and not snap["paused"]) for j in snap["jobs"])
        return jobs_busy or chat.status()["generating"]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        loops = []
        if start_jobs:
            jm.start()
            loops.append(KeepAwake(is_busy).start())  # komputer nie zasypia w trakcie przetwarzania
            if auto_exit:
                def exit_now():
                    app.state.exit_reason = ae.reason or "auto-exit"
                    app.state.request_exit()
                ae = AutoExit(tracker, is_busy, exit_now, idle_grace, startup_grace)
                loops.append(ae.start())
        yield
        for lp in loops:
            lp.stop()
        jm.stop()
        chat.release()  # rozladuj model czatu i zwolnij GPU przy zamykaniu serwera

    app = FastAPI(title="LectureNoter", lifespan=lifespan)
    ollama = ollama or OllamaClient(timeout=C().get("chat", {}).get("timeout_s", 600))
    chat = ChatService(lib, gpu, cfg, ollama)
    quizzes = QuizService(lib, chat, cfg, ollama)
    jm.set_preempt(chat.preempt)  # zadanie czekajace na GPU zwalnia model czatu (gdy ten nie generuje)
    app.state.chat, app.state.tracker, app.state.is_busy = chat, tracker, is_busy
    app.state.quizzes = quizzes
    app.state.auto_exit = auto_exit
    app.state.previous_exit, app.state.exit_reason, app.state.started_at = None, None, time.time()
    app.state.request_exit = lambda: None  # main() podstawia zatrzymanie serwera uvicorn
    # ochrona przed DNS rebinding: tylko lokalne nazwy hosta
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["localhost", "127.0.0.1", "[::1]", *extra_hosts])
    app.state.lib, app.state.jobs, app.state.gpu = lib, jm, gpu

    def guard(fn):
        try:
            return fn()
        except (LibraryError, JobError, FolderError, TransferError, archive_mod.ArchiveError) as e:
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
        return {"ollama": ollama, "gpu_holder": gpu.holder(), "profile": pick_profile(C()),
                "pandoc": bool(shutil.which("pandoc")),
                "auto_exit": auto_exit, "busy": is_busy(), "previous_exit": app.state.previous_exit,
                "uptime_s": round(time.time() - app.state.started_at), **{k: v for k, v in read_mem().items() if k == "rss_mb"},
                "paths": {"videos_dir": str(lib.videos_dir.resolve()), "work_dir": str(lib.work_dir.resolve())},
                "vram": {"total_gb": total_vram_gb(), "used_gb": vram_used_gb()},
                "device": "cuda" if has_cuda() else "cpu",
                "models": {"vlm": apply_profile(C(), pick_profile(C()))["vlm"]["model"],
                           "llm": C()["profiles"][pick_profile(C())]["llm_model"]}}

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
        if chat.is_generating_for(vid) or quizzes.is_generating(vid):
            raise HTTPException(409, "Trwa odpowiedz czatu lub generowanie quizu dla tego wykladu. Zatrzymaj je lub poczekaj.")

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

    # --- eksport i import wykladow (.lec) ---
    def require_header(request):
        if request.headers.get("x-lecturenoter") != "1":
            raise HTTPException(403, "brak wymaganego naglowka")

    def max_import_bytes():
        return int(float(C().get("import", {}).get("max_gb", 50)) * 1e9)

    def token_path(token):
        if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
            raise HTTPException(400, "niepoprawny token")
        return staging_dir(work_dir) / f"{token}.lec"

    @app.get("/api/videos/{vid}/export-info")
    def export_info(vid: str):
        guard(lambda: lib.workdir(vid))
        return exports.info(lib, vid)

    @app.post("/api/videos/{vid}/export")
    def export_start(vid: str, req: ExportRequest, request: Request):
        require_header(request)
        wd = guard(lambda: lib.workdir(vid))
        if jm.has_active(vid) or is_live(read_run_state(wd)):
            raise HTTPException(409, "Wykład jest w kolejce lub trwa jego przetwarzanie. Poczekaj na koniec, żeby archiwum było spójne.")
        return {"token": guard(lambda: exports.start(lib, folders, vid, req.include_video))}

    @app.get("/api/exports/{token}")
    def export_status(token: str):
        return guard(lambda: exports.status(token))

    @app.get("/api/exports/{token}/download")
    def export_download(token: str):
        path, filename = guard(lambda: exports.file(token))
        return FileResponse(path, media_type="application/octet-stream", filename=filename,
                            background=BackgroundTask(exports.discard, token))

    @app.delete("/api/exports/{token}")
    def export_discard(token: str, request: Request):
        require_header(request)
        exports.discard(token)
        return {"ok": True}

    @app.put("/api/import/upload")
    async def import_upload(request: Request, filename: str = "wyklad.lec"):
        require_header(request)
        limit = max_import_bytes()
        clen = request.headers.get("content-length")
        if clen and int(clen) > limit:
            raise HTTPException(413, f"Plik jest za duży (limit {limit / 1e9:.0f} GB, ustawienie import.max_gb w config.yaml).")
        token = uuid.uuid4().hex
        path = token_path(token)
        if clen and int(clen) * 1.05 + 100 * 1024 * 1024 > free_bytes(path.parent):
            raise HTTPException(507, "Za mało miejsca na dysku na przesłane archiwum.")
        written = 0
        try:
            with open(path, "wb") as f:
                buf = bytearray()
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(413, f"Plik jest za duży (limit {limit / 1e9:.0f} GB).")
                    buf += chunk
                    if len(buf) >= 4 * 1024 * 1024:  # zapis w watku, zeby nie blokowac petli zdarzen (SSE, API) przy gigabajtach
                        await asyncio.to_thread(f.write, bytes(buf))
                        buf.clear()
                if buf:
                    await asyncio.to_thread(f.write, bytes(buf))
            info = await asyncio.to_thread(archive_mod.inspect_archive, path, limit)
        except archive_mod.ArchiveError as e:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(e))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        summary = summarize(info, lib.find_by_uid(info["manifest"].get("uid")))
        summary.update(token=token, size=written, filename=filename)
        return summary

    @app.post("/api/import/commit")
    async def import_commit(req: ImportCommit, request: Request):
        require_header(request)
        path = token_path(req.token)
        if not path.exists():
            raise HTTPException(404, "Przesłane archiwum wygasło. Wybierz plik ponownie.")
        if req.folder_mode not in ("current", "recreate") or req.on_conflict not in (None, "copy", "replace", "skip"):
            raise HTTPException(400, "niepoprawne opcje importu")
        existing = None
        try:
            existing = lib.find_by_uid(archive_mod.inspect_archive(path, max_import_bytes())["manifest"].get("uid"))
        except archive_mod.ArchiveError as e:
            raise HTTPException(400, str(e))
        if existing and req.on_conflict == "replace":
            if jm.has_active(existing[0]) or is_live(read_run_state(lib.workdir(existing[0]))):
                raise HTTPException(409, "Zastępowany wykład jest w kolejce lub trwa jego przetwarzanie.")
        try:
            res = await asyncio.to_thread(import_lecture, lib, folders, path, work_dir=work_dir, cfg=cfg, folder=req.folder,
                                          folder_mode=req.folder_mode, on_conflict=req.on_conflict, include_video=req.include_video,
                                          max_bytes=max_import_bytes())
        except ConflictError as e:
            raise HTTPException(409, str(e))
        except (TransferError, archive_mod.ArchiveError) as e:
            raise HTTPException(400, str(e))
        path.unlink(missing_ok=True)
        logger.info(f"zaimportowano wyklad {res['id']} ({res['title']})" + (" [zastapiony]" if res.get("replaced") else ""))
        return res

    @app.delete("/api/import/{token}")
    def import_discard(token: str, request: Request):
        require_header(request)
        token_path(token).unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/videos/{vid}/relink")
    def relink(vid: str, req: RelinkRequest, request: Request):
        require_header(request)
        return guard(lambda: lib.relink(vid, req.file))

    # --- nazwy, foldery, wyszukiwanie ---
    def folders_with_counts():
        counts = {}
        for _, meta in lib.iter_meta():
            counts[meta.get("folder")] = counts.get(meta.get("folder"), 0) + 1
        return [dict(f, count=counts.get(f["id"], 0)) for f in folders.list()]

    @app.get("/api/folders")
    def list_folders():
        return folders_with_counts()

    @app.post("/api/folders")
    def create_folder(req: FolderCreate):
        fid = guard(lambda: folders.create(req.name, req.parent))
        return {"id": fid}

    @app.patch("/api/folders/{fid}")
    def patch_folder(fid: str, req: FolderPatch):
        kw = {}
        if "name" in req.model_fields_set and req.name is not None:
            kw["name"] = req.name
        if "parent" in req.model_fields_set:
            kw["parent"] = req.parent
        guard(lambda: folders.update(fid, **kw))
        return {"ok": True}

    @app.delete("/api/folders/{fid}")
    def delete_folder(fid: str):
        parent = guard(lambda: folders.delete(fid))
        moved = lib.reassign_folder(fid, parent)  # wyklady nie sa usuwane: trafiaja do folderu nadrzednego
        logger.info(f"usunieto folder {fid}; wyklady przeniesione: {moved}")
        return {"ok": True, "moved_lectures": moved, "moved_to": parent}

    @app.patch("/api/videos/{vid}")
    def patch_video(vid: str, req: VideoPatch):
        guard(lambda: lib.workdir(vid))
        if "title" in req.model_fields_set and req.title is not None:
            guard(lambda: lib.rename(vid, req.title))
        if "folder" in req.model_fields_set:
            if req.folder and not folders.exists(req.folder):
                raise HTTPException(400, "Nie ma takiego folderu.")
            guard(lambda: lib.set_folder(vid, req.folder))
        return guard(lambda: lib.summary(vid))

    @app.get("/api/search")
    def search(q: str = "", folder: str | None = None):
        return search_index.search(q, folder)

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

    @app.get("/api/videos/{vid}/chats/{cid}/export")
    def chat_export(vid: str, cid: str, format: str = "md"):
        if format != "md":
            raise HTTPException(400, "format: md (PDF: drukowanie z przegladarki)")
        wd = wd_of(vid)
        chat_ = chat_guard(lambda: ChatStore(wd).get(cid))
        title = (read_json(wd / "meta.json", {}) or {}).get("title")
        return Response(export_markdown(chat_, title), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{vid}-rozmowa-{cid}.md"'})

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

    # --- quizy ---
    def quiz_guard(fn):
        try:
            return fn()
        except ChatBusy as e:
            raise HTTPException(409, str(e))
        except QuizError as e:
            raise HTTPException(404 if "nie ma takiego" in str(e) else 400, str(e))

    @app.get("/api/videos/{vid}/quizzes")
    def quiz_list(vid: str):
        wd_of(vid)
        return quizzes.list(vid)

    @app.get("/api/videos/{vid}/quiz-scopes")
    def quiz_scopes(vid: str):
        wd_of(vid)
        return quizzes.scopes(vid)

    @app.post("/api/videos/{vid}/quizzes")
    def quiz_create(vid: str, req: QuizCreate):
        wd_of(vid)
        return quiz_guard(lambda: quizzes.create(vid, req.type, req.count, req.sections, req.topic, req.model, req.title))

    @app.get("/api/videos/{vid}/quizzes/{qid}")
    def quiz_get(vid: str, qid: str):
        wd_of(vid)
        return quiz_guard(lambda: quizzes.get(vid, qid))

    @app.delete("/api/videos/{vid}/quizzes/{qid}")
    def quiz_delete(vid: str, qid: str):
        wd_of(vid)
        quiz_guard(lambda: quizzes.delete(vid, qid))
        return {"ok": True}

    @app.post("/api/videos/{vid}/quizzes/{qid}/attempts")
    def quiz_attempt(vid: str, qid: str):
        wd_of(vid)
        return quiz_guard(lambda: quizzes.start_attempt(vid, qid))

    @app.post("/api/videos/{vid}/quizzes/{qid}/attempts/{aid}/answer")
    def quiz_answer(vid: str, qid: str, aid: str, req: QuizAnswer):
        wd_of(vid)
        return quiz_guard(lambda: quizzes.answer(vid, qid, aid, req.question_id, req.answer, req.self_score))

    def build_state():
        return {"queue": jm.snapshot(), "videos": lib.list_videos(), "gpu_holder": gpu.holder(), "folders": folders_with_counts()}
    app.state.build_state = build_state

    @app.get("/api/stream")
    async def stream(request: Request, once: bool = False):
        async def gen():
            tracker.enter()  # okno aplikacji jest polaczone (serwer z --auto-exit nie wylaczy sie)
            try:
                async for chunk in loop():
                    yield chunk
            finally:
                tracker.leave()

        async def loop():
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

    @app.post("/api/shutdown")
    def shutdown(request: Request, force: bool = False):
        """Wylacza serwer (tryb headless nie ma okna terminala). Wymaga naglowka X-LectureNoter, wiec obca strona
        w przegladarce nie moze tego wywolac (naglowek wymusza preflight CORS)."""
        if request.headers.get("x-lecturenoter") != "1":
            raise HTTPException(403, "brak wymaganego naglowka")
        if is_busy() and not force:
            raise HTTPException(409, "Trwa przetwarzanie lub odpowiedz czatu. Anuluj zadania albo poczekaj na koniec.")
        logger.info("wylaczanie serwera na zadanie uzytkownika")
        app.state.exit_reason = "zakonczono przyciskiem Zakoncz"
        threading.Timer(0.4, lambda: app.state.request_exit()).start()  # po wyslaniu odpowiedzi
        return {"ok": True}

    @app.post("/api/bye")
    def bye():
        """Wolane przez przegladarke przy zamykaniu okna (sendBeacon/pagehide): skraca karencje auto-exit. Bez naglowka
        ochronnego, bo sendBeacon nie pozwala go ustawic; skutek jest niegrozny (serwer bez okna i tak by sie wylaczyl)."""
        tracker.bye()
        return {"ok": True}

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


class LoggingServer(uvicorn.Server):
    """uvicorn z zapisem przyczyny zamkniecia (sygnal od systemu/uzytkownika zostawia slad w logu)."""

    def handle_exit(self, sig, frame):
        name = signal.Signals(sig).name if sig else "?"
        logger.warning(f"otrzymano sygnal {name}: zamykam serwer")
        state = self.config.app.state
        if not getattr(state, "exit_reason", None):
            state.exit_reason = f"sygnal {name}"
        lv = getattr(state, "liveness", None)
        if lv:  # uvicorn moze zakonczyc proces samym sygnalem przed 'finally': znacznik czystego zamkniecia zapisujemy tu
            lv.close(state.exit_reason)
        super().handle_exit(sig, frame)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--auto-exit", action="store_true", default=os.environ.get("LECTURENOTER_AUTO_EXIT") == "1",
                    help="wylacz serwer po zamknieciu okna aplikacji (gdy nic nie jest przetwarzane)")
    ap.add_argument("--idle-grace", type=float, default=120.0,
                    help="sekundy zerwanego polaczenia okna przed wylaczeniem (po jawnym zamknieciu okna: 15 s)")
    ap.add_argument("--startup-grace", type=float, default=180.0, help="sekundy oczekiwania na pierwsze okno po starcie")
    args = ap.parse_args()
    cfg = LiveConfig(args.config)
    work_dir = ROOT / cfg()["paths"]["work_dir"]
    work_dir.mkdir(parents=True, exist_ok=True)
    logger.add(work_dir / "server.log", rotation="5 MB", retention=3, encoding="utf-8", enqueue=True)
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    crash_log = open(work_dir / "server.crash.log", "a", encoding="utf-8")
    faulthandler.enable(crash_log)  # fatalne bledy procesu (np. SIGSEGV/SIGABRT w bibliotece natywnej) zostawiaja traceback
    liveness = Liveness(work_dir / "server.state.json")
    mem = read_mem()
    logger.info(f"LectureNoter: start serwera na {args.host}:{args.port} pid {os.getpid()}"
                + (" (auto-exit)" if args.auto_exit else "") + f", pamiec: serwer {mem['rss_mb']} MB, wolna w systemie {mem['mem_available_mb']} MB")
    app = create_app(cfg, auto_exit=args.auto_exit, idle_grace=args.idle_grace, startup_grace=args.startup_grace)
    app.state.previous_exit, app.state.liveness = liveness.previous, liveness
    server = LoggingServer(uvicorn.Config(app, host=args.host, port=args.port, log_level="info", log_config=None,
                                          access_log=False, timeout_graceful_shutdown=2))  # nie czekaj na otwarte strumienie SSE
    app.state.request_exit = lambda: setattr(server, "should_exit", True)
    if hasattr(signal, "SIGHUP"):  # zamkniecie terminala nie zabija serwera po cichu
        signal.signal(signal.SIGHUP, lambda s, f: server.handle_exit(s, f))
    liveness.begin()
    reason = None
    try:
        server.run()
        reason = app.state.exit_reason or "zakonczenie przez uvicorn"
    except BaseException as e:  # nieobsluzony wyjatek w serwerze: zapisz przyczyne zamiast znikac po cichu
        reason = f"wyjatek: {type(e).__name__}: {e}"
        logger.exception("serwer zakonczyl sie wyjatkiem")
        raise
    finally:
        liveness.close(reason or "nieznana")
        logger.info(f"LectureNoter: serwer zakonczyl prace ({reason})")


if __name__ == "__main__":
    main()
