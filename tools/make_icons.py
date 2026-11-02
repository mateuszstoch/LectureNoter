"""Generuje ikony PWA (ui/static/icons) w stylu aplikacji: kartka z czerwonym marginesem na tle koloru tuszu.
Uruchom: python tools/make_icons.py"""
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "ui" / "static" / "icons"
INK, PAPER, RULE, RED = (21, 26, 28), (227, 238, 243), (187, 207, 216), (43, 53, 163)  # BGR: #1c1a15, #f3eee3, ...


def icon(size, maskable=False):
    img = np.zeros((size, size, 4), np.uint8)
    img[:, :] = (*INK, 255)
    if not maskable:  # ikona "any": zaokraglone rogi (przezroczyste narozniki)
        mask = np.zeros((size, size), np.uint8)
        r = int(size * 0.18)
        cv2.rectangle(mask, (r, 0), (size - r, size), 255, -1)
        cv2.rectangle(mask, (0, r), (size, size - r), 255, -1)
        for cx, cy in ((r, r), (size - r, r), (r, size - r), (size - r, size - r)):
            cv2.circle(mask, (cx, cy), r, 255, -1)
        img[mask == 0] = 0
    pad = int(size * (0.27 if maskable else 0.2))
    x0, y0, x1, y1 = pad, pad, size - pad, size - pad
    cv2.rectangle(img, (x0, y0), (x1, y1), (*PAPER, 255), -1)
    mx = x0 + int((x1 - x0) * 0.2)  # czerwony margines
    w = max(2, size // 48)
    cv2.rectangle(img, (mx - w // 2, y0), (mx + w // 2, y1), (*RED, 255), -1)
    for frac in (0.3, 0.45, 0.6, 0.75):  # linie zeszytu
        y = int(y0 + (y1 - y0) * frac)
        cv2.line(img, (mx + size // 24, y), (x1 - size // 24, y), (*RULE, 255), max(1, size // 96), cv2.LINE_AA)
    return img


OUT.mkdir(parents=True, exist_ok=True)
for name, size, mk in (("icon-192.png", 192, False), ("icon-512.png", 512, False), ("icon-maskable-512.png", 512, True)):
    cv2.imwrite(str(OUT / name), icon(size, mk))
print("ok")
