"""Wyszukiwanie pelnotekstowe po wszystkich wykladach: tytuly, notatki (wersja edytowana ma pierwszenstwo),
transkrypty i odczyt tablicy. BM25 z prosta polska fleksja (stemming z ui.retrieval), frazy w cudzyslowie, fragmenty."""
import re
import threading
from pathlib import Path

from io_utils import fmt_ts, read_json
from llm_synthesis import dedupe_board, merge_speech
from ui import notes as notes_mod
from ui.retrieval import BM25, split_notes, stem, tokens

TITLE_BOOST = 3.0
ALL_TERMS_BOOST = 1.5
MAX_HITS_PER_LECTURE = 5
_WORD = re.compile(r"[\wąćęłńóśźż]+", re.I)


def parse_query(q):
    """(frazy w cudzyslowie, tokeny); gdy sa same frazy, ich slowa tez sa tokenami."""
    phrases = [" ".join(p.lower().split()) for p in re.findall(r'"([^"]+)"', q) if p.strip()]
    rest = re.sub(r'"[^"]*"', " ", q)
    toks = tokens(rest) or [t for p in phrases for t in tokens(p)]
    return phrases, toks


def _term_regex(stems):
    return re.compile(r"\b(?:" + "|".join(re.escape(x) for x in stems) + r")[\wąćęłńóśźż]*", re.I) if stems else None


def make_snippet(text, stems, before=70, after=170):
    """Fragment tekstu wokol pierwszego trafienia (bez znacznikow Markdown)."""
    plain = re.sub(r"[#*`>|]+", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not stems:
        return plain[:before + after]
    rx = _term_regex(stems)
    m = rx.search(plain)
    if not m:
        return plain[:before + after]
    start, end = max(0, m.start() - before), min(len(plain), m.end() + after)
    if start > 0:
        sp = plain.find(" ", start)
        start = sp + 1 if 0 <= sp < m.start() else start
    snippet = plain[start:end].strip()
    return ("… " if start > 0 else "") + snippet + (" …" if end < len(plain) else "")


class SearchIndex:
    def __init__(self, library, folders):
        self.lib, self.folders = library, folders
        self._lock = threading.RLock()
        self._lecture_cache = {}   # video_id -> (sygnatura plikow, dokumenty)
        self._bm25 = None          # (sygnatura calosci, docs, BM25)

    # --- dokumenty jednego wykladu (cache wg plikow) ---
    def _signature(self, wd):
        sig = []
        for name in (notes_mod.GENERATED, notes_mod.EDITED, "transcript.json", "vision_context.json"):
            p = wd / name
            if p.exists():
                st = p.stat()
                sig.append((name, st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def _lecture_docs(self, vid, wd):
        sig = self._signature(wd)
        cached = self._lecture_cache.get(vid)
        if cached and cached[0] == sig:
            return cached[1]
        docs = []
        try:
            md = notes_mod.get_active(wd)["md"]
        except notes_mod.NotesError:
            md = ""
        for s in split_notes(md) if md else []:
            docs.append({"kind": "notes", "label": s["title"], "heading": s["title"], "body": s["text"],
                         "text": f"{s['title']}\n{s['text']}"})
        transcript = read_json(wd / "transcript.json", []) or []
        for it in merge_speech(transcript, window_s=75):
            docs.append({"kind": "transcript", "label": f"Mowa {fmt_ts(it['t'])}", "t": it["t"], "text": it["text"]})
        vision = read_json(wd / "vision_context.json", []) or []
        for it in dedupe_board(vision):
            docs.append({"kind": "board", "label": f"Tablica {fmt_ts(it['t'])}", "t": it["t"], "text": it["text"]})
        for d in docs:
            d["tokens"] = tokens(d["text"])
            d["low"] = " ".join(d["text"].lower().split())
        self._lecture_cache[vid] = (sig, docs)
        return docs

    # --- caly korpus ---
    def _corpus(self):
        lectures = []
        for vid, meta in self.lib.iter_meta():
            lectures.append((vid, meta, self.lib.workdir(vid)))
        sig = []
        all_docs, info = [], {}
        folder_names = {f["id"]: f["path"] for f in self.folders.list()}
        for vid, meta, wd in lectures:
            title = meta.get("title") or vid
            fpath = folder_names.get(meta.get("folder"), [])
            info[vid] = {"title": title, "folder": meta.get("folder") if meta.get("folder") in folder_names else None,
                         "folder_path": fpath}
            sig.append((vid, title, tuple(fpath), self._signature(wd)))
            title_doc_text = f"{title} {' '.join(fpath)}"
            all_docs.append({"vid": vid, "kind": "title", "label": "Tytuł", "text": title_doc_text, "tokens": tokens(title_doc_text),
                             "low": " ".join(title_doc_text.lower().split())})
            for d in self._lecture_docs(vid, wd):
                all_docs.append(dict(d, vid=vid))
        sig = tuple(sig)
        if self._bm25 and self._bm25[0] == sig:
            return self._bm25[1], self._bm25[2], info
        bm = BM25([d["tokens"] for d in all_docs])
        self._bm25 = (sig, all_docs, bm)
        return all_docs, bm, info

    def search(self, q, folder=None, limit_lectures=30):
        q = (q or "").strip()
        phrases, toks = parse_query(q)
        if len(q) < 2 or not toks:
            return {"query": q, "terms": [], "results": [], "total_hits": 0}
        with self._lock:
            docs, bm, info = self._corpus()
            allowed = None
            if folder:
                allowed = self.folders.descendants(folder)
            scores = bm.scores(toks)
            tokset = set(toks)
            per_lecture = {}
            for d, sc in zip(docs, scores):
                if sc <= 0:
                    continue
                vid = d["vid"]
                if allowed is not None and info[vid]["folder"] not in allowed:
                    continue
                if phrases and not all(p in d["low"] for p in phrases):
                    continue
                if tokset <= set(d["tokens"]):
                    sc *= ALL_TERMS_BOOST
                if d["kind"] == "title":
                    sc *= TITLE_BOOST
                per_lecture.setdefault(vid, []).append((sc, d))
            results = []
            for vid, hits in per_lecture.items():
                hits.sort(key=lambda x: -x[0])
                title_hit = any(d["kind"] == "title" for _, d in hits)
                out = []
                for sc, d in hits[:MAX_HITS_PER_LECTURE]:
                    src = d["text"]
                    body = d.get("body")
                    if body and (rx := _term_regex(toks)) and rx.search(body):
                        src = body  # fragment z tresci sekcji: naglowek jest juz w etykiecie, wiec go nie powtarzamy
                    h = {"kind": d["kind"], "label": d["label"], "snippet": make_snippet(src, toks)}
                    if "t" in d:
                        h["t"] = d["t"]
                    if "heading" in d:
                        h["heading"] = d["heading"]
                    out.append(h)
                results.append({"video_id": vid, "title": info[vid]["title"], "folder": info[vid]["folder"],
                                "folder_path": info[vid]["folder_path"], "title_match": title_hit,
                                "score": hits[0][0] + 0.15 * sum(s for s, _ in hits[1:4]),
                                "hits": out, "hit_count": len(hits)})
            results.sort(key=lambda r: -r["score"])
            return {"query": q, "terms": sorted(set(toks)), "results": results[:limit_lectures],
                    "total_hits": sum(r["hit_count"] for r in results)}
