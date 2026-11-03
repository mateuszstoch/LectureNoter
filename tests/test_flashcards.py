import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import create_app  # noqa: E402
from test_quiz import QuizOllama, make  # noqa: E402,F401
from test_ui import CFG, NOTES, env  # noqa: E402,F401
from ui import flashcards as fc  # noqa: E402


def test_schedule_progression():
    c = {"ease": 2.5, "interval": 0.0, "reps": 0, "lapses": 0}
    now = 1000.0
    fc.schedule(c, 2, now)
    assert c["interval"] == 1 and c["due"] == now + fc.DAY
    fc.schedule(c, 2, now)
    assert c["interval"] == 3
    fc.schedule(c, 2, now)
    assert c["interval"] == pytest.approx(7.5)             # 3 * 2.5
    fc.schedule(c, 0, now)                                  # zapomniane: wraca na poczatek
    assert c["reps"] == 0 and c["lapses"] == 1 and c["due"] == now + 600 and c["ease"] < 2.5
    fc.schedule(c, 3, now)
    assert c["interval"] >= 4 and c["ease"] > 2.3
    h = {"ease": 1.3, "interval": 2.0, "reps": 2, "lapses": 0}
    fc.schedule(h, 1, now)
    assert h["ease"] == 1.3 and h["interval"] == pytest.approx(2.4)     # latwosc nie spada ponizej 1.3


def test_store_dedupes_and_validates(tmp_path):
    st = fc.FlashStore(tmp_path)
    assert st.add_many([{"front": "Taśma", "back": "Pamięć."}, {"front": "taśma!", "back": "Inna."}, {"front": "", "back": "x"}]) == 1
    assert st.add_many([{"front": "Taśma", "back": "Znowu"}]) == 0
    cid = st.all()[0]["id"]
    with pytest.raises(fc.CardError):
        st.review(cid, 9)
    with pytest.raises(fc.CardError):
        st.review("brak", 2)
    assert st.review(cid, 2)["reps"] == 1
    st.delete(cid)
    assert st.all() == []


def test_anki_export_format():
    out = fc.export_anki([{"front": "Co to?\nLinia 2", "back": "A\tB", "source": "słownik"}])
    lines = out.splitlines()
    assert lines[:3] == ["#separator:tab", "#html:true", "#tags column:3"]
    assert lines[3] == "Co to?<br>Linia 2\tA B\tLectureNoter_słownik"


def test_cards_from_quiz_questions():
    q = {"id": "a", "type": "single", "question": "Q?", "options": [{"text": "x", "correct": True, "why": "bo tak"}, {"text": "y", "correct": False, "why": "nie"}]}
    c = fc.card_for_question(q)
    assert c["back"].startswith("Poprawne: x") and "bo tak" in c["back"] and "Błędne – y: nie" in c["back"]
    t = fc.card_for_question({"type": "term", "question": "Opis", "term": "Taśma", "explanation": "wyjaśnienie"})
    assert t["front"].startswith("Jak nazywa się pojęcie") and t["back"].startswith("Taśma")


@pytest.fixture
def fcl(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    wd = lib.workdir("ok")
    (wd / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    (wd / "outline.md").write_text("**Słownik:**\n- **Taśma**: nieskończona pamięć.\n- **Głowica**: czyta symbole.", encoding="utf-8")
    fake = QuizOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    return TestClient(app), fake, app


def test_flashcard_api_flow(fcl):
    c, fake, app = fcl
    assert c.get("/api/videos/ok/flashcards").json() == {"cards": [], "total": 0, "due": 0}
    r = c.post("/api/videos/ok/flashcards/from-glossary").json()
    assert r["added"] == 2 and r["due"] == 2
    assert c.post("/api/videos/ok/flashcards/from-glossary").json()["added"] == 0
    card = r["cards"][0]
    rr = c.post(f"/api/videos/ok/flashcards/{card['id']}/review", json={"grade": 2}).json()
    assert rr["due"] == 1 and rr["card"]["reps"] == 1
    assert c.post(f"/api/videos/ok/flashcards/{card['id']}/review", json={"grade": 7}).status_code == 400
    assert c.post("/api/videos/ok/flashcards/zly/review", json={"grade": 1}).status_code == 404
    ex = c.get("/api/videos/ok/flashcards/export")
    assert ex.status_code == 200 and "#separator:tab" in ex.text and "attachment" in ex.headers["content-disposition"]
    assert c.delete(f"/api/videos/ok/flashcards/{card['id']}").json()["total"] == 1
    assert c.post("/api/videos/ok/flashcards", json={"front": "Własna", "back": "Treść"}).json()["added"] == 1


def test_cards_from_wrong_quiz_answers_and_progress(fcl):
    c, fake, app = fcl
    fake.suggest = ["single"]
    q = make(c, type="single", count=3)
    att = c.post(f"/api/videos/ok/quizzes/{q['id']}/attempts").json()
    url = f"/api/videos/ok/quizzes/{q['id']}/attempts/{att['id']}/answer"
    for i, x in enumerate(q["questions"]):
        right = [j for j, o in enumerate(x["options"]) if o["correct"]]
        wrong = [j for j, o in enumerate(x["options"]) if not o["correct"]][:1]
        c.post(url, json={"question_id": x["id"], "answer": wrong if i == 0 else right})
    r = c.post("/api/videos/ok/flashcards/from-quizzes").json()
    assert r["added"] == 1 and r["cards"][0]["source"] == "quiz"
    assert c.post("/api/videos/ok/flashcards/from-quizzes").json()["added"] == 0
    p = c.get("/api/progress").json()
    row = next(x for x in p if x["id"] == "ok")
    assert row["quizzes"] == 1 and row["attempts"] == 1 and row["passed"] is False and row["best"] == pytest.approx(66.7, abs=0.1)
    assert len(row["weak"]) == 1 and row["cards"] == 1 and row["cards_due"] == 1


# ---------- czat po wszystkich wykladach ----------
def test_global_chat_across_lectures_with_scope(env):
    from test_ui import FakeOllama, parse_sse
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    lib.add_video("other.mp4")
    (lib.workdir("ok") / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    (lib.workdir("other") / "lecture_notes.md").write_text("# Notatki\n## Grafy\nGraf jest spójny, gdy istnieje ścieżka między każdą parą wierzchołków.\n", encoding="utf-8")
    fake = FakeOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    c = TestClient(app)
    cid = c.post("/api/videos/_all/chats", json={}).json()["id"]
    ev = parse_sse(c.post(f"/api/videos/_all/chats/{cid}/messages", json={"content": "Kiedy graf jest spójny?"}).text)
    assert ev[-1][0] == "done"
    sources = next(d for e, d in ev if e == "sources")
    assert any(s["kind"] == "lecture" and s["video_id"] == "other" and s["section"] == "Grafy" for s in sources)
    sys_prompt = fake.calls[-1]["messages"][0]["content"]
    assert "RÓŻNYCH wykładów" in sys_prompt and "[Wykład: " in sys_prompt
    assert c.get("/api/videos/_all/chats").json()[0]["count"] == 2
    assert c.get(f"/api/videos/_all/chats/{cid}/export").status_code == 200
    # zakres folderu: tylko wyklady z folderu
    fid = c.post("/api/folders", json={"name": "Semestr", "parent": None}, headers={"X-LectureNoter": "1"}).json()["id"]
    c.patch("/api/videos/ok", json={"folder": fid}, headers={"X-LectureNoter": "1"})
    cid2 = c.post("/api/videos/_all/chats", json={"scope": fid}).json()["id"]
    ev2 = parse_sse(c.post(f"/api/videos/_all/chats/{cid2}/messages", json={"content": "Co to maszyna Turinga?"}).text)
    labels = [s["video_id"] for s in next(d for e, d in ev2 if e == "sources")]
    assert labels and set(labels) == {"ok"}
    assert c.delete(f"/api/videos/_all/chats/{cid}").json() == {"ok": True}


def test_global_chat_without_notes_reports_error(env):
    from test_ui import FakeOllama, parse_sse
    lib, gpu, jm, videos, work = env
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=FakeOllama())
    c = TestClient(app)
    cid = c.post("/api/videos/_all/chats", json={}).json()["id"]
    ev = parse_sse(c.post(f"/api/videos/_all/chats/{cid}/messages", json={"content": "Cokolwiek?"}).text)
    assert ev[-1][0] == "error" and "Brak notatek" in ev[-1][1]
