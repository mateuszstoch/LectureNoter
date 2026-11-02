import copy
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as m  # noqa: E402
import progress  # noqa: E402
import run_state as rsmod  # noqa: E402

CFG = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config.yaml"))


def make(tmp_path, fail=None, cfg=CFG):
    video = tmp_path / "wyklad 1.mp4"
    if not video.exists():
        video.write_bytes(b"x" * 100)
    calls = []

    def runner(stage):
        calls.append(stage)
        if fail and fail[0] == stage:
            raise fail[1]
        for o in m.OUTPUTS[stage]:
            (tmp_path / "w" / o).write_text(f"{len(calls)}")

    o = m.PipelineOrchestrator(video, tmp_path / "w", cfg, profile="8gb", runners={s: runner for s in m.STAGES})
    o.has_audio = True
    return o, calls


def test_meta_created(tmp_path):
    o, _ = make(tmp_path)
    o.run()
    meta = m.read_json(tmp_path / "w" / "meta.json")
    assert meta["title"] == "wyklad 1" and meta["video_path"].endswith("wyklad 1.mp4")
    assert meta["language"] == "pl" and meta["created"]


def test_from_workdir_roundtrip(tmp_path):
    o, _ = make(tmp_path)
    o.run()
    o2 = m.PipelineOrchestrator.from_workdir(tmp_path / "w", CFG, profile="8gb")
    assert o2.video == o.video.resolve() and o2.status()["overall"] == "done"


def test_status_pending_done_stale(tmp_path):
    o, _ = make(tmp_path)
    assert o.status()["overall"] == "idle"
    assert all(s["status"] == "pending" for s in o.status()["stages"].values())
    o.run()
    st = o.status()
    assert st["overall"] == "done" and all(s["status"] == "done" for s in st["stages"].values())
    cfg = copy.deepcopy(CFG)
    cfg["llm"]["temperature"] = 0.9
    o2, _ = make(tmp_path, cfg=cfg)
    assert o2.status()["stages"]["llm"]["status"] == "stale"
    assert o2.status()["stages"]["vlm"]["status"] == "done"


def test_status_error_and_cancelled(tmp_path):
    o, _ = make(tmp_path, fail=("vlm", RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        o.run()
    st = o.status()
    assert st["overall"] == "error" and st["stages"]["vlm"]["status"] == "error"
    assert st["stages"]["asr"]["status"] == "done" and st["run"]["error"] == "boom"

    o2, _ = make(tmp_path, fail=("llm", m.Cancelled()))
    with pytest.raises(m.Cancelled):
        o2.run()
    st = o2.status()
    assert st["overall"] == "cancelled" and st["stages"]["llm"]["status"] == "cancelled"
    assert st["stages"]["vlm"]["status"] == "done"  # wznowienie pominie gotowe etapy


def test_interrupted_detected_by_stale_heartbeat(tmp_path):
    o, _ = make(tmp_path)
    o.run(only_stage="asr")
    rsmod.write_run_state(tmp_path / "w", status="running", pid=99999999, current_stage="frames",
                          heartbeat=time.time() - 3600)
    st = o.status()
    assert st["overall"] == "interrupted" and st["stages"]["frames"]["status"] == "interrupted"
    o.run()  # wznowienie jest dozwolone
    assert o.status()["overall"] == "done"


def test_running_and_refuse_parallel_run(tmp_path):
    o, calls = make(tmp_path)
    rsmod.write_run_state(tmp_path / "w", status="running", pid=os.getppid(), current_stage="vlm",
                          heartbeat=time.time())
    st = o.status()
    assert st["overall"] == "running" and st["stages"]["vlm"]["status"] == "running"
    with pytest.raises(RuntimeError, match="juz inne uruchomienie"):
        o.run()
    assert calls == []


def test_heartbeat_updates(tmp_path):
    rsmod.write_run_state(tmp_path, status="running", heartbeat=0)
    hb = rsmod.Heartbeat(tmp_path, interval=0.05).start()
    time.sleep(0.3)
    hb.stop()
    assert rsmod.read_run_state(tmp_path)["heartbeat"] > 0
    assert rsmod.read_run_state(tmp_path)["status"] == "running"  # heartbeat nie nadpisuje reszty


def test_progress_reporter(tmp_path):
    r = progress.ProgressReporter(tmp_path, "vlm", min_interval=10)
    r.update(1, 10, "a")
    r.update(2, 10, "b")  # pominiete (throttling)
    assert progress.read_progress(tmp_path)["vlm"]["done"] == 1
    r.update(10, 10, "koniec")  # koniec zawsze zapisany
    assert progress.read_progress(tmp_path)["vlm"]["done"] == 10
    r.reset()
    assert "vlm" not in progress.read_progress(tmp_path)


def test_invalidate_clears_progress(tmp_path):
    o, _ = make(tmp_path)
    progress.ProgressReporter(tmp_path / "w", "vlm").update(3, 5, "x")
    o.invalidate("vlm")
    assert "vlm" not in progress.read_progress(tmp_path / "w")
