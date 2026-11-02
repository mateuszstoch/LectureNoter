"""Notatki: wersja wygenerowana (lecture_notes.md) i edytowana (lecture_notes.edited.md) z historią."""
import re
import shutil
import time
from pathlib import Path

GENERATED = "lecture_notes.md"
EDITED = "lecture_notes.edited.md"
HISTORY = "notes_history"
MAX_VERSIONS = 20
MAX_BYTES = 2_000_000
VERSION_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


class NotesError(ValueError):
    pass


class NotesMissing(NotesError):
    pass


def _write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def get_active(wd):
    """Zwraca aktywna wersje notatek: edytowana, jesli istnieje, w przeciwnym razie wygenerowana."""
    wd = Path(wd)
    edited, gen = wd / EDITED, wd / GENERATED
    if not edited.exists() and not gen.exists():
        raise NotesMissing("Notatki nie zostaly jeszcze wygenerowane.")
    path = edited if edited.exists() else gen
    return {"md": path.read_text(encoding="utf-8"), "source": "edited" if path == edited else "generated",
            "has_edited": edited.exists(), "has_generated": gen.exists(), "updated": path.stat().st_mtime}


def _archive(wd, src):
    hist = Path(wd) / HISTORY
    hist.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name, n = stamp, 1
    while (hist / f"{name}.md").exists():
        name, n = f"{stamp}-{n}", n + 1
    shutil.copyfile(src, hist / f"{name}.md")
    for old in sorted(hist.glob("*.md"))[:-MAX_VERSIONS]:
        old.unlink()


def save_edited(wd, md):
    wd = Path(wd)
    if not isinstance(md, str) or not md.strip():
        raise NotesError("Notatki nie moga byc puste.")
    if len(md.encode("utf-8")) > MAX_BYTES:
        raise NotesError("Notatki sa zbyt duze (limit 2 MB).")
    edited = wd / EDITED
    if edited.exists():
        _archive(wd, edited)
    _write(edited, md)
    return get_active(wd)


def revert_edited(wd):
    wd = Path(wd)
    edited = wd / EDITED
    if not edited.exists():
        raise NotesError("Brak edytowanej wersji.")
    if not (wd / GENERATED).exists():
        raise NotesError("Brak wygenerowanej wersji, do ktorej mozna wrocic.")
    _archive(wd, edited)
    edited.unlink()
    return get_active(wd)


def list_versions(wd):
    hist = Path(wd) / HISTORY
    if not hist.exists():
        return []
    return [{"id": p.stem, "saved_at": p.stat().st_mtime, "size": p.stat().st_size}
            for p in sorted(hist.glob("*.md"), reverse=True)]


def read_version(wd, version):
    if not VERSION_RE.match(version or ""):
        raise NotesError("niepoprawny identyfikator wersji")
    p = Path(wd) / HISTORY / f"{version}.md"
    if not p.exists():
        raise NotesError("nie ma takiej wersji")
    return p.read_text(encoding="utf-8")
