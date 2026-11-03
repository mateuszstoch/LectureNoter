"""Kontrola wiernosci notatek: zdania slabo poparte transkryptem i odczytem tablicy oraz miejsca oznaczone [?].
Heurystyka leksykalna (bez modelu): jaka czesc znaczacych slow zdania wystepuje w materiale zrodlowym."""
import re

from ui.retrieval import tokens

THRESHOLD = 0.45
MIN_TOKENS = 5
TS = re.compile(r"\[\d{1,2}:\d{2}:\d{2}\]")


def _clean(line):
    line = TS.sub(" ", line)
    line = re.sub(r"\$\$?[^$]+\$\$?", " ", line)          # wzory
    line = re.sub(r"[*_`>#|]|^\s*(?:[-+]|\d+[.)])\s+", " ", line)
    return re.sub(r"\s+", " ", line).strip()


def sentences(md):
    out, fence = [], False
    for raw in (md or "").splitlines():
        if raw.strip().startswith("```"):
            fence = not fence
            continue
        if fence or raw.lstrip().startswith("#") or raw.strip().startswith("|") or not raw.strip():
            continue
        for s in re.split(r"(?<=[.!?])\s+", _clean(raw)):
            if s:
                out.append(s)
    return out


def check_notes(notes_md, transcript, vision):
    corpus = set()
    for s in transcript or []:
        corpus.update(tokens(s.get("text", "")))
    for v in vision or []:
        corpus.update(tokens(v.get("text") or ""))
    flagged, total = [], 0
    for s in sentences(notes_md):
        uncertain = "[?]" in s
        toks = tokens(s.replace("[?]", " "))
        if len(toks) < MIN_TOKENS and not uncertain:
            continue
        total += 1
        support = (sum(1 for t in toks if t in corpus) / len(toks)) if toks else 1.0
        if uncertain:
            flagged.append({"text": s, "kind": "uncertain", "support": round(support, 2)})
        elif corpus and support < THRESHOLD:
            flagged.append({"text": s, "kind": "unsupported", "support": round(support, 2)})
    return {"sentences": total, "flagged": flagged, "has_sources": bool(corpus)}
