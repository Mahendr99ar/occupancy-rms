"""Reads prices that a booking site draws as images.

Classic pipeline, chosen because the images are small and regular:
  1. threshold the image (Otsu) so ink is 1 and paper is 0,
  2. cut it into characters at empty columns,
  3. describe each character as a 16x16 bitmap plus its height and
     vertical position (a comma is short and sits low; a 7 is tall),
  4. classify with an RBF support vector machine.

A k-nearest-neighbours classifier on the same features is the baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import joblib
import numpy as np
from PIL import Image
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from ..ota import price_image as pim

SIDE = 16


def otsu(arr: np.ndarray) -> int:
    hist = np.bincount(arr.ravel(), minlength=256).astype(float)
    total = arr.size
    cum = np.cumsum(hist)
    cum_mean = np.cumsum(hist * np.arange(256))
    best, thresh = -1.0, 128
    for t in range(1, 255):
        w0, w1 = cum[t], total - cum[t]
        if w0 == 0 or w1 == 0:
            continue
        m0 = cum_mean[t] / w0
        m1 = (cum_mean[-1] - cum_mean[t]) / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best:
            best, thresh = between, t
    return thresh


def segment(img: Image.Image) -> list[tuple[np.ndarray, float, float]]:
    """Characters left to right, each as (ink bitmap, height share, centre
    height share)."""
    arr = np.asarray(img.convert("L"), dtype=np.uint8)
    ink = (arr < otsu(arr)).astype(np.uint8)
    rows = np.where(ink.any(axis=1))[0]
    if rows.size == 0:
        return []
    top, bottom = rows[0], rows[-1] + 1
    text_h = bottom - top
    cols = ink.any(axis=0)
    chars, start = [], None
    for x, on in enumerate([*cols, False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            piece = ink[:, start:x]
            r = np.where(piece.any(axis=1))[0]
            if piece.sum() >= 3:  # drop isolated noise pixels
                glyph = piece[r[0] : r[-1] + 1]
                centre = ((r[0] + r[-1]) / 2 - top) / text_h
                chars.append((glyph, (r[-1] - r[0] + 1) / text_h, centre))
            start = None
    return chars


def features(glyph: np.ndarray, height: float, centre: float) -> np.ndarray:
    h, w = glyph.shape
    size = max(h, w)
    canvas = np.zeros((size, size), dtype=np.uint8)
    canvas[(size - h) // 2 : (size - h) // 2 + h, (size - w) // 2 : (size - w) // 2 + w] = glyph * 255
    small = np.asarray(Image.fromarray(canvas).resize((SIDE, SIDE), Image.BILINEAR), dtype=np.float32) / 255
    return np.concatenate([small.ravel(), [height, centre, w / max(h, 1)]])


def dataset(n: int, seed: int, fonts) -> tuple[np.ndarray, np.ndarray, int]:
    """Features and labels for `n` rendered prices. Images whose character
    count does not match the text (a segmentation miss) are skipped and
    counted."""
    r = np.random.default_rng(seed)
    xs, ys, skipped = [], [], 0
    for _ in range(n):
        text = pim.price_text(int(r.integers(10, 400)) * 50)
        chars = segment(pim.render(text, r, str(r.choice(fonts))))
        if len(chars) != len(text):
            skipped += 1
            continue
        for (g, h, c), label in zip(chars, text, strict=True):
            xs.append(features(g, h, c))
            ys.append(label)
    return np.array(xs), np.array(ys), skipped


@dataclass
class PriceReader:
    model: object

    @classmethod
    def train(cls, n: int = 3000, seed: int = 1, kind: str = "svm") -> PriceReader:
        x, y, _ = dataset(n, seed, pim.TRAIN_FONTS)
        clf = SVC(C=10, gamma="scale") if kind == "svm" else KNeighborsClassifier(n_neighbors=3)
        return cls(make_pipeline(StandardScaler(), clf).fit(x, y))

    def read(self, image: Image.Image | bytes) -> int | None:
        if isinstance(image, bytes):
            image = Image.open(BytesIO(image))
        chars = segment(image)
        if not chars:
            return None
        labels = self.model.predict(np.array([features(*c) for c in chars]))
        text = "".join(labels)
        digits = text.replace("₹", "").replace(",", "")
        if not text.startswith("₹") or not digits.isdigit():
            return None
        return int(digits)

    def save(self, path: str | Path) -> None:
        joblib.dump(self.model, path)

    @classmethod
    def load(cls, path: str | Path) -> PriceReader:
        return cls(joblib.load(path))


def evaluate(reader: PriceReader, n: int = 500, seed: int = 99, fonts=pim.ALL_FONTS) -> dict:
    """Whole-price accuracy on fresh renders."""
    r = np.random.default_rng(seed)
    right = 0
    for _ in range(n):
        rate = int(r.integers(10, 400)) * 50
        img = pim.render(pim.price_text(rate), r, str(r.choice(fonts)))
        right += reader.read(img) == rate
    return {"n": n, "exact": right / n}


def main() -> None:
    """Trains the SVM and a kNN baseline on the training fonts, scores both
    on the training fonts and on a font neither has seen, and saves the SVM."""
    import json

    from .. import config

    out = {}
    for kind in ("knn", "svm"):
        reader = PriceReader.train(kind=kind)
        out[kind] = {
            "training_fonts": evaluate(reader, fonts=pim.TRAIN_FONTS)["exact"],
            "held_out_font": evaluate(reader, fonts=(pim.HELD_OUT_FONT,))["exact"],
        }
    reader.save(Path(config.MODEL_DIR) / "price_reader.joblib")
    (Path(config.MODEL_DIR) / "price_reader_metrics.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
