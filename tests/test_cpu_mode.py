import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ollama_client as oc  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config.yaml"))


def test_no_gpu_picks_cpu_profile(monkeypatch):
    monkeypatch.setenv("LECTURENOTER_DEVICE", "cpu")
    assert oc.pick_profile(CFG) == "cpu"
    assert oc.pick_profile(CFG, "8gb") == "8gb"      # jawny wybor ma pierwszenstwo


def test_gpu_present_keeps_vram_profiles(monkeypatch):
    monkeypatch.setenv("LECTURENOTER_DEVICE", "cuda")
    monkeypatch.setattr(oc, "total_vram_gb", lambda: 16.0)
    assert oc.pick_profile(CFG) == "16gb"
    monkeypatch.setattr(oc, "total_vram_gb", lambda: 8.0)
    assert oc.pick_profile(CFG) == "8gb"


def test_detects_cpu_without_nvml_and_nvidia_smi(monkeypatch):
    monkeypatch.delenv("LECTURENOTER_DEVICE", raising=False)
    monkeypatch.setattr(oc, "total_vram_gb", lambda: None)
    monkeypatch.setattr(oc.shutil, "which", lambda n: None)
    assert oc.has_cuda() is False
    monkeypatch.setattr(oc.shutil, "which", lambda n: "/usr/bin/nvidia-smi")   # GPU jest, tylko NVML zawodzi
    assert oc.has_cuda() is True


def test_apply_profile_overrides_models_and_device():
    cpu = oc.apply_profile(CFG, "cpu")
    assert cpu["asr"]["device"] == "cpu" and cpu["asr"]["model"] == "small" and cpu["asr"]["compute_type"] == "int8"
    assert cpu["vlm"]["model"] == "qwen2.5vl:3b"
    gpu = oc.apply_profile(CFG, "8gb")
    assert "device" not in gpu["asr"] and gpu["asr"]["model"] == CFG["asr"]["model"] and gpu["vlm"]["model"] == CFG["vlm"]["model"]
    assert CFG["asr"].get("device") is None           # oryginalna konfiguracja nie jest modyfikowana


def test_asr_cpu_never_uses_float16(monkeypatch):
    import types
    import asr_module
    seen = {}

    class FakeModel:
        def __init__(self, name, device, compute_type):
            seen.update(name=name, device=device, compute_type=compute_type)
            raise RuntimeError("stop")
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(asr_module, "extract_audio", lambda v, w: None)
    monkeypatch.setattr(asr_module, "load_wav16k", lambda w: __import__("numpy").zeros(16000, dtype="float32"))
    with pytest.raises(RuntimeError):
        asr_module.run_asr("x.mp4", "/tmp/lecturenoter-cpu-test", {"model": "small", "compute_type": "int8_float16", "device": "cpu",
                                                                  "vad_filter": True, "beam_size": 1})
    assert seen == {"name": "small", "device": "cpu", "compute_type": "int8"}


def test_cpu_profile_flows_into_orchestrator_signature(tmp_path, monkeypatch):
    import main as m
    monkeypatch.setenv("LECTURENOTER_DEVICE", "cpu")
    o = m.PipelineOrchestrator(tmp_path / "v.mp4", tmp_path / "w", CFG, profile="auto")
    assert o.profile == "cpu" and o.cfg["asr"]["model"] == "small"
    g = m.PipelineOrchestrator(tmp_path / "v.mp4", tmp_path / "w2", CFG, profile="8gb")
    assert o.config_sig("asr") != g.config_sig("asr")      # zmiana sprzetu uniewaznia wyniki ASR
