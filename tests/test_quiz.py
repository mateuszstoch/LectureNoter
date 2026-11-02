import json
import random
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import OllamaError  # noqa: E402
from server import create_app  # noqa: E402
from test_ui import CFG, NOTES, FakeOllama, env  # noqa: E402,F401
from ui import quiz as qz  # noqa: E402

STEMS = ["Do czego służy głowica w maszynie Turinga?", "Czym różni się problem decyzyjny od obliczeniowego?",
         "Dlaczego zaliczenie wymaga dwóch kolokwiów?", "Jak działa funkcja przejścia delta?",
         "Co oznacza nieskończona taśma w modelu obliczeń?", "Kiedy graf jest spójny według definicji z wykładu?",
         "Które klasy problemów zawierają faktoryzację liczb?", "Po co wprowadzono stany sterujące automatu?"]


def mk_choice(i, multi=False):
    ok = [True, True, False, False] if multi else [True, False, False, False]
    return {"question": STEMS[i % len(STEMS)], "section": "Maszyna Turinga",
            "options": [{"text": f"opcja {i}-{k} {'xyz'[k % 3]}{k * 7}", "correct": ok[k], "why": f"powód {k}"} for k in range(4)]}


class QuizOllama(FakeOllama):
    """Odpowiada JSON-em zaleznie od typu pytan w prompcie."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.n = 0
        self.delay = 0
        self.mode = None   # nadpisanie: "garbage" | "error"
        self.grade = {"score": 1, "feedback": "Dobrze."}

    def generate(self, model, prompt, **kw):
        self.generated.append(prompt)
        time.sleep(self.delay)
        if self.mode == "error":
            raise OllamaError("Ollama niedostepna")
        if "Oceń odpowiedź studenta" in prompt:
            return json.dumps(self.grade)
        if self.mode == "garbage":
            return "to nie jest json"
        want = int(prompt.split("Ułóż ")[1].split()[0])
        out = []
        for _ in range(want):
            i, self.n = self.n, self.n + 1
            if "jednokrotnego" in prompt:
                out.append(mk_choice(i))
            elif "wielokrotnego" in prompt:
                out.append(mk_choice(i, True))
            elif "podaj nazwę" in prompt:
                out.append({"definition": STEMS[i % len(STEMS)], "term": f"Pojęcie{i}", "aliases": [f"Alias{i}"], "explanation": "Uzupełnienie."})
            else:
                out.append({"question": STEMS[i % len(STEMS)], "model_answer": "Wzorcowa odpowiedź.", "key_points": ["a", "b"]})
        return json.dumps({"questions": out})


@pytest.fixture
def qc(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    (lib.workdir("ok") / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    fake = QuizOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    return TestClient(app), fake, app


def wait_ready(c, qid, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        q = c.get(f"/api/videos/ok/quizzes/{qid}").json()
        if q["status"] != "generating":
            return q
        time.sleep(0.05)
    raise AssertionError("quiz sie nie wygenerowal")


def make(c, **kw):
    body = {"type": "single", "count": 4, **kw}
    r = c.post("/api/videos/ok/quizzes", json=body)
    assert r.status_code == 200, r.text
    return wait_ready(c, r.json()["id"])


# ---------- jednostkowe ----------
def test_extract_json_variants():
    assert qz.extract_json('{"a": 1}') == {"a": 1}
    assert qz.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert qz.extract_json('Oto wynik: {"a": [1, 2]} koniec') == {"a": [1, 2]}
    assert qz.extract_json("nic") is None


def test_normalize_choice_rules():
    good = mk_choice(0)
    q = qz.normalize_question(good, "single", random.Random(1))
    assert q and sum(o["correct"] for o in q["options"]) == 1 and len(q["options"]) == 4
    two = mk_choice(0, True)
    assert qz.normalize_question(two, "single") is None          # dwie poprawne w jednokrotnym
    assert qz.normalize_question(good, "multi") is None           # jedna poprawna w wielokrotnym
    assert qz.normalize_question(two, "multi") is not None
    dup = mk_choice(0)
    dup["options"][1]["text"] = dup["options"][0]["text"]
    assert qz.normalize_question(dup, "single") is None           # powtorzona opcja
    lab = mk_choice(0)
    lab["options"][0]["text"] = "A) z etykieta"
    assert "z etykieta" == next(o["text"] for o in qz.normalize_question(lab, "single")["options"] if "etykiet" in o["text"])


def test_normalize_term_and_open():
    assert qz.normalize_question({"definition": "Taśma jest nieskończona", "term": "taśma"}, "term") is None  # zdradza odpowiedz
    t = qz.normalize_question({"definition": "Urządzenie z nieskończoną pamięcią", "term": "Maszyna Turinga", "aliases": ["MT"]}, "term")
    assert t["term"] == "Maszyna Turinga"
    assert qz.normalize_question({"question": "Q?"}, "open") is None
    assert qz.normalize_question({"question": "Q?", "model_answer": "A", "key_points": ["x"]}, "open")["key_points"] == ["x"]


def test_grade_term_tolerance():
    q = {"term": "Maszyna Turinga", "aliases": ["MT"]}
    assert qz.grade_term(q, "maszyna turinga.")
    assert qz.grade_term(q, "Maszyna Turinag")        # literowka
    assert qz.grade_term(q, "mt")
    assert not qz.grade_term(q, "automat skonczony")
    assert not qz.grade_term(q, "")


def test_split_counts():
    s = qz.QuizService._split_counts
    assert sum(n for _, n in s(10, ["a" * 100, "b" * 300])) == 10
    assert all(n >= 1 for _, n in s(5, ["x", "y", "z"]))
    few = s(2, list("abcdef"))
    assert len(few) == 2 and sum(n for _, n in few) == 2


# ---------- API ----------
def test_quiz_single_full_flow_and_pass(qc):
    c, fake, app = qc
    q = make(c, count=4)
    assert q["status"] == "ready" and len(q["questions"]) == 4 and q["summary"]["attempts"] == 0
    assert c.get("/api/videos/ok/quizzes").json()[0]["id"] == q["id"]
    att = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").json()
    last = None
    for qu in q["questions"]:
        right = [i for i, o in enumerate(qu["options"]) if o["correct"]]
        last = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts/{att['id']}/answer", json={"question_id": qu["id"], "answer": right}).json()
        assert last["result"]["correct"] is True
    assert last["attempt"]["finished"] and last["attempt"]["percent"] == 100 and last["summary"]["passed"] is True
    # drugie podejscie: bledne odpowiedzi nie zaliczaja, ale najlepszy wynik zostaje
    att2 = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").json()
    for qu in q["questions"]:
        wrong = [i for i, o in enumerate(qu["options"]) if not o["correct"]][:1]
        res = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts/{att2['id']}/answer", json={"question_id": qu["id"], "answer": wrong}).json()
        assert res["result"]["correct"] is False
    s = c.get(f"/api/videos/ok/quizzes/{q['id']}").json()["summary"]
    assert s["attempts"] == 2 and s["best"] == 100 and s["last"] == 0 and s["passed"] is True


def test_multi_requires_all_correct(qc):
    c, fake, app = qc
    q = make(c, type="multi", count=2)
    att = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").json()
    qu = q["questions"][0]
    right = [i for i, o in enumerate(qu["options"]) if o["correct"]]
    url = f"/api/videos/ok/quizzes/{q['id']}/attempts/{att['id']}/answer"
    assert c.post(url, json={"question_id": qu["id"], "answer": right[:1]}).json()["result"]["score"] == 0   # tylko czesc
    assert c.post(url, json={"question_id": qu["id"], "answer": right}).json()["result"]["score"] == 1       # poprawka nadpisuje
    assert c.post(url, json={"question_id": qu["id"], "answer": []}).status_code == 400
    assert c.post(url, json={"question_id": qu["id"], "answer": [99]}).status_code == 400


def test_term_quiz_and_self_override(qc):
    c, fake, app = qc
    q = make(c, type="term", count=2)
    att = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").json()
    url = f"/api/videos/ok/quizzes/{q['id']}/attempts/{att['id']}/answer"
    a, b = q["questions"]
    assert c.post(url, json={"question_id": a["id"], "answer": a["term"].lower()}).json()["result"]["correct"] is True
    wrong = c.post(url, json={"question_id": b["id"], "answer": "synonim"}).json()
    assert wrong["result"]["correct"] is False and wrong["attempt"]["finished"]
    fix = c.post(url, json={"question_id": b["id"], "answer": "synonim", "self_score": 1}).json()   # uznanie synonimu
    assert fix["result"]["graded_by"] == "self" and fix["attempt"]["percent"] == 100


def test_open_quiz_llm_grading_and_fallback(qc):
    c, fake, app = qc
    q = make(c, type="open", count=2)
    att = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").json()
    url = f"/api/videos/ok/quizzes/{q['id']}/attempts/{att['id']}/answer"
    a, b = q["questions"]
    fake.grade = {"score": 0.5, "feedback": "Brakuje drugiego elementu."}
    r = c.post(url, json={"question_id": a["id"], "answer": "Czesc odpowiedzi"}).json()["result"]
    assert r["score"] == 0.5 and r["graded_by"] == "llm" and "Brakuje" in r["feedback"]
    fake.mode = "error"   # model niedostepny: prosimy o samoocene
    r = c.post(url, json={"question_id": b["id"], "answer": "cos"}).json()
    assert r["result"]["graded"] is False and r["result"]["error"]
    fake.mode = None
    r = c.post(url, json={"question_id": b["id"], "answer": "cos", "self_score": 0.5}).json()
    assert r["attempt"]["finished"] and r["attempt"]["percent"] == 50
    assert c.post(url, json={"question_id": a["id"], "answer": "   "}).status_code == 400


def test_garbage_model_output_gives_error_status(qc):
    c, fake, app = qc
    fake.mode = "garbage"
    q = make(c, count=3)
    assert q["status"] == "error" and "poprawnych pytań" in q["error"]
    fake.mode = "error"
    q = make(c, count=3)
    assert q["status"] == "error" and "Ollama" in q["error"]
    assert c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").status_code == 400  # niegotowy quiz


def test_scope_sections_and_topic_reach_prompt(qc):
    c, fake, app = qc
    assert c.get("/api/videos/ok/quiz-scopes").json() == ["Maszyna Turinga", "Problemy decyzyjne", "Organizacja"]
    make(c, count=2, sections=["Organizacja"])
    p = fake.generated[-1]
    assert "Zaliczenie od 6 punktów" in p and "nieskończoną taśmę" not in p
    make(c, count=2, topic="problem decyzyjny")
    assert "Problem decyzyjny to pytanie" in fake.generated[-1]
    q = make(c, count=2, sections=["Nie ma takiej"])
    assert q["status"] == "error" and "obszar" in q["error"]


def test_partial_generation_warns(qc):
    c, fake, app = qc
    q = make(c, count=30)   # model zwraca tyle, ile poprosimy, ale zrodlo ma tylko 8 roznych tresci pytan -> duplikaty odrzucone
    assert q["status"] == "ready" and len(q["questions"]) < 30 and q["warning"]


def test_validation_and_delete(qc):
    c, fake, app = qc
    assert c.post("/api/videos/ok/quizzes", json={"type": "zly", "count": 3}).status_code == 400
    assert c.post("/api/videos/ok/quizzes", json={"type": "open", "count": 0}).status_code == 400
    assert c.post("/api/videos/ok/quizzes", json={"type": "open", "count": 99}).status_code == 400
    assert c.post("/api/videos/ok/quizzes", json={"type": "open", "count": 3, "model": "brak:1b"}).status_code == 400
    q = make(c, count=2)
    assert c.delete(f"/api/videos/ok/quizzes/{q['id']}").json() == {"ok": True}
    assert c.get(f"/api/videos/ok/quizzes/{q['id']}").status_code == 404
    assert c.delete(f"/api/videos/ok/quizzes/{q['id']}").status_code == 404
    assert c.get("/api/videos/ok/quizzes").json() == []


def test_stale_generating_marked_error_after_restart(qc):
    c, fake, app = qc
    from ui.library import Library
    wd = app.state.quizzes.lib.workdir("ok")
    qz.QuizStore(wd).add({"id": "old1", "title": "x", "type": "open", "status": "generating", "questions": [], "attempts": [], "created": 1})
    assert c.get("/api/videos/ok/quizzes").json()[0]["status"] == "error"


def test_quiz_blocks_chat_and_survives_in_delete_preview(qc):
    c, fake, app = qc
    q = make(c, count=2)
    assert c.get("/api/videos/ok/delete-preview").json()["quiz_count"] == 1
    chat = app.state.chat
    with chat.session("ok", "qwen2.5:7b-instruct"):  # model zajety (np. generowanie quizu)
        cid = c.post("/api/videos/ok/chats", json={}).json()["id"]
        ev = c.post(f"/api/videos/ok/chats/{cid}/messages", json={"content": "Co to maszyna Turinga?"}).text
        assert "zajęty" in ev
        assert c.delete("/api/videos/ok").status_code == 409
    assert q["status"] == "ready"


def test_parallel_quizzes_wait_for_model_instead_of_failing(qc):
    c, fake, app = qc
    fake.delay = 0.3
    ids = [c.post("/api/videos/ok/quizzes", json={"type": "term", "count": 2}).json()["id"] for _ in range(3)]
    states = [wait_ready(c, i, timeout=20)["status"] for i in ids]
    assert states == ["ready"] * 3


def test_delete_while_waiting_cancels_generation(qc):
    c, fake, app = qc
    fake.delay = 0.3
    a = c.post("/api/videos/ok/quizzes", json={"type": "term", "count": 2}).json()["id"]
    b = c.post("/api/videos/ok/quizzes", json={"type": "term", "count": 2}).json()["id"]
    assert c.delete(f"/api/videos/ok/quizzes/{b}").status_code == 200
    assert wait_ready(c, a, timeout=20)["status"] == "ready"
    t = time.time()
    while app.state.quizzes.is_generating("ok") and time.time() - t < 10:
        time.sleep(0.05)
    assert not app.state.quizzes.is_generating("ok") and c.get("/api/videos/ok/quizzes").json()[0]["id"] == a
