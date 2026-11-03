"""Import napisow (.srt / .vtt) jako transkryptu: segmenty {start, end, text} w formacie transcript.json."""
import re

TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")
MAX_BYTES = 8 * 1024 * 1024


class SubtitleError(ValueError):
    pass


def _sec(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def decode(raw):
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SubtitleError("Nie udało się odczytać kodowania pliku z napisami.")


def parse(text):
    """Zwraca liste segmentow; powtorzone kolejno identyczne wiersze (typowe w napisach z YouTube) sa scalane."""
    if len(text) > MAX_BYTES:
        raise SubtitleError("Plik z napisami jest za duży.")
    segs, cur = [], None
    for line in text.replace("\r", "").split("\n"):
        m = TS.search(line)
        if m:
            if cur and cur["text"]:
                segs.append(cur)
            g = m.groups()
            cur = {"start": round(_sec(*g[:4]), 2), "end": round(_sec(*g[4:]), 2), "text": ""}
            continue
        if cur is None:
            continue
        t = TAG.sub("", line).strip()
        if not t or t.isdigit() or t.startswith(("NOTE", "STYLE", "WEBVTT")):
            if not t:
                segs.append(cur) if cur["text"] else None
                cur = None
            continue
        cur["text"] = (cur["text"] + " " + t).strip()
    if cur and cur["text"]:
        segs.append(cur)
    out = []
    for s in segs:
        if out and out[-1]["text"] == s["text"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(s)
    if not out:
        raise SubtitleError("W pliku nie znaleziono napisów (oczekiwano formatu .srt lub .vtt).")
    return out
