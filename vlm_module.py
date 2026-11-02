"""Etap 3: opis klatek (tablica/slajd) przez VLM w Ollamie. Checkpoint per klatka."""
import base64
import json
import time
from pathlib import Path

import cv2
from loguru import logger

from io_utils import atomic_write_json, fmt_ts
from ollama_client import OllamaClient, OllamaError
from textguard import collapse_loops

PROMPT = ("Extract all text, mathematical formulas, and code from this slide. "
          "Describe any charts or diagrams concisely. The content is a handwritten or typed whiteboard/slide "
          "in Polish: transcribe it faithfully in Polish, keep the structure (lists, indentation), write formulas "
          "in LaTeX. Do not invent anything; if a fragment is illegible write [nieczytelne]. "
          "Ignore application UI (toolbars, mode labels such as 'Tryb precyzyjny', participant names, player controls). "
          "If the page is blank, say so briefly.")
MAX_SIDE = 1344
SHRINK = (1.0, 0.75, 0.55)  # przy ponowieniach obraz jest coraz mniejszy (mniej tokenow wizyjnych, mniej VRAM)
_sleep = time.sleep         # podmieniane w testach


def encode_image(path, max_side=MAX_SIDE):
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f"nie da sie odczytac obrazu {Path(path).name} (uszkodzony plik?)")
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return base64.b64encode(buf.tobytes()).decode()


def clean_repetitions(text, max_chars=6000):
    """Ucina zapetlone odpowiedzi (ten sam wiersz lub slowo/fraza powtorzone wielokrotnie) i zbyt dlugie."""
    text = collapse_loops(text)
    out, seen = [], {}
    for line in text.splitlines():
        key = line.strip()
        if key:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 3:
                continue
        out.append(line)
    return "\n".join(out)[:max_chars].strip()


def _is_fatal(e):
    return isinstance(e, OllamaError) and (getattr(e, "fatal", False) or "niedostepn" in str(e))


def recycle_model(client, model, wait=5.0):
    """Po awarii runnera lub profilaktycznie: rozladuj model i poczekaj, az Ollama zaladuje go od nowa. Pomaga na
    narastajaca pamiec VRAM po wielu obrazach i na zawieszony proces modelu."""
    try:
        client.unload(model)
    except Exception as e:
        logger.warning(f"VLM: nie udalo sie rozladowac {model}: {e}")
    _sleep(wait)


def describe_frame(client, cfg, path):
    """Opis jednej klatki. Przy bledzie Ollamy (np. HTTP 500 po padnieciu runnera) odtwarza model i ponawia z mniejszym obrazem."""
    attempts = cfg.get("frame_retries", 2) + 1
    base = cfg.get("max_side", MAX_SIDE)
    last = None
    for attempt in range(attempts):
        side = int(base * SHRINK[min(attempt, len(SHRINK) - 1)])
        try:
            text = client.generate(
                cfg["model"], PROMPT, images=[encode_image(path, side)],
                options={"temperature": 0, "num_predict": cfg["num_predict"], "num_ctx": cfg.get("num_ctx", 4096)},
                keep_alive=cfg["keep_alive"])
            if attempt:
                logger.info(f"VLM: {Path(path).name} opisana po ponowieniu {attempt} (obraz {side}px)")
            return clean_repetitions(text)
        except OllamaError as e:
            if _is_fatal(e):
                raise
            last = e
            nxt = int(base * SHRINK[min(attempt + 1, len(SHRINK) - 1)])
            logger.warning(f"VLM {Path(path).name}: {e} (proba {attempt + 1}/{attempts})"
                           + (f"; odtwarzam model i zmniejszam obraz do {nxt}px" if attempt + 1 < attempts else ""))
            if attempt + 1 < attempts:
                recycle_model(client, cfg["model"])
    raise last


def run_vlm(workdir, cfg, client=None, progress=None):
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
    errors = {}
    logger.info(f"VLM: {len(meta)} klatek, do zrobienia {len(todo)}")
    if progress:
        progress.update(len(done), len(meta), "opis klatek")
    if todo:
        client.check()
        client.assert_vram_free()
    reload_every, max_consecutive = cfg.get("reload_every", 25), cfg.get("max_consecutive_failures", 5)
    since_reload = consecutive = 0
    try:
        with open(ckpt, "a", encoding="utf-8") as f:
            for i, m in enumerate(todo, 1):
                if reload_every and since_reload >= reload_every:
                    logger.info(f"VLM: odswiezam model po {since_reload} klatkach (profilaktyka przed wyciekiem pamieci)")
                    recycle_model(client, cfg["model"], wait=2.0)
                    since_reload = 0
                logger.info(f"VLM {i}/{len(todo)} {m['file']} (t={fmt_ts(m['timestamp'])})")
                rec = {"file": m["file"], "timestamp": m["timestamp"]}
                try:
                    rec["text"] = describe_frame(client, cfg, workdir / "frames" / m["file"])
                    done[m["file"]] = rec
                    errors.pop(m["file"], None)
                    consecutive = 0
                except OllamaError as e:
                    if _is_fatal(e):
                        raise  # brak modelu albo Ollamy: dalsze klatki tez padna
                    rec["error"] = errors[m["file"]] = str(e)
                    consecutive += 1
                except Exception as e:  # np. uszkodzony plik obrazu: pomijamy te klatke
                    logger.error(f"VLM blad dla {m['file']}: {e}")
                    rec["error"] = errors[m["file"]] = str(e)
                    consecutive += 1
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                since_reload += 1
                if progress:
                    progress.update(len(done), len(meta), "opis klatek")
                if consecutive >= max_consecutive:
                    raise RuntimeError(f"VLM: {consecutive} kolejnych klatek zakonczylo sie bledem (ostatni: {rec['error']}). "
                                       "Ollama jest niestabilna: sprawdz jej log; gotowe klatki zostaja, po naprawie uruchom ponownie.")
    finally:
        if todo:
            try:
                client.unload(cfg["model"])
            except Exception as e:
                logger.error(f"nie udalo sie rozladowac {cfg['model']}: {e}")
    failed = [m for m in meta if m["file"] not in done]
    allowed = min(cfg.get("max_failed_frames", 3), len(meta) // 5)  # przy krotkich wykladach zadnych brakow
    if len(failed) > allowed:  # nie oznaczamy etapu jako gotowego: gotowe klatki zostaja w checkpoincie
        last_error = next(iter(reversed(list(errors.values()))), None)
        raise RuntimeError(f"VLM: {len(failed)} z {len(meta)} klatek bez opisu (ostatni blad: {last_error}). "
                           "Uruchom ponownie: gotowe klatki zostana pominiete.")
    if failed:
        logger.warning(f"VLM: pomijam {len(failed)} klatek, ktorych nie udalo sie opisac mimo ponowien: "
                       + ", ".join(m["file"] for m in failed))
    result = [done[m["file"]] if m["file"] in done else
              {"file": m["file"], "timestamp": m["timestamp"], "text": "", "error": errors.get(m["file"], "nieznany blad"), "skipped": True}
              for m in meta]
    atomic_write_json(str(workdir / "vision_context.json"), result)
    return result
