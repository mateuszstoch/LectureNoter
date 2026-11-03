import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import create_app  # noqa: E402
from test_ui import CFG, NOTES, FakeOllama, env  # noqa: E402,F401
from ui import glossary as gl, verify as vf  # noqa: E402

TRANSCRIPT = [{"start": 10, "end": 20, "text": "Dzisiaj zaczniemy od maszyny Turinga: ma nieskończoną taśmę, głowicę i stany."},
              {"start": 30, "end": 40, "text": "Funkcja przejścia delta opisuje kolejne kroki obliczenia maszyny."}]


def test_verify_flags_unsupported_and_uncertain():
    md = ("# Notatki\n## Maszyna\nMaszyna Turinga ma nieskończoną taśmę, głowicę i stany sterujące. [00:00:10]\n"
          "Kwantowe komputery fotoniczne rozwiązują wielomianowo problemy kryptograficzne ogromnych rozmiarów.\n"
          "Funkcja przejścia opisuje [?] kroki maszyny.\n```\nkod ignorowany ignorowany ignorowany ignorowany ignorowany\n```\n")
    r = vf.check_notes(md, TRANSCRIPT, [])
    kinds = {f["kind"]: f for f in r["flagged"]}
    assert "Kwantowe" in kinds["unsupported"]["text"] and kinds["unsupported"]["support"] < 0.45
    assert kinds["uncertain"]["text"].startswith("Funkcja przejścia")
    assert not any("Maszyna Turinga ma" in f["text"] for f in r["flagged"])      # dobrze poparte zdanie nie jest oznaczone
    assert not any("kod ignorowany" in f["text"] for f in r["flagged"])


def test_verify_without_sources_does_not_flag_everything():
    r = vf.check_notes("## A\nZdanie o czyms bardzo ciekawym i dlugim tutaj.", [], [])
    assert r["has_sources"] is False and r["flagged"] == []


def test_glossary_parse_outline_variants():
    md = ("**Temat:** x\n**Przebieg:** a\n**Słownik:**\n- **Maszyna Turinga**: model obliczeń z taśmą.\n"
          "- Głowica – element czytający i piszący na taśmie.\n* **Stan** - bieżący stan sterowania maszyny\n")
    t = gl.parse_outline(md)
    assert [x["term"] for x in t] == ["Maszyna Turinga", "Głowica", "Stan"]
    assert gl.parse_outline("brak slownika") == [] and gl.parse_outline("") == []


@pytest.fixture
def nc(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    wd = lib.workdir("ok")
    (wd / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    (wd / "transcript.json").write_text(json.dumps(TRANSCRIPT), encoding="utf-8")

    class F(FakeOllama):
        reply = "Prostsze wyjaśnienie."

        def generate(self, model, prompt, **kw):
            self.generated.append(prompt)
            if "słownik 10-25" in prompt:
                return json.dumps({"terms": [{"term": "Taśma", "definition": "Nieskończona pamięć maszyny."},
                                             {"term": "Głowica", "definition": "Czyta i zapisuje symbole na taśmie."}]})
            return self.reply
    fake = F()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    return TestClient(app), fake, app, wd


def test_explain_uses_context_and_validates(nc):
    c, fake, app, wd = nc
    (wd / "outline.md").write_text("**Temat:** Maszyny Turinga.", encoding="utf-8")
    r = c.post("/api/videos/ok/explain", json={"text": "Funkcja przejścia delta opisuje kroki.", "mode": "simpler"})
    assert r.status_code == 200 and r.json()["text"] == "Prostsze wyjaśnienie."
    p = fake.generated[-1]
    assert "Maszyny Turinga" in p and "Fragment do wyjaśnienia" in p and "prostszymi słowami" in p
    assert c.post("/api/videos/ok/explain", json={"text": "x", "mode": "expand"}).status_code == 400
    assert c.post("/api/videos/ok/explain", json={"text": "Jakiś fragment tekstu", "mode": "zly"}).status_code == 400
    with app.state.chat.session("ok", "qwen2.5:7b-instruct"):
        assert c.post("/api/videos/ok/explain", json={"text": "Jakiś fragment tekstu"}).status_code == 409


def test_verify_endpoint(nc):
    c, fake, app, wd = nc
    r = c.get("/api/videos/ok/notes/verify").json()
    assert r["has_sources"] is True and "flagged" in r


def test_glossary_endpoint_flow(nc):
    c, fake, app, wd = nc
    assert c.get("/api/videos/ok/glossary").json() == {"terms": [], "source": "none"}
    (wd / "outline.md").write_text("**Słownik:**\n- **Taśma**: pamięć.", encoding="utf-8")
    assert c.get("/api/videos/ok/glossary").json()["source"] == "outline"
    made = c.post("/api/videos/ok/glossary").json()
    assert made["source"] == "model" and [t["term"] for t in made["terms"]] == ["Taśma", "Głowica"]
    assert c.get("/api/videos/ok/glossary").json()["source"] == "model"
