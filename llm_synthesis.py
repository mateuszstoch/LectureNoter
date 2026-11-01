"""Etap 4: fuzja transkryptu i opisow tablicy + synteza notatek (map-reduce) przez Ollame."""
import difflib
import json
import re
from pathlib import Path

from loguru import logger

from io_utils import atomic_write_json, fmt_ts
from ollama_client import OllamaClient

SYSTEM = (
    "Jesteś asystentem akademickim. Otrzymujesz fragment chronologicznej osi wykładu: transkrypcję mowy "
    "([Mowa]) i odczytany tekst z tablicy/slajdów ([Tablica]). Twoim zadaniem jest stworzyć z niego "
    "kompleksową, zorganizowaną notatkę w formacie Markdown po polsku. Używaj nagłówków (##, ###) i list "
    "punktowych. Wzory matematyczne zapisuj w LaTeX w blokach $$ ... $$. Ignoruj błędy rozpoznawania mowy "
    "i wyciągaj sens; przy nazwach własnych, symbolach i wzorach ufaj bardziej tablicy niż mowie. "
    "Opisy tablicy bywają niedokładne (pismo odręczne): nie zgaduj, oznacz wątpliwe miejsca jako [?]. "
    "Nie dodawaj faktów spoza materiału. Dodawaj znaczniki czasu [HH:MM:SS] przy kluczowych tematach. "
    "Tablica opisuje stan końcowy planszy, więc jej treść mogła być zapisywana wcześniej niż jest pokazana."
)
SUMMARY_SYSTEM = ("Na podstawie listy nagłówków notatek z wykładu napisz po polsku krótkie (5-8 zdań) "
                  "podsumowanie tematyki wykładu. Nie dodawaj informacji spoza nagłówków.")


def est_tokens(text):
    return len(text) // 3 + 1  # polski w tokenizerze Qwena: ok. 3 znaki/token (szacunek zachowawczy)


def merge_speech(segments, window_s=45):
    """Laczy krotkie segmenty mowy w akapity ok. window_s sekund."""
    items, cur = [], None
    for s in segments:
        text = s["text"].strip()
        if not text:
            continue
        if cur is None or s["start"] - cur["t"] > window_s:
            cur = {"t": s["start"], "kind": "Mowa", "text": text}
            items.append(cur)
        else:
            cur["text"] += " " + text
    return items


def dedupe_board(frames, ratio=0.85):
    """Zostawia najpelniejszy stan: kolejny opis niemal identyczny z poprzednim zastepuje go."""
    out = []
    for f in frames:
        text = (f.get("text") or "").strip()
        if not text:
            continue
        if out and difflib.SequenceMatcher(None, out[-1]["text"], text).ratio() >= ratio:
            if len(text) >= len(out[-1]["text"]):
                out[-1]["text"] = text
            continue
        out.append({"t": f["timestamp"], "kind": "Tablica", "text": text})
    return out


def build_timeline(transcript, vision):
    items = merge_speech(transcript) + dedupe_board(vision)
    items.sort(key=lambda i: (i["t"], i["kind"] != "Mowa"))
    return items


def render(item):
    return f"[{fmt_ts(item['t'])}] [{item['kind']}]: {item['text']}"


def chunk_timeline(items, token_budget, max_minutes):
    chunks, cur, tok, start = [], [], 0, None
    for it in items:
        t = est_tokens(render(it))
        start = it["t"] if start is None else start
        too_long = cur and (tok + t > token_budget or it["t"] - start > max_minutes * 60)
        if too_long:
            chunks.append(cur)
            cur, tok, start = [], 0, it["t"]
        cur.append(it)
        tok += t
    if cur:
        chunks.append(cur)
    return chunks


def shift_headings(md, min_level=2):
    """Gwarantuje, ze notatka z chunku nie zawiera H1 (tytul dodaje skladanie)."""
    return re.sub(r"^#(?!#)", "##", md, flags=re.M) if min_level == 2 else md


def run_synthesis(workdir, cfg, model, num_ctx, client=None):
    workdir = Path(workdir)
    client = client or OllamaClient()
    tpath = workdir / "transcript.json"
    transcript = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else []
    vision = json.loads((workdir / "vision_context.json").read_text(encoding="utf-8")) \
        if (workdir / "vision_context.json").exists() else []
    if not transcript:
        logger.warning("brak transkryptu: tryb 'tylko tablica'")
    items = build_timeline(transcript, vision)
    if not items:
        raise RuntimeError("brak danych wejsciowych (transkrypt i opisy tablicy sa puste)")
    budget = num_ctx - cfg["num_predict"] - est_tokens(SYSTEM) - 500
    chunks = chunk_timeline(items, budget, cfg["chunk_minutes"])
    logger.info(f"LLM: {len(items)} elementow osi, {len(chunks)} fragmentow, model {model}, num_ctx {num_ctx}")
    cdir = workdir / "chunks"
    cdir.mkdir(exist_ok=True)
    client.check()
    client.assert_vram_free()
    notes = []
    try:
        for i, ch in enumerate(chunks, 1):
            path = cdir / f"chunk_{i:02d}.md"
            if path.exists():
                notes.append(path.read_text(encoding="utf-8"))
                continue
            logger.info(f"LLM fragment {i}/{len(chunks)} [{fmt_ts(ch[0]['t'])}-{fmt_ts(ch[-1]['t'])}]")
            prompt = (f"To jest fragment {i} z {len(chunks)} wykładu. Nie pisz wstępu ani podsumowania całości, "
                      f"tylko notatki z tego fragmentu.\n\n" + "\n".join(render(x) for x in ch))
            md = client.generate(model, prompt, system=SYSTEM,
                                 options={"num_ctx": num_ctx, "num_predict": cfg["num_predict"],
                                          "temperature": cfg["temperature"]},
                                 keep_alive="10m").strip()
            md = shift_headings(md)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(md, encoding="utf-8")
            tmp.replace(path)
            notes.append(md)
        heads = "\n".join(l for n in notes for l in n.splitlines() if l.startswith("## "))
        summary = ""
        if heads:
            summary = client.generate(model, heads, system=SUMMARY_SYSTEM,
                                      options={"num_ctx": num_ctx, "num_predict": 500, "temperature": 0.2},
                                      keep_alive="10m").strip()
    finally:
        try:
            client.unload(model)
        except Exception as e:
            logger.error(f"nie udalo sie rozladowac {model}: {e}")
    toc = "\n".join(f"- {l[3:]}" for l in heads.splitlines())
    doc = ["# Notatki z wykładu", "", "## Podsumowanie", "", summary, "", "## Spis treści", "", toc, "", "---", ""]
    for n in notes:
        doc += [n, ""]
    out = workdir / "lecture_notes.md"
    out.with_suffix(".tmp").write_text("\n".join(doc), encoding="utf-8")
    out.with_suffix(".tmp").replace(out)
    return out
