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
from test_ui import CFG, FakeOllama, env  # noqa: E402,F401
from ui import health as hl  # noqa: E402

H = {"X-LectureNoter": "1"}


def by_id(checks):
    return {c["id"]: c for c in checks}


def test_checks_missing_model_and_ollama_down(tmp_path):
    ok = by_id(hl.run_checks(CFG, ["qwen2.5:7b-instruct"], True, tmp_path))
    assert ok["ollama"]["status"] == "ok" and ok["model:qwen2.5:7b-instruct"]["status"] == "ok"
    vlm = ok["model:qwen2.5vl:7b"]
    assert vlm["status"] == "fail" and vlm["model"] == "qwen2.5vl:7b"        # brakujacy model mozna pobrac
    down = by_id(hl.run_checks(CFG, [], False, tmp_path))
    assert down["ollama"]["status"] == "fail" and down["model:qwen2.5vl:7b"]["status"] == "warn"


def test_checks_cpu_profile_models(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTURENOTER_DEVICE", "cpu")
    ch = by_id(hl.run_checks(CFG, [], True, tmp_path))
    assert ch["gpu"]["status"] == "warn" and "CPU" in ch["gpu"]["detail"]
    assert "model:qwen2.5:3b-instruct" in ch and "model:qwen2.5vl:3b" in ch
    assert "small" in ch["asr"]["detail"] and "cpu" in ch["asr"]["detail"]


def test_model_name_validation():
    pm = hl.PullManager("http://x")
    for bad in ("", "a b", "a;rm -rf", "../../etc", "x" * 200, "-x"):
        with pytest.raises(ValueError):
            pm.start(bad)


class FakeResp:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        for ln in self.lines:
            yield json.dumps(ln).encode()


def wait_done(pm, model, timeout=5):
    t = time.time()
    while time.time() - t < timeout:
        s = pm.snapshot()[model]
        if s["done"]:
            return s
        time.sleep(0.02)
    raise AssertionError("pobieranie sie nie zakonczylo")


def test_pull_progress_and_errors(monkeypatch):
    seen = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        seen.update(url=url, body=json)
        return FakeResp([{"status": "pulling manifest"}, {"status": "pulling abc", "total": 200, "completed": 50},
                         {"status": "pulling abc", "total": 200, "completed": 200}, {"status": "success"}])
    monkeypatch.setattr(hl.requests, "post", fake_post)
    pm = hl.PullManager("http://ollama:11434")
    pm.start("qwen2.5:3b-instruct")
    s = wait_done(pm, "qwen2.5:3b-instruct")
    assert s["percent"] == 100.0 and s["error"] is None and seen["url"] == "http://ollama:11434/api/pull" and seen["body"]["model"] == "qwen2.5:3b-instruct"

    monkeypatch.setattr(hl.requests, "post", lambda *a, **k: FakeResp([{"error": "pull model manifest: file does not exist"}]))
    pm.start("nie-ma:1b")
    assert "does not exist" in wait_done(pm, "nie-ma:1b")["error"]


def test_health_api_and_pull_requires_header(env, monkeypatch):
    lib, gpu, jm, videos, work = env
    fake = FakeOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    c = TestClient(app)
    resp = c.get("/api/environment")
    assert resp.status_code == 200, resp.text
    r = resp.json()
    ids = {x["id"] for x in r["checks"]}
    assert {"python", "ffmpeg", "gpu", "ollama", "disk"} <= ids and r["pulls"] == {}
    monkeypatch.setattr(hl.requests, "post", lambda *a, **k: FakeResp([{"status": "success"}]))
    assert c.post("/api/models/pull", json={"model": "qwen2.5vl:7b"}).status_code == 403          # bez naglowka
    assert c.post("/api/models/pull", json={"model": "zła nazwa"}, headers=H).status_code == 400
    ok = c.post("/api/models/pull", json={"model": "qwen2.5vl:7b"}, headers=H)
    assert ok.status_code == 200
    t = time.time()
    while time.time() - t < 5 and not c.get("/api/environment").json()["pulls"].get("qwen2.5vl:7b", {}).get("done"):
        time.sleep(0.05)
    assert c.get("/api/environment").json()["pulls"]["qwen2.5vl:7b"]["percent"] == 100.0
