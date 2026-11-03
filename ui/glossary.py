"""Slownik pojec wykladu: z szkieletu (outline.md), z pliku glossary.json albo generowany modelem na zadanie."""
import json
import re
import time
from pathlib import Path

from io_utils import atomic_write_json, read_json

FILE = "glossary.json"
SYSTEM = ("Pomagasz studentowi uczyć się z notatek z wykładu. Odpowiadasz po polsku, wyłącznie poprawnym JSON-em, "
          "używając wyłącznie alfabetu łacińskiego.")
PROMPT = ('Na podstawie notatek z wykładu przygotuj słownik 10-25 najważniejszych pojęć, symboli i skrótów. Każde pojęcie ma krótką '
          'definicję (1-2 zdania) zgodną z treścią wykładu. Wzory i symbole matematyczne zapisuj w LaTeX w znakach dolara ($...$), np. $x^2$, $\\mathbb{{R}}$. Format: {{"terms": [{{"term": "...", "definition": "..."}}]}}\n\n# Notatki\n{notes}')
LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*\**\s*([^:–—\n]{2,60}?)\s*\**\s*[:–—-]+\s*\**\s*(.{6,})$")


def parse_outline(md):
    """Pojecia z czesci 'Slownik' szkieletu wykladu (linie 'pojecie: definicja')."""
    if not md:
        return []
    m = re.search(r"(?is)\*{0,2}s[łl]ownik\*{0,2}\s*:?\**\s*(.*)$", md)
    if not m:
        return []
    out, seen = [], set()
    for line in m.group(1).splitlines():
        mm = LINE.match(line)
        if mm:
            term, definition = mm.group(1).strip(" *_`"), mm.group(2).strip(" *_`")
            if term and term.lower() not in seen:
                seen.add(term.lower())
                out.append({"term": term, "definition": definition})
    return out


def clean(data):
    items = data.get("terms") if isinstance(data, dict) else data
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and isinstance(it.get("term"), str) and isinstance(it.get("definition"), str):
            t, d = it["term"].strip(), it["definition"].strip()
            if 2 <= len(t) <= 80 and len(d) >= 6 and t.lower() not in seen:
                seen.add(t.lower())
                out.append({"term": t, "definition": d[:600]})
    return out[:40]


def load(wd):
    wd = Path(wd)
    d = read_json(wd / FILE, None)
    if isinstance(d, dict) and clean(d):
        return {"terms": clean(d), "source": "model", "model": d.get("model")}
    op = wd / "outline.md"
    terms = parse_outline(op.read_text(encoding="utf-8")) if op.exists() else []
    return {"terms": terms, "source": "outline" if terms else "none"}


def generate(client, model, notes_md, num_ctx):
    if not (notes_md or "").strip():
        return []
    budget = max(3000, (num_ctx - 1800) * 3)
    raw = client.generate(model, PROMPT.format(notes=notes_md[:budget]), system=SYSTEM,
                          options={"num_ctx": num_ctx, "num_predict": 1500, "temperature": 0.2}, keep_alive="10m", format="json")
    try:
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()))
    except ValueError:
        i, j = (raw or "").find("{"), (raw or "").rfind("}")
        try:
            data = json.loads(raw[i:j + 1])
        except ValueError:
            return []
    return clean(data)


def save(wd, terms, model):
    atomic_write_json(str(Path(wd) / FILE), {"terms": terms, "model": model, "ts": time.time()})
