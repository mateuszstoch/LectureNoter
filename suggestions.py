"""Podpowiedzi z wykladu dla czatu i quizow: slowa kluczowe i pytania pomocnicze (suggestions.json).
Generuje je model po napisaniu notatek; dla starszych wykladow i na wypadek bledu istnieja podpowiedzi z naglowkow notatek."""
import json
import re
import time
from pathlib import Path

from loguru import logger

from io_utils import atomic_write_json, read_json

FILE = "suggestions.json"
SKIP_HEADS = {"spis tresci", "spis treści", "podsumowanie", "notatki z wykładu", "notatki", "wstęp", "(wstęp)"}
SYSTEM = ("Pomagasz studentowi korzystać z notatek z jednego wykładu. Odpowiadasz po polsku, wyłącznie poprawnym JSON-em, "
          "używając wyłącznie alfabetu łacińskiego.")
PROMPT = (
    "Na podstawie poniższych notatek z wykładu przygotuj podpowiedzi dla studenta:\n"
    '- "keywords": 8-12 najważniejszych pojęć z tego wykładu (każde to 1-3 słowa, w mianowniku, bez powtórzeń),\n'
    '- "questions": 6 konkretnych pytań, które student mógłby zadać asystentowi o TEN wykład (mają odwoływać się do jego treści i '
    'pojęć; nie pytaj ogólnikowo typu „podsumuj wykład”; różne pytania: wyjaśnienie, porównanie, przykład, zastosowanie).\n'
    'Format: {{"keywords": ["..."], "questions": ["...?"]}}\n\n# Notatki\n{notes}')


def _heads(md):
    out, seen = [], set()
    for m in re.finditer(r"(?m)^#{1,3}\s+(.+?)\s*$", md or ""):
        t = re.sub(r"^\d+[\.\)]\s*", "", m.group(1)).strip(" *_`")
        if t and t.lower() not in SKIP_HEADS and t.lower() not in seen and len(t) <= 80:
            seen.add(t.lower())
            out.append(t)
    return out


def heuristic(notes_md):
    """Podpowiedzi z samych naglowkow notatek (bez modelu)."""
    heads = _heads(notes_md)
    tpl = ["Wyjaśnij zagadnienie: {t}", "Co najważniejszego warto wiedzieć o: {t}?"]
    questions = [tpl[i % 2].format(t=t) for i, t in enumerate(heads[:6])]
    return {"keywords": heads[:12], "questions": questions or ["Podsumuj ten wykład w punktach"], "source": "headings"}


def clean(data):
    """Waliduje odpowiedz modelu; zwraca {'keywords', 'questions'} albo None, gdy za malo uzytecznych danych."""
    if not isinstance(data, dict):
        return None

    def strings(v, limit, maxlen):
        out, seen = [], set()
        for x in v if isinstance(v, list) else []:
            if isinstance(x, str):
                t = x.strip().strip("-•* ").strip()
                if 2 <= len(t) <= maxlen and t.lower() not in seen:
                    seen.add(t.lower())
                    out.append(t)
        return out[:limit]
    kw = strings(data.get("keywords"), 12, 50)
    qs = [q if q.endswith("?") else q + "?" for q in strings(data.get("questions"), 8, 200)]
    if len(kw) < 3 or len(qs) < 2:
        return None
    return {"keywords": kw, "questions": qs}


def generate(client, model, notes_md, num_ctx, keep_alive="10m"):
    """Prosi model o slowa kluczowe i pytania. Zwraca slownik z kluczami keywords/questions albo None."""
    if not (notes_md or "").strip():
        return None
    budget_chars = max(3000, (num_ctx - 1500) * 3)
    text = notes_md if len(notes_md) <= budget_chars else notes_md[:budget_chars]
    raw = client.generate(model, PROMPT.format(notes=text), system=SYSTEM,
                          options={"num_ctx": num_ctx, "num_predict": 700, "temperature": 0.3}, keep_alive=keep_alive, format="json")
    try:
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()))
    except ValueError:
        i, j = (raw or "").find("{"), (raw or "").rfind("}")
        try:
            data = json.loads(raw[i:j + 1])
        except ValueError:
            return None
    return clean(data)


def save(workdir, data, model):
    atomic_write_json(str(Path(workdir) / FILE), {**data, "source": "model", "model": model, "ts": time.time()})


def load(workdir, notes_md=""):
    """Zapisane podpowiedzi modelu albo, gdy ich nie ma, podpowiedzi z naglowkow."""
    d = read_json(Path(workdir) / FILE, None)
    if isinstance(d, dict) and clean(d):
        return {**clean(d), "source": "model", "model": d.get("model")}
    return heuristic(notes_md)


def try_generate_and_save(client, model, workdir, notes_md, num_ctx):
    """Dla potoku notatek: nieudane podpowiedzi nie moga psuc etapu."""
    try:
        data = generate(client, model, notes_md, num_ctx)
        if data:
            save(workdir, data, model)
            logger.info(f"podpowiedzi: {len(data['keywords'])} slow kluczowych, {len(data['questions'])} pytan")
        else:
            logger.warning("podpowiedzi: model nie zwrocil uzytecznych danych (zostana podpowiedzi z naglowkow)")
    except Exception as e:
        logger.warning(f"podpowiedzi: pominieto ({e})")
