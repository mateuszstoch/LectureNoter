import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import suggestions as sg  # noqa: E402
from server import create_app  # noqa: E402
from test_ui import CFG, NOTES, FakeOllama, env  # noqa: E402,F401

GOOD = {"keywords": ["maszyna Turinga", "funkcja przejścia", "problem decyzyjny", "taśma"],
        "questions": ["Jak działa funkcja przejścia?", "Czym różni się problem decyzyjny od obliczeniowego", "Po co maszynie taśma?"]}


class SuggOllama(FakeOllama):
    def __init__(self, reply=None, **kw):
        super().__init__(**kw)
        self.reply = json.dumps(GOOD) if reply is None else reply

    def generate(self, model, prompt, **kw):
        self.generated.append(prompt)
        return self.reply


def test_heuristic_uses_headings_not_generic_text():
    h = sg.heuristic(NOTES)
    assert h["source"] == "headings"
    assert h["keywords"] == ["Maszyna Turinga", "Problemy decyzyjne", "Organizacja"]
    assert all(("Turinga" in q or "decyzyjne" in q or "Organizacja" in q) for q in h["questions"])
    assert sg.heuristic("")["questions"] == ["Podsumuj ten wykład w punktach"]


def test_headings_skip_structural_sections():
    md = "# Notatki z wykładu\n## Podsumowanie\nx\n## Spis treści\n- a\n## 1. Grafy\ntekst\n## Grafy\nduplikat"
    assert sg.heuristic(md)["keywords"] == ["Grafy"]


def test_clean_validates_model_output():
    c = sg.clean(GOOD)
    assert c["questions"][1].endswith("?")                       # brakujacy znak zapytania jest dodawany
    assert sg.clean({"keywords": ["a"], "questions": []}) is None  # za malo uzytecznych danych
    assert sg.clean("nie slownik") is None
    dup = sg.clean({"keywords": ["x1", "X1", "y2", "z3"], "questions": ["Co?", "Jak?"]})
    assert dup["keywords"] == ["x1", "y2", "z3"]


def test_generate_parses_wrapped_json():
    cl = SuggOllama(reply="Oto wynik:\n" + json.dumps(GOOD) + "\nkoniec")
    assert sg.generate(cl, "m", NOTES, 8192)["keywords"][0] == "maszyna Turinga"
    assert sg.generate(SuggOllama(reply="nie json"), "m", NOTES, 8192) is None
    assert sg.generate(cl, "m", "  ", 8192) is None


def test_pipeline_helper_never_raises(tmp_path):
    class Boom(SuggOllama):
        def generate(self, *a, **k):
            raise RuntimeError("model padl")
    sg.try_generate_and_save(Boom(), "m", tmp_path, NOTES, 8192)       # nie rzuca
    assert not (tmp_path / sg.FILE).exists()
    sg.try_generate_and_save(SuggOllama(), "m", tmp_path, NOTES, 8192)
    assert sg.load(tmp_path)["source"] == "model"


@pytest.fixture
def sc(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    (lib.workdir("ok") / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    fake = SuggOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    return TestClient(app), fake, app, lib


def test_api_falls_back_to_headings_then_model(sc):
    c, fake, app, lib = sc
    first = c.get("/api/videos/ok/suggestions").json()
    assert first["source"] == "headings" and "Maszyna Turinga" in first["keywords"]
    made = c.post("/api/videos/ok/suggestions").json()
    assert made["source"] == "model" and made["keywords"][0] == "maszyna Turinga"
    assert c.get("/api/videos/ok/suggestions").json()["source"] == "model"       # zapisane w wykladzie
    assert (lib.workdir("ok") / "suggestions.json").exists()


def test_api_generate_errors(sc):
    c, fake, app, lib = sc
    fake.reply = "smiecie"
    assert c.post("/api/videos/ok/suggestions").status_code == 502
    fake.reply = json.dumps(GOOD)
    with app.state.chat.session("ok", "qwen2.5:7b-instruct"):                      # model zajety
        assert c.post("/api/videos/ok/suggestions").status_code == 409
    (lib.workdir("ok") / "lecture_notes.md").unlink()
    assert c.post("/api/videos/ok/suggestions").status_code == 400


def test_synthesis_writes_suggestions(tmp_path):
    import llm_synthesis as ls

    class C:
        def __init__(self):
            self.n = 0

        def check(self): pass
        def assert_vram_free(self): pass
        def unload(self, m): pass

        def generate(self, model, prompt, **kw):
            if "przygotuj podpowiedzi" in prompt:
                return json.dumps(GOOD)
            return "## Maszyna Turinga\nMaszyna ma taśmę i głowicę, a funkcja przejścia opisuje kolejne kroki obliczenia."
    (tmp_path / "transcript.json").write_text(json.dumps([{"start": 1, "end": 5, "text": "Maszyna Turinga ma taśmę."}]), encoding="utf-8")
    ls.run_synthesis(tmp_path, CFG["llm"], "m", 8192, client=C())
    d = json.loads((tmp_path / "suggestions.json").read_text(encoding="utf-8"))
    assert d["source"] == "model" and "maszyna Turinga" in d["keywords"]
