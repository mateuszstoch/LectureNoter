import subprocess
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import asr_module  # noqa: E402


def write_wav(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def test_load_wav16k_scales_to_float32(tmp_path):
    write_wav(tmp_path / "a.wav", [0, 16384, -32768, 32767])
    a = asr_module.load_wav16k(tmp_path / "a.wav")
    assert a.dtype == np.float32 and a.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768], abs=1e-6)


def test_load_wav16k_rejects_wrong_format_and_empty(tmp_path):
    write_wav(tmp_path / "stereo.wav", [0, 0], channels=2)
    with pytest.raises(ValueError, match="oczekiwano WAV 16 kHz mono"):
        asr_module.load_wav16k(tmp_path / "stereo.wav")
    write_wav(tmp_path / "cisza.wav", [])
    with pytest.raises(ValueError, match="pusta"):
        asr_module.load_wav16k(tmp_path / "cisza.wav")


def test_run_asr_passes_array_not_path_so_pyav_is_not_used(tmp_path, monkeypatch):
    """Regresja: stare PyAV nie zna av.open(metadata_errors=...), a faster-whisper uzywa go przy podaniu sciezki."""
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", "-shortest", "-pix_fmt", "yuv420p", str(video)], check=True)
    seen = {}

    class Seg:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class FakeModel:
        def __init__(self, name, device, compute_type):
            seen["init"] = (name, device, compute_type)

        def transcribe(self, audio, **kw):
            seen["audio"], seen["kw"] = audio, kw
            return iter([Seg(0.0, 1.0, " Dzien dobry "), Seg(1.0, 2.0, "Dziękuję za uwagę")]), types.SimpleNamespace(duration=2.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    wd = tmp_path / "w"
    wd.mkdir()
    out = asr_module.run_asr(video, wd, {"model": "m", "compute_type": "int8", "vad_filter": True, "beam_size": 5}, "pl")
    assert isinstance(seen["audio"], np.ndarray) and seen["audio"].dtype == np.float32
    assert 1.9 * 16000 < len(seen["audio"]) < 2.1 * 16000 and float(np.abs(seen["audio"]).max()) > 0.1
    assert seen["kw"]["language"] == "pl" and seen["kw"]["condition_on_previous_text"] is False
    assert out == [{"start": 0.0, "end": 1.0, "text": "Dzien dobry"}]  # halucynacja z ciszy odfiltrowana
    assert not (wd / "audio.wav").exists() and (wd / "transcript.json").exists()
