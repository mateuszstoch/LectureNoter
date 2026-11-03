import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ui import client, env, search, search_env  # noqa: E402,F401


@pytest.fixture
def qenv(search_env):
    c, lib = search_env
    quiz = {"id": "q1", "title": "Quiz o grafach", "type": "single", "status": "ready", "questions": [
        {"id": "a", "type": "single", "question": "Kiedy graf jest spójny?", "topic": "spójność grafu",
         "options": [{"text": "Gdy istnieje ścieżka między każdą parą wierzchołków", "correct": True, "why": ""}, {"text": "Gdy ma cykl", "correct": False, "why": ""}]},
        {"id": "b", "type": "term", "question": "Struktura bez cykli, w której każde dwa wierzchołki łączy jedna ścieżka", "term": "Drzewo", "aliases": [], "explanation": "Graf acykliczny."}],
        "attempts": []}
    (lib.workdir("other") / "quizzes.json").write_text(json.dumps({"quizzes": [quiz, {"id": "q2", "title": "w trakcie", "status": "generating", "questions": [
        {"id": "z", "type": "open", "question": "Niedokończone pytanie o grafy", "model_answer": "x"}], "attempts": []}]}), encoding="utf-8")
    return c, lib


def test_at_prefix_searches_titles_only(qenv):
    c, lib = qenv
    r = search(c, "@Turinga")                       # 'Turinga' jest w notatkach, ale w zadnym tytule
    assert r["scope"] == "titles" and r["results"] == []
    r = search(c, "@grafów")
    assert [x["video_id"] for x in r["results"]] == ["other"] and {h["kind"] for h in r["results"][0]["hits"]} == {"title"}
    assert search(c, "@ grafów")["results"][0]["video_id"] == "other"        # spacja po prefiksie jest dozwolona


def test_hash_prefix_searches_notes_only(qenv):
    c, lib = qenv
    r = search(c, "#granice")                       # 'granice' jest tez w transkrypcie ('granice funkcji') i na tablicy
    assert r["scope"] == "notes" and [x["video_id"] for x in r["results"]] == ["ok"]
    assert {h["kind"] for h in r["results"][0]["hits"]} == {"notes"}
    assert all(h["kind"] == "notes" for x in search(c, "#Turinga")["results"] for h in x["hits"])
    plain = search(c, "granice")                    # bez prefiksu: wszystkie zrodla
    assert {h["kind"] for x in plain["results"] for h in x["hits"]} >= {"notes", "transcript"}


def test_question_mark_prefix_searches_quizzes(qenv):
    c, lib = qenv
    r = search(c, "?spójny")
    assert r["scope"] == "quizzes" and [x["video_id"] for x in r["results"]] == ["other"]
    hit = r["results"][0]["hits"][0]
    assert hit["kind"] == "quiz" and hit["quiz_id"] == "q1" and "pytanie 1" in hit["label"]
    by_answer = search(c, "?ścieżka wierzchołków")                              # szuka tez w tresci odpowiedzi
    assert by_answer["results"][0]["hits"][0]["quiz_id"] == "q1"
    assert search(c, "?drzewo")["results"][0]["hits"][0]["label"].endswith("pytanie 2")   # pytanie typu termin: po nazwie pojecia
    assert search(c, "?niedokończone")["results"] == []                         # quiz w trakcie generowania nie jest indeksowany
    assert search(c, "spójny")["results"][0]["hits"][0]["kind"] != "quiz"       # zwykle wyszukiwanie nie miesza quizow


def test_prefix_with_folder_filter_and_short_queries(qenv):
    c, lib = qenv
    assert search(c, "@a")["results"] == [] and search(c, "#")["results"] == [] and search(c, "?")["results"] == []
    assert search(c, "@")["scope"] == "titles"


def test_quiz_index_refreshes_after_change(qenv):
    c, lib = qenv
    assert search(c, "?nowepojecie")["results"] == []
    p = lib.workdir("ok") / "quizzes.json"
    p.write_text(json.dumps({"quizzes": [{"id": "n", "title": "Nowy", "status": "ready", "attempts": [], "questions": [
        {"id": "1", "type": "term", "question": "Opis nowego pojęcia", "term": "Nowepojecie", "explanation": ""}]}]}), encoding="utf-8")
    r = search(c, "?nowepojecie")
    assert [x["video_id"] for x in r["results"]] == ["ok"]
