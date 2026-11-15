"""Prices drawn as images, the way some booking sites hide them from scrapers.

The RoomBazaar mock site serves every price as a small PNG. The same drawing
code generates training images for the price reader, but the fonts are split:
the reader never trains on HELD_OUT_FONT, and the site uses all of them, so
the measured accuracy includes a font the model has not seen.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
TRAIN_FONTS = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSerif.ttf", "DejaVuSansMono.ttf")
HELD_OUT_FONT = "DejaVuSansCondensed.ttf"
ALL_FONTS = (*TRAIN_FONTS, HELD_OUT_FONT)
CHARSET = "0123456789,₹"


def price_text(rate: int) -> str:
    # Indian digit grouping: 1,25,000 rather than 125,000.
    s = str(rate)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return "₹" + s


def render(text: str, r: np.random.Generator, font_name: str | None = None) -> Image.Image:
    """Draws `text` one character at a time with small random offsets,
    a slight rotation, blur and noise: enough variation that a fixed
    template match fails, as on sites that do this to slow scrapers."""
    font_name = font_name or str(r.choice(ALL_FONTS))
    size = int(r.integers(13, 27))
    font = ImageFont.truetype(str(FONT_DIR / font_name), size)
    pad_x, pad_y = int(r.integers(3, 10)), int(r.integers(3, 8))
    gap = int(r.integers(1, 3))
    w = int(sum(font.getlength(c) for c in text)) + gap * len(text) + 2 * pad_x + 4
    h = size + 2 * pad_y + 6
    bg = int(r.integers(200, 256))
    img = Image.new("L", (w, h), bg)
    draw = ImageDraw.Draw(img)
    x = pad_x
    ink = int(r.integers(0, 90))
    for c in text:
        draw.text((x, pad_y + int(r.integers(-2, 3))), c, font=font, fill=ink)
        x += font.getlength(c) + gap
    img = img.rotate(float(r.uniform(-2.5, 2.5)), resample=Image.BILINEAR, fillcolor=bg, expand=False)
    arr = np.asarray(img, dtype=np.float32)
    arr += r.normal(0, r.uniform(4, 16), arr.shape)  # sensor-like noise
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    if r.random() < 0.5:
        img = img.filter(ImageFilter.GaussianBlur(r.uniform(0.3, 0.8)))
    return img


def png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
