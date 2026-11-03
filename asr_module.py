"""Etap 1: ekstrakcja audio i transkrypcja (faster-whisper). Uruchamiany w osobnym podprocesie."""
import gc
import subprocess
import wave
from pathlib import Path

import numpy as np
from loguru import logger

from io_utils import atomic_write_json, fmt_ts
from textguard import collapse_loops

HALLUCINATIONS = ("dziękuję za uwagę", "napisy stworzone przez", "napisy wykonane przez",
                  "dziękuję za oglądanie", "subskrybuj")


def extract_audio(video, wav):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(video), "-vn", "-ac", "1",
                    "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)


def load_wav16k(path):
    """Wczytuje WAV 16 kHz mono 16-bit (wynik extract_audio) jako float32 w [-1, 1].

    Podajemy modelowi gotowa tablice zamiast sciezki, wiec faster-whisper nie uzywa wlasnego dekodera (PyAV):
    stare wersje `av` nie maja parametru `metadata_errors` w av.open() i wywalaja sie przy czytaniu pliku."""
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f"oczekiwano WAV 16 kHz mono 16-bit, jest: {w.getframerate()} Hz, "
                             f"{w.getnchannels()} kan., {8 * w.getsampwidth()} bit")
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size == 0:
        raise ValueError("sciezka audio jest pusta (film bez dzwieku?)")
    return audio


def drop_repeated_segments(segments, max_run=2):
    """Whisper na ciszy lub szumie potrafi powtarzac ten sam segment dziesiatki razy: zostawiamy najwyzej max_run
    kolejnych identycznych segmentow i czyscimy petle slow wewnatrz segmentu."""
    out, run, prev = [], 0, None
    for s in segments:
        s = dict(s, text=collapse_loops(s["text"]).strip())
        key = " ".join(s["text"].lower().split())
        run = run + 1 if key == prev else 1
        prev = key
        if s["text"] and run <= max_run:
            out.append(s)
    return out


def is_hallucination(text):
    t = text.strip().lower()
    return not t or any(t.startswith(h) and len(t) < 60 for h in HALLUCINATIONS)


def run_asr(video, workdir, cfg, language="pl", progress=None):
    from faster_whisper import WhisperModel
    workdir = Path(workdir)
    wav = workdir / "audio.wav"
    if progress:
        progress.update(0, 1, "ekstrakcja audio")
    logger.info(f"ASR: ekstrakcja audio z {Path(video).name}")
    extract_audio(video, wav)
    audio = load_wav16k(wav)
    logger.info(f"ASR: audio {len(audio) / 16000:.0f} s ({audio.nbytes / 1e6:.0f} MB w pamieci)")
    device = cfg.get("device", "cuda")
    compute_type = cfg["compute_type"] if device == "cuda" else ("int8" if "float16" in cfg["compute_type"] else cfg["compute_type"])
    logger.info(f"ASR: ladowanie modelu {cfg['model']} ({device}, {compute_type})")
    model = WhisperModel(cfg["model"], device=device, compute_type=compute_type)
    logger.info("ASR: model zaladowany, start transkrypcji")
    try:
        segments, info = model.transcribe(
            audio, language=language, vad_filter=cfg["vad_filter"], beam_size=cfg["beam_size"],
            condition_on_previous_text=False)
        out, next_log = [], 60
        for s in segments:
            if not is_hallucination(s.text):
                out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
            if progress:
                progress.update(s.end, info.duration, "transkrypcja")
            if s.end >= next_log:
                logger.info(f"ASR: {fmt_ts(s.end)} / {fmt_ts(info.duration)}")
                next_log = s.end + 300
    finally:
        del model
        gc.collect()
    n_raw = len(out)
    out = drop_repeated_segments(out)
    if len(out) < n_raw:
        logger.warning(f"ASR: usunieto {n_raw - len(out)} powtorzonych segmentow (zapetlenie modelu)")
    logger.info(f"ASR: gotowe, {len(out)} segmentow")
    atomic_write_json(str(workdir / "transcript.json"), out)
    wav.unlink(missing_ok=True)
    return out
