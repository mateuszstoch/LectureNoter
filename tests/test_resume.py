import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as m  # noqa: E402

CFG = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config.yaml"))


@pytest.fixture
def setup(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 100)
    calls = []

    def make(stage):
        def runner(s):
            calls.append(s)
            for o in m.OUTPUTS[s]:
                (tmp_path / "w" / o).write_text(f"{len(calls)}")
        return runner

    def orch(cfg=CFG):
        o = m.PipelineOrchestrator(video, tmp_path / "w", cfg, profile="8gb", runners={s: make(s) for s in m.STAGES})
        o.has_audio = True
        return o
    return orch, calls, tmp_path


def test_second_run_skips_everything(setup):
    orch, calls, _ = setup
    orch().run()
    assert calls == m.STAGES
    calls.clear()
    orch().run()
    assert calls == []


def test_config_change_invalidates_only_dependents(setup):
    import copy
    orch, calls, _ = setup
    orch().run()
    calls.clear()
    cfg = copy.deepcopy(CFG)
    cfg["llm"]["temperature"] = 0.9
    orch(cfg).run()
    assert calls == ["llm"]


def test_missing_output_reruns_and_cascades(setup):
    orch, calls, tmp = setup
    orch().run()
    calls.clear()
    (tmp / "w" / "frames_metadata.json").write_text('[{"x":1}]')  # zmiana wejscia dla vlm
    (tmp / "w" / "frames_metadata.json").unlink()
    orch().run()
    assert calls[0] == "frames" and "vlm" in calls and "llm" in calls and "asr" not in calls


def test_no_audio_skips_asr(setup):
    orch, calls, _ = setup
    o = orch()
    o.has_audio = False
    o.run()
    assert "asr" not in calls and o.state["asr"]["status"] == "skipped"


def test_from_stage(setup):
    orch, calls, _ = setup
    orch().run(from_stage="vlm", force=True)
    assert calls == ["vlm", "llm"]
