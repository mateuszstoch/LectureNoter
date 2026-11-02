"""Wyszukiwanie fragmentow wiedzy (BM25) i budowa kontekstu dla czatu: notatki, transkrypt, opisy tablicy."""
import math
import re

from llm_synthesis import dedupe_board, est_tokens, merge_speech
from io_utils import fmt_ts

STOP = set("""i w z na do o to jest się nie że a po od za jak co czy dla przez ale lub oraz tak ten ta te tym tego ich
jego jej są był była było być by już też tylko gdy które który która których tej tych mi mnie czym jaki jaka jakie
mówił mówi wykładowca wykład wykładzie""".split())
VOWELS = "aeiouyąęó"
TS_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")


def stem(w):
    """Prosty stemming dla polskiej fleksji: ucinanie do 5 znakow (dla krotszych zdejmuje koncowa samogloske)."""
    if len(w) > 5:
        return w[:5]
    if len(w) == 5 and w[-1] in VOWELS:
        return w[:4]
    return w


def tokens(text):
    return [stem(w) for w in re.findall(r"[\wąćęłńóśźż]+", text.lower()) if len(w) > 1 and w not in STOP]


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.n = len(docs)
        self.avg = (sum(len(d) for d in docs) / self.n) if self.n else 0
        self.df = {}
        for d in docs:
            for t in set(d):
                self.df[t] = self.df.get(t, 0) + 1
        self.tf = []
        for d in docs:
            c = {}
            for t in d:
                c[t] = c.get(t, 0) + 1
            self.tf.append(c)

    def scores(self, query):
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            for t in query:
                if t not in tf:
                    continue
                idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                f = tf[t]
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / (self.avg or 1)))
            out.append(s)
        return out


def split_notes(md):
    """Dzieli notatki na sekcje wg naglowkow #/##/###; kazda sekcja: tytul + tresc."""
    sections, title, buf = [], None, []
    for line in md.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            if title is not None or any(x.strip() for x in buf):
                sections.append({"title": title or "(wstęp)", "text": "\n".join(buf).strip()})
            title, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if title is not None or any(x.strip() for x in buf):
        sections.append({"title": title or "(wstęp)", "text": "\n".join(buf).strip()})
    return [s for s in sections if s["text"] or s["title"]]


def _fit(items, budget, render):
    """Zwraca elementy (juz uporzadkowane wg waznosci), ktore mieszcza sie w budzecie tokenow."""
    used, out = 0, []
    for it in items:
        cost = est_tokens(render(it))
        if used + cost > budget:
            if not out and budget > 200:  # przynajmniej najlepszy element, przyciety
                it = dict(it, text=it["text"][: budget * 3])
                out.append(it)
            continue
        out.append(it)
        used += cost
    return out


def _times_in(question):
    out = []
    for m in TS_RE.finditer(question):
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        out.append(a * 3600 + b * 60 + int(c) if c is not None else a * 60 + b)  # "15:30" = 15 min 30 s
    return out


def build_context(question, history, notes_md, transcript, vision, budget):
    """Zwraca (tekst kontekstu, lista zrodel). `history`: lista {role, content} (bez biezacego pytania)."""
    prev_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    prev_a = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")[:300]
    q = tokens(question) * 2 + tokens(prev_q) + tokens(prev_a)
    sources, blocks = [], []

    sections = split_notes(notes_md) if notes_md else []
    speech = merge_speech(transcript, window_s=75) if transcript else []
    boards = dedupe_board(vision or []) if vision else []

    notes_full = est_tokens(notes_md or "")
    if sections and notes_full <= 0.6 * budget:
        blocks.append(f"[Notatki z wykładu (całość)]\n{notes_md.strip()}")
        sources.append({"kind": "notes", "label": "Notatki (całość)"})
        rest = budget - notes_full
        speech_budget, board_budget = int(rest * 0.8), int(rest * 0.2)
    else:
        notes_budget, speech_budget, board_budget = int(budget * 0.5), int(budget * 0.35), int(budget * 0.15)
        if sections:
            sc = BM25([tokens(s["title"] + " " + s["text"]) for s in sections]).scores(q)
            ranked = [s for _, s in sorted(zip(sc, sections), key=lambda x: -x[0]) if _ > 0] or sections[:3]
            toc = "; ".join(s["title"] for s in sections)[:1500]
            blocks.append(f"[Spis treści notatek]\n{toc}")
            for s in _fit(ranked, notes_budget, lambda x: f"[Notatki: {x['title']}]\n{x['text']}"):
                blocks.append(f"[Notatki: {s['title']}]\n{s['text']}")
                sources.append({"kind": "notes", "label": s["title"]})

    def pick(items, budget_tokens, key_text):
        if not items or budget_tokens <= 0:
            return []
        sc = BM25([tokens(key_text(i)) for i in items]).scores(q)
        order = [i for _, i in sorted(zip(sc, range(len(items))), key=lambda x: -x[0])]
        wanted = []
        for t in _times_in(question):  # pytanie o konkretny moment: najblizszy element na poczatek
            near = min(range(len(items)), key=lambda i: abs(items[i]["t"] - t))
            wanted.append(near)
        order = list(dict.fromkeys(wanted + [i for i in order if sc[i] > 0]))
        chosen = _fit([items[i] for i in order], budget_tokens, lambda x: x["text"])
        return sorted(chosen, key=lambda x: x["t"])  # w kolejnosci chronologicznej

    for it in pick(speech, speech_budget, lambda x: x["text"]):
        blocks.append(f"[Mowa {fmt_ts(it['t'])}]\n{it['text']}")
        sources.append({"kind": "transcript", "label": f"Mowa {fmt_ts(it['t'])}", "t": it["t"]})
    for it in pick(boards, board_budget, lambda x: x["text"]):
        blocks.append(f"[Tablica {fmt_ts(it['t'])}]\n{it['text']}")
        sources.append({"kind": "board", "label": f"Tablica {fmt_ts(it['t'])}", "t": it["t"]})
    return "\n\n".join(blocks), sources
