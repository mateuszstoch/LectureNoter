"""Biblioteka filmow: skan katalogu videos/, dodawanie i podsumowania stanu z work/*."""
import os
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath

from config_loader import resolve
from io_utils import atomic_write_json, read_json
from main import STAGES, PipelineOrchestrator

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v",
             ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}  # takze samo audio (bez tablicy)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


_PL = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss"})  # litery bez rozkladu w Unicode


def slugify(name):
    s = unicodedata.normalize("NFKD", name.translate(_PL)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "film"


class LibraryError(ValueError):
    pass


class Library:
    def __init__(self, videos_dir, work_dir, cfg):
        self.videos_dir, self.work_dir, self._cfg_src = Path(videos_dir), Path(work_dir), cfg
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @property
    def cfg(self):
        return resolve(self._cfg_src)  # aktualna konfiguracja (LiveConfig przeladowuje plik po zmianie)

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
            if meta and ID_RE.match(d.name) and meta.get("video_path"):
                idx[meta["video_path"]] = d.name
        return idx

    # --- pliki z katalogu videos/ ---
    def _iter_files(self, max_depth=2):
        """Wpisy (pliki lub dowiazania) wideo w videos/, bez rozwiazywania dowiazan."""
        base = self.videos_dir.resolve()
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXT or p.name.startswith("."):
                continue
            if len(p.relative_to(base).parts) > max_depth + 1:
                continue
            yield p

    def list_files(self, max_depth=2):
        idx = self._index()
        base = self.videos_dir.resolve()
        out = []
        for p in self._iter_files(max_depth):
            st = p.stat()
            out.append({"file": p.relative_to(base).as_posix(), "size": st.st_size, "mtime": st.st_mtime,
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

    # --- uid, wskazanie pliku wideo ---
    def ensure_uid(self, video_id):
        """Staly identyfikator wykladu (przy imporcie pozwala wykryc ten sam wyklad); nadawany leniwie starym wykladom."""
        import uuid
        wd = self.workdir(video_id)
        meta = read_json(wd / "meta.json", {})
        if not meta.get("uid"):
            meta["uid"] = uuid.uuid4().hex
            atomic_write_json(str(wd / "meta.json"), meta)
        return meta["uid"]

    def find_by_uid(self, uid):
        for vid, meta in self.iter_meta():
            if uid and meta.get("uid") == uid:
                return vid, meta.get("title") or vid
        return None

    def unique_video_path(self, name):
        """Sciezka w videos/ bez nadpisywania istniejacego pliku."""
        base = self.videos_dir.resolve()
        stem, suf = Path(name).stem, Path(name).suffix
        p, n = base / name, 2
        while p.exists() or p.is_symlink():
            p, n = base / f"{stem} ({n}){suf}", n + 1
        return p

    def relink(self, video_id, rel):
        """Wskazuje plik wideo z katalogu videos/ dla wykladu (np. zaimportowanego bez filmu albo przeniesionego)."""
        from main import video_sig
        p = self._resolve_file(rel)
        other = self._index().get(str(p))
        if other and other != video_id:
            raise LibraryError(f"Ten plik jest już przypisany do innego wykładu ({other}).")
        try:
            import frames_module
            probe = frames_module.probe(p)
        except Exception as e:
            raise LibraryError(f"ffprobe nie rozpoznał pliku jako wideo: {e}")
        wd = self.workdir(video_id)
        meta = read_json(wd / "meta.json", {})
        meta.update(video_path=str(p), video_sig=video_sig(p), duration=probe["duration"], has_audio=probe["has_audio"],
                    width=probe["width"], height=probe["height"])
        atomic_write_json(str(wd / "meta.json"), meta)
        self.orchestrator(video_id).rebase_state(stages=("asr", "frames"))  # przyjmij wyniki jako dotyczace tego filmu
        return self.summary(video_id)

    # --- nazwa i folder ---
    def iter_meta(self):
        """(id, meta) wszystkich wykladow: lekko, bez liczenia statusow etapow."""
        for d in sorted(self.work_dir.iterdir()):
            if d.is_dir() and ID_RE.match(d.name):
                meta = read_json(d / "meta.json")
                if meta:
                    yield d.name, meta

    def rename(self, video_id, title):
        title = re.sub(r"\s+", " ", (title or "")).strip()
        if not title:
            raise LibraryError("Nazwa wykładu nie może być pusta.")
        if len(title) > 120:
            raise LibraryError("Nazwa wykładu jest za długa (maks. 120 znaków).")
        wd = self.workdir(video_id)
        meta = read_json(wd / "meta.json", {})
        meta["title"] = title
        atomic_write_json(str(wd / "meta.json"), meta)  # tylko wyswietlany tytul: id i katalog zostaja bez zmian
        return title

    def set_folder(self, video_id, folder_id):
        wd = self.workdir(video_id)
        meta = read_json(wd / "meta.json", {})
        if folder_id:
            meta["folder"] = folder_id
        else:
            meta.pop("folder", None)
        atomic_write_json(str(wd / "meta.json"), meta)

    def reassign_folder(self, old_folder, new_folder):
        """Przenosi wszystkie wyklady z folderu `old_folder` do `new_folder` (None = korzen)."""
        n = 0
        for vid, meta in self.iter_meta():
            if meta.get("folder") == old_folder:
                self.set_folder(vid, new_folder)
                n += 1
        return n

    def summary(self, video_id):
        wd = self.workdir(video_id)
        meta = read_json(wd / "meta.json", {})
        st = self.orchestrator(video_id).status()
        return {"id": video_id, "title": meta.get("title"), "created": meta.get("created"),
                "duration": meta.get("duration"), "has_audio": meta.get("has_audio"), "has_video": meta.get("has_video"),
                "transcript_source": meta.get("transcript_source"),
                "video_path": meta.get("video_path"), "profile": meta.get("profile"), "folder": meta.get("folder"),
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

    # --- usuwanie ---
    def library_entry(self, video_id):
        """Wpis w videos/ (plik lub dowiazanie), ktory wskazuje na film tego wykladu, albo None."""
        target = (read_json(self.workdir(video_id) / "meta.json", {}) or {}).get("video_path")
        for p in self._iter_files():
            if str(p.resolve()) == target:
                return p
        return None

    def disk_usage(self, video_id):
        total = 0
        for root, _, files in os.walk(self.workdir(video_id)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def delete_preview(self, video_id):
        wd = self.workdir(video_id)
        entry = self.library_entry(video_id)
        chats = read_json(wd / "chat.json", {"chats": []}) or {"chats": []}
        return {"size_bytes": self.disk_usage(video_id),
                "has_edited_notes": (wd / "lecture_notes.edited.md").exists(),
                "chat_count": len(chats.get("chats", [])),
                "quiz_count": len((read_json(wd / "quizzes.json", {"quizzes": []}) or {}).get("quizzes", [])),
                "video_entry": entry.relative_to(self.videos_dir.resolve()).as_posix() if entry else None,
                "video_is_link": bool(entry and entry.is_symlink())}

    def delete(self, video_id, delete_video=False):
        """Trwale usuwa dane wykladu (work/<id>). Opcjonalnie wpis w videos/: dla dowiazania symbolicznego tylko
        dowiazanie (cel zostaje). Zwraca {'deleted': id, 'video_deleted': bool}."""
        wd = self.workdir(video_id)
        entry = self.library_entry(video_id) if delete_video else None
        shutil.rmtree(wd)
        if entry is not None:
            entry.unlink()  # unlink na dowiazaniu usuwa dowiazanie, nie plik docelowy
        return {"deleted": video_id, "video_deleted": entry is not None}
