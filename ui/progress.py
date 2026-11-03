"""Przeglad postepow nauki: quizy, slabe zagadnienia i fiszki do powtorki dla kazdego wykladu."""
import time
from pathlib import Path

from ui.flashcards import FlashStore, is_due
from ui.quiz import QuizStore, _norm


def lecture_progress(wd, pass_ratio):
    quizzes = QuizStore(wd).all()
    ready = [q for q in quizzes if q.get("status") == "ready"]
    best, attempts, last, weak = None, 0, None, {}
    for q in ready:
        by_id = {x["id"]: x for x in q.get("questions", []) if isinstance(x, dict) and x.get("id")}
        latest = {}
        for a in q.get("attempts", []):
            if a.get("finished"):
                attempts += 1
                best = a["percent"] if best is None else max(best, a["percent"])
                last = a["finished"] if last is None else max(last, a["finished"])
                for qid, res in (a.get("answers") or {}).items():
                    latest[qid] = (a["finished"], res.get("score", 0))
        for qid, (_, score) in latest.items():
            x = by_id.get(qid)
            if x and score < 1:
                topic = x.get("topic") or x.get("section") or x["question"][:60]
                weak.setdefault(_norm(topic), [topic, 0])[1] += 1 - score
    cards = FlashStore(wd).all()
    now = time.time()
    return {"quizzes": len(ready), "attempts": attempts, "best": best, "last_attempt": last,
            "passed": best is not None and best >= pass_ratio * 100,
            "weak": [t for t, _ in sorted(weak.values(), key=lambda v: -v[1])[:5]],
            "cards": len(cards), "cards_due": sum(1 for c in cards if is_due(c, now))}
