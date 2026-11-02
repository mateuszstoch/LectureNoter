"""Archiwum wykladu .lec: zwykly ZIP z innym rozszerzeniem.

Uklad:
  manifest.json            opis (format, wersja, uid, tytul, folder, dane o filmie)
  data/<pliki wykladu>     zawartosc work/<id>/ (bez logow i plikow tymczasowych)
  video/<plik>             opcjonalnie film

Archiwum z zewnatrz jest NIEZAUFANE: import odrzuca sciezki spoza katalogu, dowiazania, zaszyfrowane wpisy,
bomby kompresji i nieprawidlowe pliki JSON, a rozpakowuje wylacznie znane pliki wykladu."""
import json
import re
import time
import zipfile
from pathlib import Path, PurePosixPath

FORMAT = "lecturenoter-lecture"
VERSION = 1
MAX_FILES = 200_000
MAX_RATIO = 400            # maks. stopien kompresji dla duzych plikow (bomba kompresji; legalne dane maja ratio rzedu 1-50)
BIG_FILE = 20 * 1024 * 1024
CHUNK = 1024 * 1024
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
TEXT_EXT = {".json", ".jsonl", ".md", ".txt"}

EXCLUDED_NAMES = {"pipeline.log", "job.out", "run_state.json", "progress.json", "checkpoints.json", "worker_error.json", "audio.wav"}
ALLOWED_FILES = {"meta.json", "transcript.json", "frames_metadata.json", "vision_context.json", "vision_context.jsonl",
                 "lecture_notes.md", "lecture_notes.edited.md", "notes_meta.json", "chat.json", "stage_state.json"}
ALLOWED_DIRS = {"frames": {".jpg"}, "chunks": {".md", ".json"}, "notes_history": {".md"}}
_SAFE_NAME = re.compile(r"^[\w.\- ()]+$")


class ArchiveError(ValueError):
    pass


def _excluded(path):
    n = path.name
    return n in EXCLUDED_NAMES or (n.startswith("worker_") and n.endswith(".log")) or n.endswith(".tmp")


def collect_files(wd):
    """Pliki wykladu do eksportu: [(sciezka, wzgledna sciezka posix, rozmiar)]; tylko znane, bez logow i dowiazan."""
    wd = Path(wd)
    out = []
    for p in sorted(wd.rglob("*")):
        if p.is_symlink() or not p.is_file() or _excluded(p):
            continue
        rel = p.relative_to(wd).as_posix()
        parts = PurePosixPath(rel).parts
        if (len(parts) == 1 and parts[0] in ALLOWED_FILES) or \
                (len(parts) == 2 and parts[0] in ALLOWED_DIRS and p.suffix.lower() in ALLOWED_DIRS[parts[0]]):
            out.append((p, rel, p.stat().st_size))
    return out


def _add_file(zf, src, arcname, progress_cb):
    src = Path(src)
    zi = zipfile.ZipInfo.from_file(src, arcname)
    zi.compress_type = zipfile.ZIP_DEFLATED if src.suffix.lower() in TEXT_EXT else zipfile.ZIP_STORED  # obrazy/filmy sa juz skompresowane
    with open(src, "rb") as fin, zf.open(zi, "w", force_zip64=True) as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            progress_cb(len(chunk))


def build_archive(wd, dest, manifest, video_path=None, progress=None):
    """Zapisuje archiwum .lec w `dest`. progress(zapisane_bajty, lacznie_bajtow). Zwraca pelny manifest."""
    files = collect_files(wd)
    video = Path(video_path) if video_path and Path(video_path).is_file() else None
    total = sum(s for _, _, s in files) + (video.stat().st_size if video else 0)
    manifest = dict(manifest, format=FORMAT, version=VERSION, exported_at=time.time(), files=len(files),
                    data_bytes=sum(s for _, _, s in files), has_video=bool(video),
                    video_name=video.name if video else None, video_bytes=video.stat().st_size if video else 0)
    done = 0

    def tick(n):
        nonlocal done
        done += n
        if progress:
            progress(done, total)
    with zipfile.ZipFile(dest, "w", allowZip64=True) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2), compress_type=zipfile.ZIP_DEFLATED)
        for p, rel, _ in files:
            _add_file(zf, p, f"data/{rel}", tick)
        if video:
            _add_file(zf, video, f"video/{video.name}", tick)
    return manifest


def _classify(zi):
    """('manifest'|'data'|'video'|'ignore', wzgledna sciezka) albo ArchiveError dla wpisu niebezpiecznego."""
    name = zi.filename
    if zi.is_dir():
        return "ignore", None
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name) or "\x00" in name \
            or ".." in PurePosixPath(name).parts:
        raise ArchiveError(f"Niebezpieczna sciezka w archiwum: {name!r}")
    if (zi.external_attr >> 16) & 0o170000 == 0o120000:
        raise ArchiveError(f"Archiwum zawiera dowiazanie symboliczne ({name}): odrzucono.")
    if zi.flag_bits & 0x1:
        raise ArchiveError("Archiwum jest zaszyfrowane: nieobslugiwane.")
    if zi.file_size > BIG_FILE and zi.compress_size and zi.file_size / zi.compress_size > MAX_RATIO:
        raise ArchiveError(f"Podejrzanie wysoki stopien kompresji ({name}): wyglada na bombe kompresji.")
    parts = PurePosixPath(name).parts
    if name == "manifest.json":
        return "manifest", name
    if parts[0] == "data":
        rel = parts[1:]
        if len(rel) == 1 and rel[0] in ALLOWED_FILES:
            return "data", "/".join(rel)
        if len(rel) == 2 and rel[0] in ALLOWED_DIRS and _SAFE_NAME.match(rel[1]) \
                and Path(rel[1]).suffix.lower() in ALLOWED_DIRS[rel[0]]:
            return "data", "/".join(rel)
        return "ignore", None            # nieznany plik: pomijamy (zgodnosc z nowszymi wersjami), nigdy nie rozpakowujemy
    if parts[0] == "video" and len(parts) == 2 and Path(parts[1]).suffix.lower() in VIDEO_EXT:
        return "video", parts[1]
    return "ignore", None


def _load_json(zf, name, limit=64 * 1024 * 1024):
    zi = zf.getinfo(name)
    if zi.file_size > limit:
        raise ArchiveError(f"Plik {name} jest zbyt duzy.")
    try:
        return json.loads(zf.read(zi).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ArchiveError(f"Plik {name} w archiwum jest uszkodzony (niepoprawny JSON).")


def _validate_data(zf, entries):
    names = {rel for kind, rel, _ in entries if kind == "data"}
    if "meta.json" not in names:
        raise ArchiveError("W archiwum brak data/meta.json: to nie jest archiwum wykladu LectureNoter.")
    meta = _load_json(zf, "data/meta.json")
    if not isinstance(meta, dict) or not str(meta.get("title") or "").strip():
        raise ArchiveError("Plik meta.json w archiwum jest niepoprawny (brak tytulu).")
    for name, kind in (("transcript.json", list), ("frames_metadata.json", list), ("vision_context.json", list),
                       ("stage_state.json", dict), ("notes_meta.json", dict)):
        if name in names:
            if not isinstance(_load_json(zf, f"data/{name}"), kind):
                raise ArchiveError(f"Plik {name} w archiwum ma niepoprawna strukture.")
    if "chat.json" in names:
        chat = _load_json(zf, "data/chat.json")
        if not isinstance(chat, dict) or not isinstance(chat.get("chats", []), list):
            raise ArchiveError("Plik chat.json w archiwum ma niepoprawna strukture.")
        for c in chat.get("chats", []):  # kazda rozmowa musi miec identyfikator i poprawna liste wiadomosci
            ok = isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"] and isinstance(c.get("messages", []), list) \
                and all(isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
                        for m in c.get("messages", []))
            if not ok:
                raise ArchiveError("Rozmowa w chat.json ma niepoprawna strukture.")
    return meta


def inspect_archive(path, max_bytes=None):
    """Sprawdza archiwum bez rozpakowywania. Zwraca {'manifest', 'meta', 'entries', 'uncompressed', 'ignored'}.
    entries: [(rodzaj, nazwa, ZipInfo)]. Rzuca ArchiveError, gdy archiwum jest niepoprawne lub niebezpieczne."""
    if not zipfile.is_zipfile(path):
        raise ArchiveError("To nie jest poprawne archiwum .lec (oczekiwano archiwum ZIP).")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILES:
                raise ArchiveError(f"Archiwum zawiera zbyt wiele plikow ({len(infos)}).")
            entries, ignored, total = [], 0, 0
            for zi in infos:
                kind, rel = _classify(zi)
                if kind == "ignore":
                    ignored += 0 if zi.is_dir() else 1
                    continue
                entries.append((kind, rel, zi))
                total += zi.file_size
            if not any(k == "manifest" for k, _, _ in entries):
                raise ArchiveError("W archiwum brak manifest.json: to nie jest archiwum wykladu LectureNoter.")
            manifest = _load_json(zf, "manifest.json")
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise ArchiveError("To archiwum nie jest w formacie LectureNoter.")
            if not isinstance(manifest.get("version"), int) or manifest["version"] > VERSION:
                raise ArchiveError(f"Archiwum pochodzi z nowszej wersji aplikacji (format {manifest.get('version')}). Zaktualizuj LectureNoter.")
            if sum(1 for k, _, _ in entries if k == "video") > 1:
                raise ArchiveError("Archiwum zawiera wiecej niz jeden plik wideo.")
            if max_bytes and total > max_bytes:
                raise ArchiveError(f"Archiwum po rozpakowaniu zajmuje {total / 1e9:.1f} GB, wiecej niz dozwolone {max_bytes / 1e9:.0f} GB.")
            meta = _validate_data(zf, entries)
            bad = zf.testzip()  # sprawdza sumy kontrolne CRC wszystkich wpisow
            if bad:
                raise ArchiveError(f"Archiwum jest uszkodzone (blad sumy kontrolnej w {bad}).")
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"Archiwum jest uszkodzone: {e}")
    return {"manifest": manifest, "meta": meta, "entries": entries, "uncompressed": total, "ignored": ignored}


def extract_archive(path, data_dir, video_dir=None, progress=None, info=None):
    """Rozpakowuje wylacznie znane pliki: dane do `data_dir`, film (jesli `video_dir`) do `video_dir`.
    Zwraca sciezke wypakowanego filmu albo None."""
    info = info or inspect_archive(path)
    data_dir = Path(data_dir).resolve()
    total = sum(zi.file_size for kind, _, zi in info["entries"] if kind in ("data", "video") and (kind == "data" or video_dir))
    done, video_out = 0, None
    with zipfile.ZipFile(path) as zf:
        for kind, rel, zi in info["entries"]:
            if kind == "data":
                dest = (data_dir / rel).resolve()
                if data_dir not in dest.parents:
                    raise ArchiveError(f"Niebezpieczna sciezka: {rel}")
            elif kind == "video" and video_dir:
                video_dir = Path(video_dir)
                dest = video_dir / rel
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zf.open(zi) as fin, open(dest, "wb") as fout:
                while True:
                    chunk = fin.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > zi.file_size:   # naglowek klamie co do rozmiaru
                        raise ArchiveError(f"Wpis {rel} jest wiekszy, niz deklaruje archiwum.")
                    fout.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if kind == "video":
                video_out = dest
    return video_out
