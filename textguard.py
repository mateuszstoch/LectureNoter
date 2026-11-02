"""Ochrona przed petlami powtorzen w tekscie z modeli (LLM, VLM, ASR): wykrywanie i usuwanie.

Model potrafi wpasc w petle ('relacji relacji relacji ...' na tysiace slow). Petle sa wykrywane na poziomie slow:
- jedno slowo powtorzone co najmniej MIN_WORD_REPEATS razy z rzedu,
- fraza dlugosci 2..MAX_PERIOD slow powtorzona co najmniej MIN_PHRASE_REPEATS razy.
Ciagi cyfr i symboli (np. '0 1 0 1 0 1', 'q q q') nie sa uznawane za petle, bo bywaja trescia wykladu."""
import re

MIN_WORD_REPEATS = 12
MIN_PHRASE_REPEATS = 4
MAX_PERIOD = 15
_TOKEN = re.compile(r"\S+")


def _norm(tok):
    return re.sub(r"\W", "", tok.lower())


def _wordy(norm_tok):
    return bool(re.search(r"[^\W\d_]{3,}", norm_tok))  # co najmniej 3 litery pod rzad


def find_loops(text):
    """Lista petli: [{'start', 'end', 'keep_end', 'period', 'repeats'}] (pozycje znakow; keep_end = koniec 1. powtorzenia)."""
    toks = [(m.start(), m.end(), _norm(m.group())) for m in _TOKEN.finditer(text)]
    n, i, loops = len(toks), 0, []
    while i < n:
        found = None
        for p in range(1, min(MAX_PERIOD, (n - i) // 2) + 1):
            if toks[i][2] != toks[i + p][2]:
                continue  # szybkie odrzucenie: kolejny okres musi zaczynac sie tak samo
            unit = [t[2] for t in toks[i:i + p]]
            if not any(_wordy(u) for u in unit):
                continue  # same cyfry/symbole: to moze byc tresc (ciagi zer i jedynek, stany q q q)
            r = 1
            while i + (r + 1) * p <= n and [t[2] for t in toks[i + r * p:i + (r + 1) * p]] == unit:
                r += 1
            if r >= (MIN_WORD_REPEATS if p == 1 else MIN_PHRASE_REPEATS):
                found = (p, r, unit)
                break
        if not found:
            i += 1
            continue
        p, r, unit = found
        last = i + r * p - 1
        # ucieta na koncu, niedokonczona kopia pierwszego slowa ('relacji relacji rel') tez nalezy do petli
        while last + 1 < n and len(toks[last + 1][2]) >= 2 and unit[0].startswith(toks[last + 1][2]) and toks[last + 1][2] != unit[0]:
            last += 1
        loops.append({"start": toks[i][0], "end": toks[last][1], "keep_end": toks[i + p - 1][1], "period": p, "repeats": r})
        i = last + 1
    return loops


def has_loop(text):
    return bool(find_loops(text))


def collapse_loops(text):
    """Zostawia pierwsze powtorzenie kazdej petli, a reszte wycina."""
    loops = find_loops(text)
    if not loops:
        return text
    out, pos = [], 0
    for lp in loops:
        out.append(text[pos:lp["keep_end"]])
        pos = lp["end"]
    out.append(text[pos:])
    return "".join(out)


def loop_chars(text):
    return sum(lp["end"] - lp["start"] for lp in find_loops(text))


def clean_heading(h, limit=90):
    """Naglowek bez petli i bez przesadnej dlugosci (trafia do list tematow, spisu tresci i podsumowania)."""
    return collapse_loops(h).strip()[:limit].rstrip()
