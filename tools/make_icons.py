"""Generuje ikony PWA (ui/static/icons). Uruchom: python tools/make_icons.py"""
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "ui" / "static" / "icons"
BLUE, WHITE, LINE = (235, 99, 37), (255, 255, 255), (235, 99, 37)  # BGR


def icon(size, maskable=False):
    img = np.zeros((size, size, 4), np.uint8)
    mask = np.zeros((size, size), np.uint8)
    r = 0 if maskable else int(size * 0.22)
    cv2.rectangle(mask, (r, 0), (size - r, size), 255, -1)
    cv2.rectangle(mask, (0, r), (size, size - r), 255, -1)
    for cx, cy in ((r, r), (size - r, r), (r, size - r), (size - r, size - r)):
        cv2.circle(mask, (cx, cy), r, 255, -1)
    img[mask > 0] = (*BLUE, 255)
    pad = int(size * (0.30 if maskable else 0.24))
    x0, y0, x1, y1 = pad, pad - int(size * 0.02), size - pad, size - pad + int(size * 0.02)
    cv2.rectangle(img, (x0, y0), (x1, y1), (*WHITE, 255), -1)
    for i, frac in enumerate((0.28, 0.5, 0.72)):
        y = int(y0 + (y1 - y0) * frac)
        w = (x1 - x0) * (0.62 if i < 2 else 0.4)
        cv2.line(img, (x0 + int((x1 - x0) * 0.16), y), (x0 + int((x1 - x0) * 0.16 + w), y),
                 (*LINE, 255), max(2, size // 40), cv2.LINE_AA)
    return img


OUT.mkdir(parents=True, exist_ok=True)
for name, size, mk in (("icon-192.png", 192, False), ("icon-512.png", 512, False),
                       ("icon-maskable-512.png", 512, True)):
    cv2.imwrite(str(OUT / name), icon(size, mk))
print("ok")
