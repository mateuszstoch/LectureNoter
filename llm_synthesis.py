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
    "Tablica opisuje stan końcowy planszy, więc jej treść mogła być zapisywana wcześniej niż jest pokazana. "
    "Elementy interfejsu programu (np. napis 'Tryb precyzyjny', paski narzędzi, nazwiska uczestników spotkania) "
    "to nie treść wykładu: pomijaj je. "
    "FORMAT ODPOWIEDZI: zwróć czysty Markdown, bez otaczania go blokiem ``` i bez słowa 'markdown'. "
    "Bloków ``` używaj wyłącznie dla prawdziwego kodu programu. Wzory zapisuj jako $...$ (w tekście) "
    "i $$...$$ (osobna linia), nie używaj \\( \\) ani \\[ \\]. "
    "Pisz wyłącznie po polsku (nazwy i terminy angielskie dopuszczalne), bez znaków chińskich. "
    "Nie dodawaj komentarzy o samej notatce (żadnych 'Uwagi', 'Ten fragment zawiera...'), "
    "nie numeruj fragmentów i nie pisz nagłówków typu 'Fragment 3'. "
    "Każdy temat opisuj tylko raz: jeśli został już opisany wcześniej (lista tematów poniżej), pomiń go "
    "lub dodaj wyłącznie nowe informacje."
)
SUMMARY_SYSTEM = ("Na podstawie listy nagłówków notatek z wykładu napisz po polsku krótkie (5-8 zdań) "
                  "podsumowanie tematyki wykładu. Nie dodawaj informacji spoza nagłówków. "
                  "Pomiń nagłówki dotyczące interfejsu programu (np. 'Tryb precyzyjny'). Bez znaków chińskich.")


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


CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
LABELED_FENCE = re.compile(r"^```\s*(?!markdown\b|md\b)[A-Za-z0-9_+-]+\s*$", re.I)


def strip_fences(md):
    """Usuwa opakowania ```markdown i zbłąkane ```; zachowuje bloki z jawnie podanym językiem (kod)."""
    out, inner = [], False
    for line in md.splitlines():
        t = line.strip()
        if t.startswith("```"):
            if inner and t == "```":
                inner = False
                out.append(line)
            elif not inner and LABELED_FENCE.match(t):
                inner = True
                out.append(line)
            elif not inner:
                continue  # ```markdown, ```md, bezjezykowe lub zbłąkane
            else:
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out)


def normalize_latex(md):
    md = re.sub(r"\\\[(.+?)\\\]", lambda m: f"$$ {m.group(1).strip()} $$", md, flags=re.S)
    md = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1).strip()}$", md, flags=re.S)
    return md


META_LINE = re.compile(r"^\s*(\*\*)?(ten fragment (notatek )?(zawiera|przedstawia)|uwagi?:?|notatki z wyk[łl]adu \d+)\b", re.I)
FRAGMENT_HEAD = re.compile(r"^#+\s*fragment\s+\d+(\s+z\s+\d+)?.*$", re.I)
TS_RANGE = re.compile(r"^\s*\*\*\[\d\d:\d\d:\d\d\]\*\*\s*-\s*\[\d\d:\d\d:\d\d\]\s*$")


def clean_chunk_md(md):
    md = normalize_latex(strip_fences(md))
    out, skip_list = [], False
    for line in md.splitlines():
        if FRAGMENT_HEAD.match(line) or TS_RANGE.match(line):
            continue
        if META_LINE.match(line):
            skip_list = True  # pomija tez ewentualna liste punktowa tuz pod komentarzem
            continue
        if skip_list and (line.strip() == "" or re.match(r"^\s*[-*]\s", line)):
            continue
        skip_list = False
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def drop_cjk_lines(md):
    return "\n".join(l for l in md.splitlines() if not CJK.search(l))


def headings(md, levels=("## ", "### ")):
    return [l.lstrip("#").strip() for l in md.splitlines() if l.startswith(levels)]


def shift_headings(md, min_level=2):
    """Gwarantuje, ze notatka z chunku nie zawiera H1 (tytul dodaje skladanie)."""
    return re.sub(r"^#(?!#)", "##", md, flags=re.M) if min_level == 2 else md


def run_synthesis(workdir, cfg, model, num_ctx, client=None, progress=None):
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
    steps = len(chunks) + 1  # fragmenty + podsumowanie
    if progress:
        progress.update(0, steps, "notatki")
    try:
        for i, ch in enumerate(chunks, 1):
            path = cdir / f"chunk_{i:02d}.md"
            if path.exists():
                notes.append(clean_chunk_md(path.read_text(encoding="utf-8")))
                if progress:
                    progress.update(i, steps, f"fragment {i}/{len(chunks)} (z cache)")
                continue
            logger.info(f"LLM fragment {i}/{len(chunks)} [{fmt_ts(ch[0]['t'])}-{fmt_ts(ch[-1]['t'])}]")
            covered = [h for n in notes for h in headings(n, ("## ",))]
            covered_txt = ("Tematy już opisane we wcześniejszych notatkach (nie powtarzaj ich): "
                           + "; ".join(dict.fromkeys(covered)) + "\n\n") if covered else ""
            prompt = (f"{covered_txt}Poniżej materiał z kolejnego odcinka wykładu. "
                      f"Napisz notatki tylko z tego materiału, bez wstępu i bez podsumowania całości.\n\n"
                      + "\n".join(render(x) for x in ch))
            opts = {"num_ctx": num_ctx, "num_predict": cfg["num_predict"], "temperature": cfg["temperature"]}
            md = clean_chunk_md(client.generate(model, prompt, system=SYSTEM, options=opts, keep_alive="10m"))
            if CJK.search(md):
                logger.warning(f"fragment {i}: znaki CJK w odpowiedzi, ponawiam")
                opts["temperature"] = 0.0
                md = clean_chunk_md(client.generate(model, prompt, system=SYSTEM, options=opts, keep_alive="10m"))
                md = drop_cjk_lines(md)
            md = shift_headings(md)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(md, encoding="utf-8")
            tmp.replace(path)
            notes.append(md)
            if progress:
                progress.update(i, steps, f"fragment {i}/{len(chunks)}")
        heads = "\n".join("## " + h for h in dict.fromkeys(h for n in notes for h in headings(n, ("## ",))))
        summary = ""
        if heads:
            summary = client.generate(model, heads, system=SUMMARY_SYSTEM,
                                      options={"num_ctx": num_ctx, "num_predict": 500, "temperature": 0.2},
                                      keep_alive="10m").strip()
            if CJK.search(summary):
                summary = drop_cjk_lines(summary)
        if progress:
            progress.update(steps, steps, "podsumowanie")
    finally:
        try:
            client.unload(model)
        except Exception as e:
            logger.error(f"nie udalo sie rozladowac {model}: {e}")
    toc = "\n".join(f"- {h}" for h in dict.fromkeys(headings("\n".join(notes), ("## ",))))
    doc = ["# Notatki z wykładu", "", "## Podsumowanie", "", summary, "", "## Spis treści", "", toc, "", "---", ""]
    for n in notes:
        doc += [n, ""]
    out = workdir / "lecture_notes.md"
    out.with_suffix(".tmp").write_text("\n".join(doc), encoding="utf-8")
    out.with_suffix(".tmp").replace(out)
    return out
