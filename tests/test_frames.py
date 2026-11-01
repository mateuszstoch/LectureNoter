import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import frames_module  # noqa: E402

CFG = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config.yaml"))["frames"]
CFG.update(sample_every_s=1, analysis_width=640, min_ink_px=100, save_new_ink_px=60, max_hold_s=1000)


def board(texts):
    img = np.full((400, 640, 3), 40, np.uint8)
    cv2.rectangle(img, (20, 60), (620, 390), (255, 255, 255), -1)
    cv2.rectangle(img, (20, 5), (200, 50), (250, 250, 250), -1)  # jasny pasek "kamery" przy tablicy
    for i, t in enumerate(texts):
        cv2.putText(img, t, (40, 120 + 60 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 0), 3)
    return img


def make_video(path, segments):
    """segments: lista (lista_tekstow, liczba_sekund)."""
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "640x400",
                          "-r", "1", "-i", "-", "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for texts, secs in segments:
        for _ in range(secs):
            p.stdin.write(board(texts).tobytes())
    p.stdin.close()
    p.wait()


def run(tmp_path, segments):
    v = tmp_path / "v.mp4"
    make_video(v, segments)
    meta = frames_module.probe(v)
    states, _ = frames_module.select_states(v, meta, CFG)
    return states


def test_writing_then_new_board_gives_final_states(tmp_path):
    states = run(tmp_path, [
        (["Alfa"], 4), (["Alfa", "Beta"], 4), (["Alfa", "Beta", "Gamma"], 4),  # dopisywanie
        ([], 3),                                                               # wymazanie
        (["Delta"], 4), (["Delta", "Epsilon"], 4),
    ])
    assert len(states) == 2  # tylko koncowe stany obu plansz, bez posrednich
    assert states[0].t < states[1].t


def test_static_board_gives_single_frame(tmp_path):
    assert len(run(tmp_path, [(["Alfa"], 10)])) == 1


def test_blank_board_gives_nothing(tmp_path):
    assert run(tmp_path, [([], 6)]) == []
