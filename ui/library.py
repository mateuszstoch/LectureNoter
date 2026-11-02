"""Biblioteka filmow: skan katalogu videos/, dodawanie i podsumowania stanu z work/*."""
import re
import unicodedata
from pathlib import Path, PurePosixPath

from io_utils import read_json
from main import STAGES, PipelineOrchestrator

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "film"


class LibraryError(ValueError):
    pass


class Library:
    def __init__(self, videos_dir, work_dir, cfg):
        self.videos_dir, self.work_dir, self.cfg = Path(videos_dir), Path(work_dir), cfg
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # --- identyfikatory i sciezki ---
    def workdir(self, video_id):
        if not ID_RE.match(video_id or ""):
            raise LibraryError("niepoprawny identyfikator filmu")
        wd = (self.work_dir / video_id).resolve()
        if wd.parent != self.work_dir.resolve() or not (wd / "meta.json").exists():
            raise LibraryError("nie ma takiego filmu")
        return wd

    def _index(self):
        """{resolved video path -> id} z work/*/meta.json."""
        idx = {}
        for d in self.work_dir.iterdir():
            meta = read_json(d / "meta.json") if d.is_dir() else None
            if meta and ID_RE.match(d.name):
                idx[meta["video_path"]] = d.name
        return idx

    # --- pliki z katalogu videos/ ---
    def list_files(self, max_depth=2):
        idx = self._index()
        base = self.videos_dir.resolve()
        out = []
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXT or p.name.startswith("."):
                continue
            rel = p.relative_to(base)
            if len(rel.parts) > max_depth + 1:
                continue
            st = p.stat()
            out.append({"file": rel.as_posix(), "size": st.st_size, "mtime": st.st_mtime,
                        "video_id": idx.get(str(p.resolve()))})
        return out

    def _resolve_file(self, rel):
        """Plik (lub dowiazanie symboliczne) musi lezec w videos/; cel dowiazania moze byc gdziekolwiek."""
        rp = PurePosixPath(str(rel))
        if rp.is_absolute() or ".." in rp.parts or not rp.parts:
            raise LibraryError("plik musi lezec w katalogu videos/ i byc plikiem wideo")
        p = self.videos_dir.resolve() / rp
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
            raise LibraryError("plik musi lezec w katalogu videos/ i byc plikiem wideo")
        return p.resolve()

    # --- dodawanie ---
    def add_video(self, rel, profile="auto", language=None):
        p = self._resolve_file(rel)
        idx = self._index()
        if str(p) in idx:
            return idx[str(p)]
        try:
            import frames_module
            frames_module.probe(p)
        except Exception as e:
            raise LibraryError(f"ffprobe nie rozpoznal pliku jako wideo: {e}")
        base = slugify(p.stem)
        vid, n = base, 2
        while (self.work_dir / vid).exists():
            vid, n = f"{base}-{n}", n + 1
        wd = self.work_dir / vid
        PipelineOrchestrator(p, wd, self.cfg, language or self.cfg["language"], profile).ensure_meta()
        return vid

    # --- stan ---
    def orchestrator(self, video_id):
        return PipelineOrchestrator.from_workdir(self.workdir(video_id), self.cfg)

    def summary(self, video_id):
        wd = self.workdir(video_id)
        meta = read_json(wd / "meta.json", {})
        st = self.orchestrator(video_id).status()
        return {"id": video_id, "title": meta.get("title"), "created": meta.get("created"),
                "duration": meta.get("duration"), "has_audio": meta.get("has_audio"),
                "video_path": meta.get("video_path"), "profile": meta.get("profile"),
                "overall": st["overall"], "video_exists": st["video_exists"],
                "stages": st["stages"], "error": st["run"].get("error")}

    def list_videos(self):
        out = []
        for d in self.work_dir.iterdir():
            if d.is_dir() and ID_RE.match(d.name) and (d / "meta.json").exists():
                out.append(self.summary(d.name))
        return sorted(out, key=lambda v: v["created"] or 0, reverse=True)

    def impact(self, video_id, stage):
        """Etapy, ktorych wyniki zostana usuniete przy uruchomieniu od `stage` (z --force)."""
        st = self.orchestrator(video_id).status()["stages"]
        return [s for s in STAGES[STAGES.index(stage):] if st[s]["status"] != "pending"]
