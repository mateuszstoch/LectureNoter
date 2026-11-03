"""Fiszki z powtorkami rozlozonymi w czasie (uproszczone SM-2): ze slownika, z blednych odpowiedzi w quizach i reczne.
Zapis w work/<film>/flashcards.json; eksport do Anki (plik tekstowy rozdzielany tabulatorami)."""
import re
import threading
import time
import uuid
from pathlib import Path

from io_utils import atomic_write_json, read_json

DAY = 86400
GRADES = {0: "again", 1: "hard", 2: "good", 3: "easy"}


class CardError(Exception):
    pass


def _norm(t):
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


class FlashStore:
    _lock = threading.RLock()

    def __init__(self, workdir):
        self.path = Path(workdir) / "flashcards.json"

    def _load(self):
        d = read_json(self.path, {"cards": []})
        return d if isinstance(d, dict) and isinstance(d.get("cards"), list) else {"cards": []}

    def all(self):
        with self._lock:
            return [c for c in self._load()["cards"] if isinstance(c, dict) and c.get("id")]

    def add_many(self, items):
        """items: [{front, back, source}]. Pomija duplikaty (ten sam awers). Zwraca liczbe dodanych."""
        with self._lock:
            data = self._load()
            have = {_norm(c.get("front")) for c in data["cards"] if isinstance(c, dict)}
            now, added = time.time(), 0
            for it in items:
                front, back = (it.get("front") or "").strip(), (it.get("back") or "").strip()
                if not front or not back or _norm(front) in have:
                    continue
                have.add(_norm(front))
                data["cards"].append({"id": uuid.uuid4().hex[:8], "front": front[:1500], "back": back[:3000], "source": it.get("source") or "ręczna",
                                      "created": now, "due": now, "interval": 0.0, "ease": 2.5, "reps": 0, "lapses": 0, "last": None})
                added += 1
            if added:
                atomic_write_json(str(self.path), data)
            return added

    def review(self, cid, grade):
        if grade not in GRADES:
            raise CardError("Niepoprawna ocena.")
        with self._lock:
            data = self._load()
            for c in data["cards"]:
                if isinstance(c, dict) and c.get("id") == cid:
                    schedule(c, grade, time.time())
                    atomic_write_json(str(self.path), data)
                    return c
        raise CardError("nie ma takiej fiszki")

    def delete(self, cid):
        with self._lock:
            data = self._load()
            n = len(data["cards"])
            data["cards"] = [c for c in data["cards"] if not (isinstance(c, dict) and c.get("id") == cid)]
            if len(data["cards"]) == n:
                raise CardError("nie ma takiej fiszki")
            atomic_write_json(str(self.path), data)


def schedule(card, grade, now):
    """Uproszczone SM-2: 0 = nie pamietam (powtorka za 10 min), 1 = trudne, 2 = dobrze, 3 = latwo."""
    ease, interval, reps = card.get("ease", 2.5), card.get("interval", 0.0), card.get("reps", 0)
    if grade == 0:
        card.update(reps=0, interval=0.0, ease=max(1.3, ease - 0.2), lapses=card.get("lapses", 0) + 1, due=now + 600)
    else:
        if grade == 1:
            interval, ease = max(1.0, interval * 1.2), max(1.3, ease - 0.15)
        elif grade == 2:
            interval = 1.0 if reps == 0 else 3.0 if reps == 1 else interval * ease
        else:
            interval = 4.0 if reps == 0 else max(5.0, interval * ease * 1.3)
            ease += 0.1
        card.update(reps=reps + 1, interval=round(interval, 2), ease=round(ease, 2), due=now + interval * DAY)
    card["last"] = now


def is_due(card, now=None):
    return card.get("due", 0) <= (now or time.time())


def cards_from_quizzes(quizzes):
    """Karty z pytan, na ktore ostatnia odpowiedz byla bledna lub czesciowa."""
    items = []
    for quiz in quizzes:
        by_id = {q["id"]: q for q in quiz.get("questions", []) if isinstance(q, dict) and q.get("id")}
        latest = {}
        for att in quiz.get("attempts", []):
            if att.get("finished"):
                for qid, res in (att.get("answers") or {}).items():
                    latest[qid] = res.get("score", 0)
        for qid, score in latest.items():
            q = by_id.get(qid)
            if q and score < 1:
                items.append(card_for_question(q))
    return items


def card_for_question(q):
    t = q.get("type")
    if t in ("single", "multi"):
        ok = [o for o in q["options"] if o.get("correct")]
        back = "Poprawne: " + "; ".join(o["text"] for o in ok)
        why = [f"{o['text']}: {o['why']}" for o in ok if o.get("why")] + [f"Błędne – {o['text']}: {o['why']}" for o in q["options"] if not o.get("correct") and o.get("why")]
        back += ("\n" + "\n".join(why[:4])) if why else ""
        front = q["question"]
    elif t == "term":
        front, back = "Jak nazywa się pojęcie: " + q["question"], q["term"] + (f"\n{q['explanation']}" if q.get("explanation") else "")
    else:
        front = q["question"]
        back = q.get("model_answer", "") + (("\n• " + "\n• ".join(q["key_points"])) if q.get("key_points") else "")
    return {"front": front, "back": back, "source": "quiz"}


def export_anki(cards, deck_tag="LectureNoter"):
    """Plik tekstowy do importu w Anki (Plik > Importuj): kolumny awers, rewers, tagi; HTML wlaczony."""
    def cell(t):
        return (t or "").replace("\t", " ").replace("\r", "").replace("\n", "<br>")
    lines = ["#separator:tab", "#html:true", "#tags column:3"]
    for c in cards:
        tag = re.sub(r"\s+", "_", f"{deck_tag} {c.get('source', '')}".strip())
        lines.append(f"{cell(c['front'])}\t{cell(c['back'])}\t{tag}")
    return "\n".join(lines) + "\n"
