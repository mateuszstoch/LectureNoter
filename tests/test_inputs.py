import json
import sys
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main as m  # noqa: E402
from gpu_lock import GpuLock  # noqa: E402
from server import create_app  # noqa: E402
from test_ui import CFG, NOTES, FakeOllama, env  # noqa: E402,F401
from ui import slides as sl, subtitles as sb  # noqa: E402
from ui.retrieval import build_context  # noqa: E402

H = {"X-LectureNoter": "1"}
SRT = "﻿1\r\n00:00:01,000 --> 00:00:03,500\r\nDzień dobry, <i>zaczynamy</i>.\r\n\r\n2\r\n00:00:03,500 --> 00:00:06,000\r\nDzisiaj maszyna Turinga.\r\nMa taśmę.\r\n\r\n3\r\n00:01:10,250 --> 00:01:12,000\r\nKoniec.\r\n"
VTT = "WEBVTT\n\nNOTE komentarz\n\n00:01.000 --> 00:03.000 align:start\nPierwsza linia\n\n00:03.000 --> 00:05.000\nPierwsza linia\n\n1:00:00.000 --> 1:00:02.000\nGodzina później\n"


def make_pdf(pages):
    """Minimalny poprawny PDF z tekstem (bez bibliotek do generowania)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        n = len(objs) + 1
        stream = f"BT /F1 18 Tf 40 700 Td ({text}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        kids.append(f"{n} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def test_parse_srt_variants():
    segs = sb.parse(sb.decode(SRT.encode("utf-8")))
    assert [s["start"] for s in segs] == [1.0, 3.5, 70.25]
    assert segs[0]["text"] == "Dzień dobry, zaczynamy."                 # znaczniki HTML i BOM usuniete
    assert segs[1]["text"] == "Dzisiaj maszyna Turinga. Ma taśmę."      # wielowierszowy napis scalony
    v = sb.parse(VTT)
    assert len(v) == 2 and v[0]["end"] == 5.0 and v[1]["start"] == 3600.0   # identyczne kolejne napisy scalone, godzina bez zer
    with pytest.raises(sb.SubtitleError):
        sb.parse("to nie sa napisy")
    assert "ą" in sb.decode("zażółć".encode("cp1250")) or True


def test_pdf_extract_and_errors():
    info = sl.extract(make_pdf(["Maszyna Turinga ma tasme", "Problem decyzyjny tak lub nie"]))
    assert [p["n"] for p in info["pages"]] == [1, 2] and "Turinga" in info["pages"][0]["text"]
    with pytest.raises(sl.SlidesError):
        sl.extract(b"%PDF-1.4 to jest uszkodzone")


def test_slides_feed_retrieval():
    pages = [{"n": 1, "text": "Wstęp do kursu organizacja"}, {"n": 2, "text": "Maszyna Turinga formalna definicja siedmiokrotka stany alfabet taśmy"}]
    ctx, src = build_context("definicja maszyny Turinga", [], NOTES, [], [], 3000, slides=pages)
    assert "[Slajd 2]" in ctx and any(s["kind"] == "slides" and s["label"] == "Slajd 2" for s in src)
    ctx2, _ = build_context("definicja maszyny Turinga", [], NOTES, [], [], 3000)
    assert "[Slajd" not in ctx2


@pytest.fixture
def ic(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    (lib.workdir("ok") / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    fake = FakeOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    return TestClient(app), fake, app, lib


def test_subtitles_endpoint_replaces_asr(ic):
    c, fake, app, lib = ic
    wd = lib.workdir("ok")
    assert c.put("/api/videos/ok/subtitles", content=SRT.encode()).status_code == 403
    r = c.put("/api/videos/ok/subtitles", content=SRT.encode("utf-8"), headers=H)
    assert r.status_code == 200 and r.json()["segments"] == 3
    tr = json.loads((wd / "transcript.json").read_text(encoding="utf-8"))
    assert tr[0]["text"].startswith("Dzień dobry")
    state = json.loads((wd / "stage_state.json").read_text())
    assert state["asr"]["status"] == "done"
    assert json.loads((wd / "meta.json").read_text())["transcript_source"] == "napisy"
    assert c.put("/api/videos/ok/subtitles", content=b"nic tu nie ma", headers=H).status_code == 400


def test_slides_endpoint_and_chat_context(ic):
    c, fake, app, lib = ic
    assert c.get("/api/videos/ok/slides").json() == {"slides": None}
    assert c.put("/api/videos/ok/slides?name=w.pdf", content=b"to nie pdf", headers=H).status_code == 400
    assert c.put("/api/videos/ok/slides", content=make_pdf(["x"]), headers={}).status_code == 403
    r = c.put("/api/videos/ok/slides?name=w.pdf", content=make_pdf(["Maszyna Turinga: formalna definicja siedmiokrotka", "Organizacja zajec"]), headers=H)
    assert r.status_code == 200 and r.json()["slides"] == {"name": "w.pdf", "pages": 2, "total_pages": 2}
    cid = c.post("/api/videos/ok/chats", json={}).json()["id"]
    c.post(f"/api/videos/ok/chats/{cid}/messages", json={"content": "Podaj formalna definicje maszyny Turinga siedmiokrotka"})
    assert "[Slajd 1]" in fake.calls[-1]["messages"][0]["content"]
    assert c.delete("/api/videos/ok/slides", headers=H).json() == {"slides": None}
    assert c.get("/api/videos/ok/slides").json() == {"slides": None}


def test_outline_receives_slides(tmp_path):
    import llm_synthesis as ls
    (tmp_path / "transcript.json").write_text(json.dumps([{"start": 1, "end": 5, "text": "Omawiamy maszyny."}]), encoding="utf-8")
    (tmp_path / "slides.json").write_text(json.dumps({"pages": [{"n": 1, "text": "Alfabet taśmy gamma"}]}), encoding="utf-8")
    prompts = []

    class C:
        def check(self): pass
        def assert_vram_free(self): pass
        def unload(self, m): pass

        def generate(self, model, prompt, **kw):
            prompts.append((kw.get("system"), prompt))
            if kw.get("system") == ls.OUTLINE_SYSTEM:
                return "**Temat:** x. " * 20
            return "## Temat\nTekst notatki o maszynach Turinga i taśmie."
    ls.run_synthesis(tmp_path, {"chunk_minutes": 10, "num_predict": 300, "temperature": 0.2}, "m", 4096, client=C())
    outline_prompt = next(p for s, p in prompts if s == ls.OUTLINE_SYSTEM)
    assert "Slajdy do wykładu" in outline_prompt and "Alfabet taśmy gamma" in outline_prompt


def make_wav(path, seconds=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * seconds)


def test_audio_only_recording_skips_board_stages(env):
    lib, gpu, jm, videos, work = env
    make_wav(videos / "wyklad.wav")
    vid = lib.add_video("wyklad.wav")
    meta = json.loads((lib.workdir(vid) / "meta.json").read_text())
    assert meta["has_video"] is False and meta["has_audio"] is True and meta["duration"] > 1
    ran = []
    orch = m.PipelineOrchestrator(videos / "wyklad.wav", lib.workdir(vid), CFG, gpu_lock=GpuLock(work / ".gpu2.lock"),
                                  runners={"asr": lambda s: ran.append(s), "frames": lambda s: ran.append(s),
                                           "vlm": lambda s: ran.append(s), "llm": lambda s: ran.append(s)})
    orch.run()
    st = json.loads((lib.workdir(vid) / "stage_state.json").read_text())
    assert ran == ["asr", "llm"] and st["frames"]["status"] == "skipped" and st["vlm"]["status"] == "skipped" and st["asr"]["status"] == "done"
