"""Etap 3: opis klatek (tablica/slajd) przez VLM w Ollamie. Checkpoint per klatka."""
import base64
import json
from pathlib import Path

import cv2
from loguru import logger

from io_utils import atomic_write_json, fmt_ts
from ollama_client import OllamaClient

PROMPT = ("Extract all text, mathematical formulas, and code from this slide. "
          "Describe any charts or diagrams concisely. The content is a handwritten or typed whiteboard/slide "
          "in Polish: transcribe it faithfully in Polish, keep the structure (lists, indentation), write formulas "
          "in LaTeX. Do not invent anything; if a fragment is illegible write [nieczytelne]. "
          "If the page is blank, say so briefly.")
MAX_SIDE = 1344


def encode_image(path, max_side=MAX_SIDE):
    img = cv2.imread(str(path))
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return base64.b64encode(buf.tobytes()).decode()


def clean_repetitions(text, max_chars=6000):
    """Ucina zapetlone odpowiedzi (ten sam wiersz powtorzony wielokrotnie) i zbyt dlugie."""
    out, seen = [], {}
    for line in text.splitlines():
        key = line.strip()
        if key:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 3:
                continue
        out.append(line)
    return "\n".join(out)[:max_chars].strip()


def run_vlm(workdir, cfg, client=None):
    workdir = Path(workdir)
    client = client or OllamaClient()
    meta = json.loads((workdir / "frames_metadata.json").read_text(encoding="utf-8"))
    ckpt = workdir / "vision_context.jsonl"
    done = {}
    if ckpt.exists():
        for line in ckpt.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if "error" not in r:
                    done[r["file"]] = r
    todo = [m for m in meta if m["file"] not in done]
    logger.info(f"VLM: {len(meta)} klatek, do zrobienia {len(todo)}")
    if todo:
        client.check()
        client.assert_vram_free()
    try:
        with open(ckpt, "a", encoding="utf-8") as f:
            for i, m in enumerate(todo, 1):
                logger.info(f"VLM {i}/{len(todo)} {m['file']} (t={fmt_ts(m['timestamp'])})")
                rec = {"file": m["file"], "timestamp": m["timestamp"]}
                try:
                    text = client.generate(
                        cfg["model"], PROMPT, images=[encode_image(workdir / "frames" / m["file"])],
                        options={"temperature": 0, "num_predict": cfg["num_predict"]},
                        keep_alive=cfg["keep_alive"])
                    rec["text"] = clean_repetitions(text)
                    done[m["file"]] = rec
                except Exception as e:  # jedna klatka nie zatrzymuje etapu
                    logger.error(f"VLM blad dla {m['file']}: {e}")
                    rec["error"] = str(e)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
    finally:
        if todo:
            try:
                client.unload(cfg["model"])
            except Exception as e:
                logger.error(f"nie udalo sie rozladowac {cfg['model']}: {e}")
    result = [done[m["file"]] for m in meta if m["file"] in done]
    failed = len(meta) - len(result)
    if failed:
        logger.warning(f"VLM: {failed} klatek bez opisu (uruchom ponownie, aby ponowic)")
    atomic_write_json(str(workdir / "vision_context.json"), result)
    return result
