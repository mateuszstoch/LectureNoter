import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as m  # noqa: E402
from diagnostics import describe_failure, hint_for, signal_name, summarize_tail  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config.yaml"))


def test_hints_and_signal_names():
    assert "cuBLAS" in hint_for("Library libcublas.so.12 is not found or cannot be loaded")
    assert "pip install" in hint_for("ModuleNotFoundError: No module named 'faster_whisper'")
    assert "ollama serve" in hint_for("Connection refused")
    assert "pamieci GPU" in hint_for("CUDA failed with error out of memory")
    assert hint_for("cos zupelnie innego") is None
    assert signal_name(-6) == "SIGABRT" and signal_name(-11) == "SIGSEGV" and signal_name(1) is None


def test_describe_failure():
    msg = describe_failure("asr", 1, {"type": "ImportError", "message": "brak libcublas"}, [])
    assert msg.startswith("etap asr zakonczyl sie kodem 1: ImportError: brak libcublas") and "Wskazowka" in msg
    msg = describe_failure("asr", -6, None, ["", "Library libcublas.so.12 is not found", ""])
    assert "SIGABRT" in msg and "libcublas" in msg and "Wskazowka" in msg
    assert summarize_tail(["a", "", "b"], 6) == "a | b"


def make_orch(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    return m.PipelineOrchestrator(video, tmp_path / "w", CFG, profile="8gb")


def test_real_worker_failure_reports_cause_and_logs(tmp_path):
    """Prawdziwy podproces workera: padnie (brak faster-whisper albo nieprawidlowe wideo), przyczyna ma byc widoczna."""
    from loguru import logger
    o = make_orch(tmp_path)
    sink = logger.add(o.workdir / "pipeline.log")  # jak w main(): worker dopisuje do pipeline.log przez wlasny proces
    try:
        with pytest.raises(RuntimeError) as e:
            o._run_worker("asr")
    finally:
        logger.remove(sink)
    msg = str(e.value)
    assert msg.startswith("etap asr zakonczyl sie kodem 1: ") and len(msg) > len("etap asr zakonczyl sie kodem 1: ") + 5
    assert (o.workdir / "worker_error.json").exists()
    worker_log = (o.workdir / "worker_asr.log").read_text(encoding="utf-8")
    assert "Traceback" in worker_log or "blad etapu" in worker_log
    pipeline_log = (o.workdir / "pipeline.log").read_text(encoding="utf-8")
    assert "ostatnie linie stderr" in pipeline_log


def test_native_abort_without_python_traceback(tmp_path):
    class Abort(m.PipelineOrchestrator):
        def _worker_cmd(self, stage):
            return [sys.executable, "-c",
                    "import sys, os; sys.stderr.write('Library libcublas.so.12 is not found or cannot be loaded\\n');"
                    "sys.stderr.flush(); os.abort()"]
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    o = Abort(video, tmp_path / "w", CFG, profile="8gb")
    with pytest.raises(RuntimeError) as e:
        o._run_worker("asr")
    msg = str(e.value)
    assert "SIGABRT" in msg and "libcublas" in msg and "pip install -r requirements.txt" in msg
    assert "libcublas" in (o.workdir / "worker_asr.log").read_text(encoding="utf-8")


def test_error_state_records_stage_and_message(tmp_path):
    o = make_orch(tmp_path)
    o.has_audio = True

    def boom(stage):
        raise RuntimeError("etap vlm zakonczyl sie kodem 1: ConnectionError: Connection refused")
    o.runners = {s: (boom if s == "vlm" else (lambda st: [(o.workdir / f).write_text("x") for f in m.OUTPUTS[st]])) for s in m.STAGES}
    with pytest.raises(RuntimeError):
        o.run()
    rs = m.read_run_state(o.workdir)
    assert rs["status"] == "error" and rs["error_stage"] == "vlm" and "Connection refused" in rs["error"]


def test_vlm_fails_loudly_when_model_missing_and_resumes(tmp_path):
    import json
    import cv2
    import numpy as np
    from ollama_client import OllamaError
    import vlm_module

    (tmp_path / "frames").mkdir()
    meta = []
    for i in range(3):
        cv2.imwrite(str(tmp_path / "frames" / f"f{i}.jpg"), np.full((50, 80, 3), 255, np.uint8))
        meta.append({"file": f"f{i}.jpg", "timestamp": i * 10.0, "index": i})
    (tmp_path / "frames_metadata.json").write_text(json.dumps(meta))

    class Fake:
        def __init__(self, fail_on=()):
            self.fail_on, self.n, self.unloaded = set(fail_on), 0, []

        def check(self): pass
        def assert_vram_free(self): pass
        def unload(self, m): self.unloaded.append(m)

        def generate(self, model, prompt, **kw):
            self.n += 1
            if self.n in self.fail_on:
                raise OllamaError("model 'x' niedostepny: uruchom `ollama pull x`")
            return f"opis {self.n}"

    cfg = {"model": "x", "num_predict": 10, "keep_alive": "1m"}
    # brak modelu: pierwsza klatka przerywa caly etap (a nie 3 razy po cichu), model jest rozladowany
    c = Fake(fail_on=[1, 2, 3])
    with pytest.raises(OllamaError):
        vlm_module.run_vlm(tmp_path, cfg, client=c)
    assert c.n == 1 and not (tmp_path / "vision_context.json").exists() and c.unloaded == ["x"]
    # blad pojedynczej klatki (nie fatalny): etap konczy sie bledem, ale gotowe klatki zostaja w checkpoincie
    class Flaky(Fake):
        def generate(self, model, prompt, **kw):
            self.n += 1
            if self.n == 2:
                raise ValueError("chwilowy blad obrazu")
            return f"opis {self.n}"
    (tmp_path / "vision_context.jsonl").unlink()
    c2 = Flaky()
    with pytest.raises(RuntimeError, match="1 z 3 klatek bez opisu"):
        vlm_module.run_vlm(tmp_path, cfg, client=c2)
    assert not (tmp_path / "vision_context.json").exists()
    c3 = Fake()  # ponowne uruchomienie opisuje tylko brakujaca klatke
    res = vlm_module.run_vlm(tmp_path, cfg, client=c3)
    assert c3.n == 1 and len(res) == 3 and (tmp_path / "vision_context.json").exists()


def test_resumable_stage_keeps_checkpoint_after_failure_but_not_after_config_change(tmp_path):
    import copy
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    state = {"fail": True}

    def make(cfg):
        def runner(stage):
            if stage == "vlm":
                (o.workdir / "vision_context.jsonl").write_text("checkpoint\n")
                if state["fail"]:
                    raise RuntimeError("padl w polowie")
            for f in m.OUTPUTS[stage]:
                (o.workdir / f).write_text("x")
        o = m.PipelineOrchestrator(video, tmp_path / "w", cfg, profile="8gb", runners={s: runner for s in m.STAGES})
        o.has_audio = True
        return o

    o = make(CFG)
    with pytest.raises(RuntimeError):
        o.run()
    assert (o.workdir / "vision_context.jsonl").exists()
    # ponowne uruchomienie z ta sama konfiguracja: checkpoint zostaje (wznowienie)
    state["fail"] = True
    o2 = make(CFG)
    seen = []
    orig = o2.runners["vlm"]

    def spy(stage):
        seen.append((o2.workdir / "vision_context.jsonl").exists())
        orig(stage)
    o2.runners["vlm"] = spy
    with pytest.raises(RuntimeError):
        o2.run()
    assert seen == [True]
    # zmiana konfiguracji VLM: stary checkpoint jest nieaktualny i musi zniknac przed startem
    cfg2 = copy.deepcopy(CFG)
    cfg2["vlm"]["num_predict"] = 123
    o3 = make(cfg2)
    seen3 = []
    orig3 = o3.runners["vlm"]

    def spy3(stage):
        seen3.append((o3.workdir / "vision_context.jsonl").exists())
        orig3(stage)
    o3.runners["vlm"] = spy3
    state["fail"] = False
    o3.run()
    assert seen3 == [False]
