import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import create_app  # noqa: E402
from test_ui import CFG, env  # noqa: E402,F401

H = {"X-LectureNoter": "1"}


def make_stale(lib, vid, stages=("asr", "frames", "vlm", "llm")):
    """Etapy 'gotowe', ale z podpisami niepasujacymi do biezacej konfiguracji = nieaktualne."""
    wd = lib.workdir(vid)
    p = wd / "stage_state.json"
    state = json.loads(p.read_text()) if p.exists() else {}
    state.update({s: {"status": "done", "config": "stary", "input": "stary", "seconds": 1} for s in stages})
    p.write_text(json.dumps(state))


def make_valid(lib, vid, stages):
    """Etapy gotowe i aktualne (poprawne podpisy i pliki wynikowe)."""
    import main as m
    wd = lib.workdir(vid)
    for name in ("transcript.json", "frames_metadata.json", "vision_context.json", "lecture_notes.md"):
        (wd / name).write_text("[]" if name.endswith(".json") else "# n", encoding="utf-8")
    orch = m.PipelineOrchestrator(Path(json.loads((wd / "meta.json").read_text())["video_path"]), wd, CFG)
    state = json.loads((wd / "stage_state.json").read_text()) if (wd / "stage_state.json").exists() else {}
    for st in stages:
        state[st] = {"status": "done", "config": orch.config_sig(st), "input": orch.input_sig(st), "seconds": 1}
    (wd / "stage_state.json").write_text(json.dumps(state))


@pytest.fixture
def rc(env):
    lib, gpu, jm, videos, work = env
    for f in ("ok.mp4", "other.mp4", "sleep.mp4"):
        lib.add_video(f)
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",))
    return TestClient(app), lib, jm


def test_preview_lists_only_stale_and_commit_enqueues(rc):
    c, lib, jm = rc
    make_stale(lib, "ok")
    make_stale(lib, "other", ("llm",))
    r = c.post("/api/refresh", json={}).json()
    assert {x["id"] for x in r["will_run"]} == {"ok", "other"} and r["queued"] == 0 and not r["committed"]
    ok = next(x for x in r["will_run"] if x["id"] == "ok")
    assert ok["stages"] == ["asr", "frames", "vlm", "llm"]
    assert next(x for x in r["will_run"] if x["id"] == "other")["stages"] == ["llm"]
    assert not [j for j in jm.snapshot()["jobs"]]                     # podglad nic nie dodaje
    assert c.post("/api/refresh", json={"commit": True}).status_code == 403
    done = c.post("/api/refresh", json={"commit": True}, headers=H).json()
    assert done["queued"] == 2 and {j["video_id"] for j in jm.snapshot()["jobs"]} == {"ok", "other"}
    again = c.post("/api/refresh", json={"commit": True}, headers=H).json()      # drugi raz: juz w kolejce
    assert again["queued"] == 0 and {x["id"] for x in again["skipped"]} == {"ok", "other"}
    assert all("w kolejce" in x["reason"] for x in again["skipped"])


def test_folder_scope_includes_subfolders(rc):
    c, lib, jm = rc
    parent = c.post("/api/folders", json={"name": "Semestr", "parent": None}).json()["id"]
    child = c.post("/api/folders", json={"name": "Matma", "parent": parent}).json()["id"]
    make_stale(lib, "ok")
    make_stale(lib, "other")
    c.patch("/api/videos/ok", json={"folder": child}, headers=H)
    r = c.post("/api/refresh", json={"folder": parent}).json()
    assert [x["id"] for x in r["will_run"]] == ["ok"]                 # tylko wyklad z podfolderu, bez 'other' spoza folderu
    assert c.post("/api/refresh", json={"folder": "nie-ma"}).status_code == 404


def test_incomplete_option_and_up_to_date(rc):
    c, lib, jm = rc
    make_stale(lib, "ok", ("llm",))
    r = c.post("/api/refresh", json={}).json()
    assert [x["id"] for x in r["will_run"]] == ["ok"] and r["up_to_date"] == 2        # 'other' i 'sleep' nie sa nieaktualne (tylko niedokonczone)
    r2 = c.post("/api/refresh", json={"include_incomplete": True}).json()
    assert {x["id"] for x in r2["will_run"]} == {"ok", "other", "sleep"}


def test_missing_video_is_skipped_but_notes_only_refresh_allowed(rc):
    c, lib, jm = rc
    make_stale(lib, "ok", ("asr",))
    make_valid(lib, "other", ("asr", "frames", "vlm"))                  # wczesniejsze etapy aktualne
    make_stale(lib, "other", ("llm",))                                  # tylko notatki nieaktualne
    st = json.loads((lib.workdir("other") / "stage_state.json").read_text())
    assert st["asr"]["config"] != "stary" and st["llm"]["config"] == "stary"
    for vid in ("ok", "other"):                                         # film zniknal
        meta_p = lib.workdir(vid) / "meta.json"
        meta = json.loads(meta_p.read_text())
        meta["video_path"] = "/nie/ma/takiego.mp4"
        meta_p.write_text(json.dumps(meta))
    r = c.post("/api/refresh", json={}).json()
    assert [x["id"] for x in r["will_run"]] == ["other"]                # same notatki nie wymagaja filmu
    assert r["skipped"][0]["id"] == "ok" and "wideo" in r["skipped"][0]["reason"]


def test_edited_notes_flag(rc):
    c, lib, jm = rc
    make_stale(lib, "ok", ("llm",))
    (lib.workdir("ok") / "lecture_notes.edited.md").write_text("# moje", encoding="utf-8")
    assert c.post("/api/refresh", json={}).json()["will_run"][0]["has_edited_notes"] is True
