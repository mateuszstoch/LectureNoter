"""Etap 2: ekstrakcja stanow tablicy/slajdow z nagrania ekranu (ffmpeg + OpenCV)."""
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml
from loguru import logger

from io_utils import atomic_write_json, fmt_ts


def probe(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)  # brak = nagranie tylko audio
    return {
        "width": int(v["width"]) if v else 0, "height": int(v["height"]) if v else 0,
        "duration": float(info["format"]["duration"]),
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
        "has_video": v is not None,
    }


def iter_gray_samples(video, every_s, width, height):
    """Generator (index, gray_frame) z ffmpeg; analiza na zmniejszonych klatkach."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video),
           "-vf", f"fps=1/{every_s},scale={width}:{height}",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = width * height
    i = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield i, np.frombuffer(buf, np.uint8).reshape(height, width)
            i += 1
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        p.wait()


def find_board(gray, cfg):
    """Zwraca bbox (x, y, w, h) najwiekszego jasnego prostokata lub None."""
    mask = (gray > cfg["board_white_level"]).astype(np.uint8)
    # otwarcie odcina waskie mostki (np. jasne tlo kamery przy tablicy); pismo nie rozlacza tablicy
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    x, y, w, h = cv2.boundingRect(max(cnts, key=cv2.contourArea))
    if w * h < cfg["min_board_area"] * gray.shape[0] * gray.shape[1]:
        return None
    return x, y, w, h


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter)


def ink_mask(gray, bbox, cfg):
    """Maska tuszu w obrebie tablicy, w ukladzie wspolrzednych calego kadru analizy."""
    x, y, w, h = bbox
    full = np.zeros(gray.shape, bool)
    crop = cv2.GaussianBlur(gray[y:y + h, x:x + w], (3, 3), 0)
    m = (crop < cfg["ink_level"]).astype(np.uint8)
    # duze wypelnione bryly (paski narzedzi, popupy, kontrolki) to nie pismo
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    big = stats[:, cv2.CC_STAT_AREA] > cfg["max_ink_blob_frac"] * gray.shape[0] * gray.shape[1]
    big[0] = False
    m[big[lab]] = 0
    full[y:y + h, x:x + w] = m.astype(bool)
    return full


class Sample:
    __slots__ = ("idx", "t", "bbox", "ink")

    def __init__(self, idx, t, bbox, ink):
        self.idx, self.t, self.bbox, self.ink = idx, t, bbox, ink


def select_states(video, meta, cfg, progress=None):
    """Zwraca liste Sample do zapisania (stan tablicy tuz przed wymazaniem/zmiana)."""
    every = cfg["sample_every_s"]
    aw = cfg["analysis_width"]
    ah = int(round(meta["height"] * aw / meta["width"] / 2)) * 2
    saved, prev, last_saved = [], None, None

    def new_ink(s):
        if last_saved is None:
            return int(s.ink.sum())
        # nowy tusz wzgledem ostatnio zapisanego stanu (poszerzenie 1 px tolerancja)
        base = cv2.dilate(last_saved.ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        return int((s.ink & ~base).sum())

    def maybe_save(s, reason):
        nonlocal last_saved
        if s is None or int(s.ink.sum()) < cfg["min_ink_px"]:
            return
        if last_saved is not None and last_saved.bbox and iou(last_saved.bbox, s.bbox) >= cfg["layout_iou"] \
                and new_ink(s) < cfg["save_new_ink_px"]:
            return
        logger.info(f"zapis stanu t={fmt_ts(s.t)} ({reason}), tusz={int(s.ink.sum())}px")
        saved.append(s)
        last_saved = s

    n = 0
    total = int(meta["duration"] / every) + 1
    for idx, gray in iter_gray_samples(video, every, aw, ah):
        n += 1
        if progress:
            progress(n, total, "analiza obrazu")
        t = idx * every
        bbox = find_board(gray, cfg)
        cur = Sample(idx, t, bbox, ink_mask(gray, bbox, cfg)) if bbox else None
        if prev is not None:
            boundary = False
            if cur is None:
                boundary = True
            elif iou(prev.bbox, cur.bbox) < cfg["layout_iou"]:
                boundary = True
            else:
                p_ink = int(prev.ink.sum())
                kept = int((prev.ink & cur.ink).sum())
                if p_ink >= cfg["min_ink_px"] and (p_ink - kept) > cfg["erase_fraction"] * p_ink:
                    boundary = True
            if boundary:
                maybe_save(prev, "granica")
        if cur is not None and last_saved is not None and t - last_saved.t > cfg["max_hold_s"]:
            maybe_save(cur, "max_hold")
        prev = cur
    maybe_save(prev, "koniec")
    saved = drop_incomplete(saved, cfg)
    logger.info(f"przeanalizowano {n} probek, wybrano {len(saved)} stanow")
    return saved, ah


def drop_incomplete(states, cfg):
    """Usuwa stan, ktorego tusz jest (prawie) w calosci zawarty w kolejnym stanie (wczesniejsza wersja)."""
    keep = []
    for i, a in enumerate(states):
        if i + 1 < len(states):
            b = states[i + 1]
            bd = cv2.dilate(b.ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
            a_px = int(a.ink.sum())
            if a_px and (a.ink & bd).sum() / a_px >= cfg["subset_ratio"] and b.ink.sum() >= a_px:
                logger.info(f"pomijam niepelny stan t={fmt_ts(a.t)} (zawarty w t={fmt_ts(b.t)})")
                continue
        keep.append(a)
    return keep


def grab_fullres(video, t):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", str(video),
         "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True, check=True).stdout
    return cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)


def extract_frames(video, out_dir, cfg, progress=None):
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    meta = probe(video)
    logger.info(f"wideo {meta['width']}x{meta['height']} {meta['duration']:.0f}s audio={meta['has_audio']}")
    states, ah = select_states(video, meta, cfg, progress)
    if len(states) > cfg["max_frames"]:
        logger.warning(f"{len(states)} klatek > max_frames, obcinam")
        states = states[:cfg["max_frames"]]
    scale = meta["width"] / cfg["analysis_width"]
    out = []
    for k, s in enumerate(states, 1):
        if progress:
            progress(k - 1, len(states), "zapis klatek")
        img = grab_fullres(video, s.t)
        x, y, w, h = s.bbox
        m = 8
        x0, y0 = max(0, int((x - m) * scale)), max(0, int((y - m) * scale))
        x1, y1 = int((x + w + m) * scale), int((y + h + m) * scale)
        crop = img[y0:y1, x0:x1]
        name = f"frame_{s.idx:06d}_t{int(s.t):05d}.jpg"
        cv2.imwrite(str(frames_dir / name), crop, [cv2.IMWRITE_JPEG_QUALITY, cfg["jpeg_quality"]])
        out.append({"file": name, "timestamp": float(s.t), "index": s.idx})
    if progress:
        progress(len(states), max(len(states), 1), "zapis klatek")
    atomic_write_json(str(out_dir / "frames_metadata.json"), out)
    return out


if __name__ == "__main__":
    cfg = yaml.safe_load(open(Path(__file__).parent / "config.yaml"))["frames"]
    res = extract_frames(sys.argv[1], sys.argv[2], cfg)
    print(json.dumps(res, indent=1))
