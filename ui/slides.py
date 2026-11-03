"""Slajdy (PDF) jako material pomocniczy wykladu: tekst stron w slides.json, uzywany w czacie i przy tworzeniu szkieletu notatek."""
import io
import time
from pathlib import Path

from io_utils import atomic_write_json, read_json

FILE = "slides.json"
MAX_BYTES = 80 * 1024 * 1024
MAX_PAGES = 600


class SlidesError(ValueError):
    pass


def extract(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SlidesError("Brak biblioteki pypdf: zainstaluj zależności (pip install pypdf) i zrestartuj serwer.")
    if len(data) > MAX_BYTES:
        raise SlidesError("Plik PDF jest za duży.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise SlidesError("PDF jest zaszyfrowany.")
        pages = []
        for i, p in enumerate(reader.pages[:MAX_PAGES], 1):
            text = " ".join((p.extract_text() or "").split())
            if text:
                pages.append({"n": i, "text": text[:6000]})
        total = len(reader.pages)
    except SlidesError:
        raise
    except Exception as e:
        raise SlidesError(f"Nie udało się odczytać PDF: {e}")
    if not pages:
        raise SlidesError("PDF nie zawiera tekstu (skan lub obrazy): wymagałby OCR, którego nie ma w tej wersji.")
    return {"pages": pages, "total_pages": total}


def save(wd, name, info):
    atomic_write_json(str(Path(wd) / FILE), {"name": name, "pages": info["pages"], "total_pages": info["total_pages"], "ts": time.time()})


def load(wd):
    d = read_json(Path(wd) / FILE, None)
    return d if isinstance(d, dict) and isinstance(d.get("pages"), list) else None


def delete(wd):
    (Path(wd) / FILE).unlink(missing_ok=True)


def summary(wd):
    d = load(wd)
    return {"name": d.get("name"), "pages": len(d["pages"]), "total_pages": d.get("total_pages")} if d else None
