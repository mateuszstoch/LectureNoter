"""Materialy zrodlowe filmu: transkrypt, klatki z tablicy i ich opisy (VLM), kontekst wokol znacznika czasu."""
import re
from pathlib import Path

from io_utils import read_json

FRAME_RE = re.compile(r"^frame_\d+_t\d+\.jpg$")


def transcript(wd):
    return read_json(Path(wd) / "transcript.json", []) or []


def frames(wd):
    wd = Path(wd)
    meta = read_json(wd / "frames_metadata.json", []) or []
    vision = {v["file"]: v.get("text", "") for v in (read_json(wd / "vision_context.json", []) or [])}
    return [{"file": m["file"], "timestamp": m["timestamp"], "text": vision.get(m["file"])} for m in meta]


def frame_path(wd, name):
    if not FRAME_RE.match(name or ""):
        return None
    p = Path(wd) / "frames" / name
    return p if p.is_file() else None


def context_at(wd, t, window=60):
    """Tablica widoczna w chwili t (pierwszy zapisany stan o znaczniku >= t) i mowa w oknie +/- window s."""
    fr = frames(wd)
    frame = next((f for f in fr if f["timestamp"] >= t), fr[-1] if fr else None)
    segs = [s for s in transcript(wd) if s["end"] >= t - window and s["start"] <= t + window]
    return {"t": t, "frame": frame, "transcript": segs}
