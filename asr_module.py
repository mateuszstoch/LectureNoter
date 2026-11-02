"""Etap 1: ekstrakcja audio i transkrypcja (faster-whisper). Uruchamiany w osobnym podprocesie."""
import gc
import subprocess
from pathlib import Path

from loguru import logger

from io_utils import atomic_write_json, fmt_ts

HALLUCINATIONS = ("dziękuję za uwagę", "napisy stworzone przez", "napisy wykonane przez",
                  "dziękuję za oglądanie", "subskrybuj")


def extract_audio(video, wav):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(video), "-vn", "-ac", "1",
                    "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)


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
    logger.info(f"ASR: ladowanie modelu {cfg['model']} (cuda, {cfg['compute_type']})")
    model = WhisperModel(cfg["model"], device="cuda", compute_type=cfg["compute_type"])
    logger.info("ASR: model zaladowany, start transkrypcji")
    try:
        segments, info = model.transcribe(
            str(wav), language=language, vad_filter=cfg["vad_filter"], beam_size=cfg["beam_size"],
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
    logger.info(f"ASR: gotowe, {len(out)} segmentow")
    atomic_write_json(str(workdir / "transcript.json"), out)
    wav.unlink(missing_ok=True)
    return out
