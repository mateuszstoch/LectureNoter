"""Eksport i import wykladow (.lec): budowanie archiwum w tle z postepem, import z kontrola konfliktow i folderow."""
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from config_loader import resolve
from io_utils import atomic_write_json, read_json
from ui import archive as ar
from ui.folders import FolderError
from ui.library import slugify

TTL = 3600   # po tylu sekundach nieodebrane archiwa i niezatwierdzone importy sa sprzatane


class TransferError(ValueError):
    pass


class ConflictError(TransferError):
    def __init__(self, existing_id, title):
        super().__init__(f"Ten wykład już istnieje: „{title}”.")
        self.existing_id, self.title = existing_id, title


def free_bytes(path):
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def _sweep(directory, ttl=TTL):
    now = time.time()
    for p in Path(directory).glob("*"):
        try:
            if now - p.stat().st_mtime > ttl:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- eksport
class ExportManager:
    def __init__(self, work_dir):
        self.dir = Path(work_dir) / ".exports"
        self.dir.mkdir(parents=True, exist_ok=True)
        _sweep(self.dir)
        self._jobs, self._lock = {}, threading.Lock()

    def info(self, lib, video_id):
        """Rozmiar danych i filmu przed eksportem (do okna wyboru)."""
        wd = lib.workdir(video_id)
        files = ar.collect_files(wd)
        video = (read_json(wd / "meta.json", {}) or {}).get("video_path")
        vpath = Path(video) if video and Path(video).is_file() else None
        return {"data_bytes": sum(s for _, _, s in files), "files": len(files), "has_video": bool(vpath),
                "video_bytes": vpath.stat().st_size if vpath else 0, "video_name": vpath.name if vpath else None}

    def start(self, lib, folders, video_id, include_video):
        _sweep(self.dir)
        wd = lib.workdir(video_id)
        uid = lib.ensure_uid(video_id)
        meta = read_json(wd / "meta.json", {})
        video = Path(meta["video_path"]) if include_video and meta.get("video_path") else None
        if include_video and not (video and video.is_file()):
            raise TransferError("Plik wideo tego wykładu nie jest dostępny, więc nie można go dołączyć do archiwum.")
        info = self.info(lib, video_id)
        need = info["data_bytes"] + (info["video_bytes"] if include_video else 0)
        if free_bytes(self.dir) < need * 1.05 + 50 * 1024 * 1024:
            raise TransferError(f"Za mało miejsca na dysku na archiwum (potrzeba ok. {need / 1e9:.1f} GB).")
        path_names = next((f["path"] for f in folders.list() if f["id"] == meta.get("folder")), [])
        manifest = {"uid": uid, "title": meta.get("title") or video_id, "created": meta.get("created"), "language": meta.get("language"),
                    "profile": meta.get("profile"), "duration": meta.get("duration"), "folder_path": path_names, "app": "LectureNoter"}
        token = uuid.uuid4().hex
        filename = f"{slugify(manifest['title'])}.lec"
        job = {"state": "building", "done": 0, "total": max(1, need), "filename": filename, "error": None, "created": time.time(),
               "path": None, "include_video": bool(include_video)}
        with self._lock:
            self._jobs[token] = job
        threading.Thread(target=self._run, args=(token, wd, manifest, video), daemon=True).start()
        return token

    def _run(self, token, wd, manifest, video):
        job = self._jobs[token]
        part, final = self.dir / f"{token}.lec.part", self.dir / f"{token}.lec"
        try:
            def progress(done, total):
                job["done"], job["total"] = done, max(1, total)
            ar.build_archive(wd, part, manifest, video_path=video, progress=progress)
            part.replace(final)
            job.update(state="ready", path=str(final), done=job["total"], size=final.stat().st_size)
        except Exception as e:  # nic nie wolno polknac po cichu: blad trafia do statusu i widzi go uzytkownik
            job.update(state="error", error=f"{type(e).__name__}: {e}")
            part.unlink(missing_ok=True)

    def status(self, token):
        job = self._jobs.get(token)
        if not job:
            raise TransferError("Nie ma takiego eksportu (wygasl?).")
        return {k: v for k, v in job.items() if k != "path"}

    def file(self, token):
        job = self._jobs.get(token)
        if not job or job["state"] != "ready" or not Path(job["path"]).exists():
            raise TransferError("Archiwum nie jest gotowe albo wygasło.")
        return Path(job["path"]), job["filename"]

    def discard(self, token):
        job = self._jobs.pop(token, None)
        if job and job.get("path"):
            Path(job["path"]).unlink(missing_ok=True)


# ---------------------------------------------------------------- import
def staging_dir(work_dir):
    d = Path(work_dir) / ".imports"
    d.mkdir(parents=True, exist_ok=True)
    _sweep(d)
    return d


def summarize(info, existing=None):
    m, meta = info["manifest"], info["meta"]
    return {"title": meta.get("title") or m.get("title"), "uid": m.get("uid"), "exported_at": m.get("exported_at"),
            "folder_path": m.get("folder_path") or [], "has_video": bool(m.get("has_video")), "video_name": m.get("video_name"),
            "video_bytes": m.get("video_bytes") or 0, "data_bytes": m.get("data_bytes") or 0, "files": m.get("files"),
            "duration": m.get("duration"), "uncompressed": info["uncompressed"], "ignored": info["ignored"],
            "conflict": {"id": existing[0], "title": existing[1]} if existing else None}


def resolve_folder(folders, mode, folder, path_names):
    """Folder docelowy: 'current' = wskazany identyfikator, 'recreate' = odtworz sciezke z archiwum (istniejace foldery sa uzywane)."""
    if mode == "recreate" and path_names:
        parent = None
        for name in path_names:
            found = next((f["id"] for f in folders.list() if (f["parent"] or None) == parent and f["name"].casefold() == str(name).casefold()), None)
            if found is None:
                try:
                    found = folders.create(str(name), parent)
                except FolderError:
                    break  # np. za gleboko: reszta trafia do ostatniego poprawnego folderu
            parent = found
        return parent
    return folder if folder and folders.exists(folder) else None


def import_lecture(lib, folders, archive_path, *, work_dir, cfg, info=None, folder=None, folder_mode="current",
                   on_conflict=None, include_video=True, max_bytes=None):
    """Importuje wyklad z archiwum. on_conflict (gdy taki sam uid juz istnieje): 'copy' | 'replace' | 'skip'; brak = ConflictError."""
    from main import PipelineOrchestrator, video_sig
    info = info or ar.inspect_archive(archive_path, max_bytes)
    manifest, meta = info["manifest"], dict(info["meta"])
    existing = lib.find_by_uid(manifest.get("uid"))
    if existing and on_conflict == "skip":
        return {"id": existing[0], "title": existing[1], "skipped": True}
    if existing and on_conflict not in ("copy", "replace"):
        raise ConflictError(*existing)
    want_video = include_video and bool(manifest.get("has_video")) and any(k == "video" for k, _, _ in info["entries"])
    needed = info["uncompressed"] if want_video else sum(zi.file_size for k, _, zi in info["entries"] if k == "data")
    if free_bytes(work_dir) < needed * 1.1 + 50 * 1024 * 1024:
        raise TransferError(f"Za mało miejsca na dysku (potrzeba ok. {needed / 1e9:.1f} GB).")

    tmp = staging_dir(work_dir) / f"{uuid.uuid4().hex}-x"
    try:
        data = tmp / "data"
        video_tmp = ar.extract_archive(archive_path, data, tmp / "video" if want_video else None, info=info)
        replacing = bool(existing and on_conflict == "replace")
        old_meta = read_json(lib.work_dir / existing[0] / "meta.json", {}) if replacing else {}
        title = meta.get("title") or manifest.get("title") or "Wykład"
        if existing and on_conflict == "copy":
            title += " (kopia)"
            meta["uid"] = uuid.uuid4().hex   # kopia jest nowym wykladem
        meta.setdefault("uid", manifest.get("uid") or uuid.uuid4().hex)
        meta["title"] = title
        if video_tmp:
            dest = lib.unique_video_path(video_tmp.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(video_tmp), str(dest))
            meta["video_path"], meta["video_sig"] = str(dest.resolve()), video_sig(dest)
        else:
            meta["video_path"] = (old_meta.get("video_path") or "") if replacing else ""
            meta["video_sig"] = old_meta.get("video_sig") if replacing else None
        if replacing:
            new_id, meta["folder"] = existing[0], old_meta.get("folder")
        else:
            base, new_id, n = slugify(title), None, 2
            new_id = base
            while (lib.work_dir / new_id).exists():
                new_id, n = f"{base}-{n}", n + 1
            target = resolve_folder(folders, folder_mode, folder, manifest.get("folder_path") or [])
            meta["folder"] = target
        if not meta.get("folder"):
            meta.pop("folder", None)
        atomic_write_json(str(data / "meta.json"), meta)
        final = lib.work_dir / new_id
        old_dir = tmp / "old"
        if replacing:
            shutil.move(str(final), str(old_dir))  # poprzednia wersja czeka, az nowa sie przeniesie (bez utraty danych przy bledzie)
        try:
            os.replace(data, final)
        except Exception:
            if replacing:
                shutil.move(str(old_dir), str(final))
            raise
        PipelineOrchestrator.from_workdir(final, resolve(cfg)).rebase_state()  # wyniki przenosimy w stanie, w jakim sa
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"id": new_id, "title": title, "folder": meta.get("folder"), "has_video": bool(meta.get("video_path")),
            "replaced": replacing}
