import base64
import copy
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as m  # noqa: E402
import ollama_client as oc  # noqa: E402
import vlm_module as vm  # noqa: E402
from diagnostics import hint_for  # noqa: E402
from ollama_client import OllamaClient, OllamaError  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config.yaml"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(oc.time, "sleep", lambda s: None)
    monkeypatch.setattr(vm, "_sleep", lambda s: None)


# ---------- klient: tresc bledu 500 i rozroznienie rodzajow bledow ----------
def serve(responses):
    """Serwer HTTP zwracajacy kolejno zadane odpowiedzi (status, body)."""
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            status, body = responses[min(len(calls), len(responses) - 1)]
            calls.append(status)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", calls


def test_500_keeps_ollama_error_text_and_is_not_fatal():
    err = {"error": "model runner has unexpectedly stopped, this may be due to resource limitations"}
    srv, host, calls = serve([(500, err)])
    try:
        with pytest.raises(OllamaError) as e:
            OllamaClient(host=host, retries=3).generate("m", "p")
    finally:
        srv.shutdown()
    msg = str(e.value)
    assert "unexpectedly stopped" in msg and "500" in msg and e.value.status == 500
    assert e.value.fatal is False and "niedostepn" not in msg   # wczesniej: komunikat 'niedostepna' wywolywal przerwanie etapu
    assert len(calls) == 3                                          # bledy 5xx sa ponawiane
    assert hint_for(msg) and "Proces modelu" in hint_for(msg)


def test_500_then_success_is_retried_transparently():
    srv, host, calls = serve([(500, {"error": "boom"}), (200, {"response": "ok"})])
    try:
        assert OllamaClient(host=host, retries=3).generate("m", "p") == "ok"
    finally:
        srv.shutdown()
    assert calls == [500, 200]


def test_client_errors_not_retried_and_missing_model_is_fatal():
    srv, host, calls = serve([(400, {"error": "zly obraz"})])
    try:
        with pytest.raises(OllamaError, match="HTTP 400: zly obraz") as e:
            OllamaClient(host=host, retries=3).generate("m", "p")
        assert len(calls) == 1 and e.value.fatal is False
    finally:
        srv.shutdown()
    srv, host, calls = serve([(404, {"error": "model not found"})])
    try:
        with pytest.raises(OllamaError, match="ollama pull m") as e:
            OllamaClient(host=host, retries=3).generate("m", "p")
        assert e.value.fatal is True and len(calls) == 1
    finally:
        srv.shutdown()


def test_connection_refused_is_fatal():
    with pytest.raises(OllamaError) as e:
        OllamaClient(host="http://127.0.0.1:1", retries=2, timeout=2).generate("m", "p")
    assert e.value.fatal is True and "niedostepna" in str(e.value)


# ---------- VLM ----------
def make_frames(tmp_path, n, bad=()):
    """Klatki o roznym poziomie szarosci (do rozpoznania w atrapie); `bad`: numery klatek, ktore 'wywracaja' model."""
    (tmp_path / "frames").mkdir(exist_ok=True)
    meta = []
    for i in range(n):
        img = np.full((900, 1600, 3), 10 + i * 10, np.uint8)
        cv2.imwrite(str(tmp_path / "frames" / f"f{i}.jpg"), img)
        meta.append({"file": f"f{i}.jpg", "timestamp": i * 10.0, "index": i})
    (tmp_path / "frames_metadata.json").write_text(json.dumps(meta))
    return meta


def decode(b64):
    return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)


class FakeVlm:
    def __init__(self, bad=(), bad_until=None, fail_first=0):
        self.bad, self.bad_until = set(bad), bad_until
        self.fail_first = fail_first
        self.calls, self.sizes, self.unloaded, self.options = [], [], [], []

    def check(self): pass
    def assert_vram_free(self): pass
    def unload(self, model): self.unloaded.append(model)

    def generate(self, model, prompt, images=None, options=None, **kw):
        img = decode(images[0])
        frame = (int(img[0, 0, 0]) - 10) // 10          # poziom szarosci -> numer klatki
        self.calls.append(frame)
        self.sizes.append(max(img.shape[:2]))
        self.options.append(options)
        if self.fail_first and len(self.calls) <= self.fail_first:
            raise OllamaError("Ollama zwrocila blad HTTP 500: model runner has unexpectedly stopped", status=500)
        if frame in self.bad and (self.bad_until is None or len(self.calls) < self.bad_until):
            raise OllamaError("Ollama zwrocila blad HTTP 500: model runner has unexpectedly stopped", status=500)
        return f"opis klatki {frame}"


VCFG = {"model": "v", "num_predict": 50, "keep_alive": "5m"}


def test_frame_retried_with_smaller_image_after_model_recycle(tmp_path):
    make_frames(tmp_path, 1)
    c = FakeVlm(fail_first=2)
    res = vm.run_vlm(tmp_path, dict(VCFG, reload_every=0), client=c)
    assert res[0]["text"] == "opis klatki 0" and "error" not in res[0]
    assert c.sizes == [1344, 1008, 739]                       # 1344 -> 75% -> 55%
    assert c.unloaded.count("v") == 3                         # odtworzenie modelu po kazdym z 2 bledow + koncowe rozladowanie
    assert all(o["num_ctx"] == 4096 and o["temperature"] == 0 for o in c.options)


def test_one_bad_frame_is_skipped_and_stage_completes(tmp_path):
    make_frames(tmp_path, 10)
    c = FakeVlm(bad={3})                                      # ta klatka zawsze wywraca model (np. dziwny obraz)
    res = vm.run_vlm(tmp_path, VCFG, client=c)                 # 1 z 10 <= min(3, 10//5)
    assert len(res) == 10
    assert res[3]["skipped"] is True and res[3]["text"] == "" and "unexpectedly stopped" in res[3]["error"]
    assert [r["text"] for r in res if r["file"] != "f3.jpg"] == [f"opis klatki {i}" for i in range(10) if i != 3]
    assert (tmp_path / "vision_context.json").exists()
    assert c.calls.count(3) == 3                              # 1 proba + 2 ponowienia


def test_too_many_failures_fail_stage_but_resume_finishes_only_missing(tmp_path):
    make_frames(tmp_path, 10)
    c = FakeVlm(bad={2, 4, 6})                                 # 3 zle klatki, a dozwolone sa min(3, 10//5)=2
    with pytest.raises(RuntimeError, match="3 z 10 klatek bez opisu"):
        vm.run_vlm(tmp_path, VCFG, client=c)
    assert not (tmp_path / "vision_context.json").exists()
    ok = FakeVlm()                                             # po naprawie Ollamy: tylko brakujace klatki
    res = vm.run_vlm(tmp_path, VCFG, client=ok)
    assert sorted(set(ok.calls)) == [2, 4, 6] and len(res) == 10 and not any(r.get("skipped") for r in res)


def test_consecutive_failures_abort_early(tmp_path):
    make_frames(tmp_path, 20)
    c = FakeVlm(bad=set(range(20)))                            # Ollama calkiem nie dziala
    with pytest.raises(RuntimeError, match="5 kolejnych klatek"):
        vm.run_vlm(tmp_path, VCFG, client=c)
    assert len(set(c.calls)) == 5                              # nie probujemy wszystkich 20 klatek


def test_model_is_refreshed_every_n_frames(tmp_path):
    make_frames(tmp_path, 8)
    c = FakeVlm()
    vm.run_vlm(tmp_path, dict(VCFG, reload_every=3), client=c)
    assert c.unloaded.count("v") == 3                          # przed klatka 4 i 7 + rozladowanie na koncu
    c2 = FakeVlm()
    (tmp_path / "vision_context.jsonl").unlink()
    vm.run_vlm(tmp_path, dict(VCFG, reload_every=0), client=c2)
    assert c2.unloaded.count("v") == 1                         # profilaktyka wylaczona


def test_corrupt_frame_is_skipped_not_fatal(tmp_path):
    make_frames(tmp_path, 10)
    (tmp_path / "frames" / "f5.jpg").write_bytes(b"to nie jest obraz")
    res = vm.run_vlm(tmp_path, VCFG, client=FakeVlm())
    assert res[5]["skipped"] is True and "nie da sie odczytac" in res[5]["error"] and res[6]["text"] == "opis klatki 6"


def test_resilience_settings_do_not_invalidate_existing_vlm_results(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    old = copy.deepcopy(CFG)
    old["vlm"] = {"model": "qwen2.5vl:7b", "num_predict": 800, "keep_alive": "10m"}   # konfiguracja sprzed zmiany
    new = copy.deepcopy(CFG)
    new["vlm"].update(max_side=900, num_ctx=2048, reload_every=5)
    a = m.PipelineOrchestrator(video, tmp_path / "w1", old, profile="8gb")
    b = m.PipelineOrchestrator(video, tmp_path / "w2", new, profile="8gb")
    assert a.config_sig("vlm") == b.config_sig("vlm")
    new["vlm"]["num_predict"] = 900                                                  # parametr zmieniajacy wynik: unieważnia
    assert a.config_sig("vlm") != m.PipelineOrchestrator(video, tmp_path / "w3", new, profile="8gb").config_sig("vlm")
