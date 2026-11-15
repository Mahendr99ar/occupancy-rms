"""A small language model, trained from scratch, that reads corporate rate
requests and writes out their fields.

It is a character-level, decoder-only transformer (653,632 parameters).
Training text is the message followed by its answer:

    <message> \x02 company=Acme Pharma;room=Deluxe;in=14-12;out=16-12;rate=7450;meal=CP;rooms=3 \x03

and the loss counts only the answer, so the model learns to read, not to
write messages. At inference it is given the message and the separator and
decodes with the field names written in and each value limited to
characters that can form a legal value for that field.

Why train one instead of calling a large model: the task is narrow, the
inputs are short, and a model this size answers in about 165 ms on a 2-vCPU
machine with no per-call cost and no data leaving the hotel. The large model is
still used where open-ended language is needed (rate change explanations).

    python -m occupancy.ml.slm train   # writes models/slm.pt
    python -m occupancy.ml.slm eval
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from .. import config
from ..market import messages

SEP, END = "\x02", "\x03"
FIELDS = (
    ("company", "company"),
    ("room", "room_type"),
    ("in", "check_in"),
    ("out", "check_out"),
    ("rate", "rate"),
    ("meal", "meal_plan"),
    ("rooms", "rooms"),
)
MODEL_PATH = Path(config.MODEL_DIR) / "slm.pt"


def answer(truth: dict) -> str:
    def dm(iso: str) -> str:
        d = date.fromisoformat(iso)
        return f"{d.day}-{d.month}"

    t = {**truth, "check_in": dm(truth["check_in"]), "check_out": dm(truth["check_out"])}
    return ";".join(f"{short}={t[full]}" for short, full in FIELDS)


def parse_answer(s: str) -> dict | None:
    out = {}
    for part in s.split(";"):
        if "=" not in part:
            return None
        k, v = part.split("=", 1)
        full = dict(FIELDS).get(k)
        if full is None:
            return None
        out[full] = int(v) if full in ("rate", "rooms") and v.isdigit() else v
    return out if len(out) == len(FIELDS) else None


def samples(n: int, seed: int, templates, noise: float | None = 0.0) -> list[tuple[str, dict]]:
    """`noise=None` draws a typo rate per message between 0 and 8%, which
    is what training uses; tests use a fixed rate."""
    r = np.random.default_rng(seed)
    base = date(2026, 1, 1)
    out = []
    for _ in range(n):
        today = base + timedelta(days=int(r.integers(0, 365)))
        p = float(r.uniform(0, 0.08)) if noise is None else noise
        out.append(messages.make(r, today, templates, noise=p))
    return out


@dataclass
class Cfg:
    vocab: str
    block: int = 256
    d: int = 112
    layers: int = 4
    heads: int = 4
    dropout: float = 0.05


class Block(nn.Module):
    def __init__(self, c: Cfg):
        super().__init__()
        self.ln1, self.ln2 = nn.LayerNorm(c.d), nn.LayerNorm(c.d)
        self.attn = nn.MultiheadAttention(c.d, c.heads, dropout=c.dropout, batch_first=True)
        self.mlp = nn.Sequential(nn.Linear(c.d, 4 * c.d), nn.GELU(), nn.Linear(4 * c.d, c.d), nn.Dropout(c.dropout))

    def forward(self, x, mask):
        h = self.ln1(x)
        x = x + self.attn(h, h, h, attn_mask=mask, need_weights=False)[0]
        return x + self.mlp(self.ln2(x))


class TinyLM(nn.Module):
    def __init__(self, c: Cfg):
        super().__init__()
        self.c = c
        self.tok = nn.Embedding(len(c.vocab) + 1, c.d)  # +1 for unknown characters
        self.pos = nn.Embedding(c.block, c.d)
        self.blocks = nn.ModuleList(Block(c) for _ in range(c.layers))
        self.ln = nn.LayerNorm(c.d)
        self.head = nn.Linear(c.d, len(c.vocab) + 1, bias=False)
        self.stoi = {ch: i for i, ch in enumerate(c.vocab)}

    def encode(self, s: str) -> list[int]:
        unk = len(self.c.vocab)
        return [self.stoi.get(ch, unk) for ch in s]

    def forward(self, idx):
        t = idx.shape[1]
        mask = torch.triu(torch.full((t, t), float("-inf"), device=idx.device), diagonal=1)
        x = self.tok(idx) + self.pos(torch.arange(t, device=idx.device))
        for b in self.blocks:
            x = b(x, mask)
        return self.head(self.ln(x))

    @torch.no_grad()
    def generate(self, text: str, max_new: int = 110) -> str:
        """Free greedy decoding, kept for inspection."""
        self.eval()
        ids = self.encode(text[: self.c.block - max_new - 1] + SEP)
        out = []
        for _ in range(max_new):
            nxt = int(self(torch.tensor([ids[-self.c.block :]]))[0, -1].argmax())
            ch = self.c.vocab[nxt] if nxt < len(self.c.vocab) else "?"
            if ch == END:
                break
            out.append(ch)
            ids.append(nxt)
        return "".join(out)

    @torch.no_grad()
    def extract(self, text: str) -> dict | None:
        """Schema-constrained decoding. The field names and separators are
        known, so they are written in rather than predicted, and each value
        may only use characters that can form a legal value for its field.
        The model still chooses every value character and when to stop."""
        self.eval()
        ids = self.encode(text[: self.c.block - 120] + SEP)
        values = {}
        for i, (short, full) in enumerate(FIELDS):
            ids += self.encode(("" if i == 0 else ";") + short + "=")
            values[full] = self._value(ids, full)
            if values[full] is None:
                return None
        return parse_answer(answer_raw(values))

    def _value(self, ids: list[int], field: str, limit: int = 40) -> str | None:
        choices = VALUE_CHOICES.get(field)
        stop = self.stoi[";"] if field != FIELDS[-1][1] else self.stoi[END]
        val = ""
        for _ in range(limit):
            if choices and val in choices and not any(c != val and c.startswith(val) for c in choices):
                break  # the value is complete and nothing longer is legal
            logits = self(torch.tensor([ids[-self.c.block :]]))[0, -1]
            allowed = self._allowed(field, val, choices)
            if self._can_stop(field, val, choices):
                allowed.add(stop)
            if not allowed:
                return None
            mask = torch.full_like(logits, float("-inf"))
            mask[list(allowed)] = 0
            nxt = int((logits + mask).argmax())
            if nxt == stop:
                break
            val += self.c.vocab[nxt]
            ids.append(nxt)
        return val or None

    def _allowed(self, field: str, val: str, choices) -> set[int]:
        if choices:
            nxt = {c[len(val)] for c in choices if c.startswith(val) and len(c) > len(val)}
        elif field in ("rate", "rooms"):
            nxt = set("0123456789") if len(val) < 6 else set()
        elif field in ("check_in", "check_out"):
            nxt = set("0123456789") | ({"-"} if val and "-" not in val else set())
        else:
            nxt = {ch for ch in self.c.vocab if ch not in (";", "=", SEP, END)}
        return {self.stoi[ch] for ch in nxt if ch in self.stoi}

    @staticmethod
    def _can_stop(field: str, val: str, choices) -> bool:
        if choices:
            return val in choices
        if field in ("check_in", "check_out"):
            d, _, m = val.partition("-")
            return bool(d) and bool(m) and 1 <= int(d) <= 31 and 1 <= int(m) <= 12
        return bool(val)


VALUE_CHOICES = {"room_type": tuple(messages.ROOMS), "meal_plan": tuple(messages.MEALS)}


def answer_raw(values: dict) -> str:
    return ";".join(f"{short}={values[full]}" for short, full in FIELDS)


def batches(data: list[tuple[str, dict]], model: TinyLM, bs: int, r: np.random.Generator):
    block = model.c.block
    while True:
        rows = []
        for i in r.integers(0, len(data), bs):
            text, truth = data[i]
            prompt = model.encode(text[: block - 120] + SEP)
            target = model.encode(answer(truth) + END)
            rows.append((prompt, (prompt + target)[: block + 1]))
        # Pad to the longest sequence in the batch, not to the block size.
        width = max(len(seq) for _, seq in rows) - 1
        x = torch.zeros((bs, width), dtype=torch.long)
        y = torch.full((bs, width), -100, dtype=torch.long)
        for row, (prompt, seq) in enumerate(rows):
            x[row, : len(seq) - 1] = torch.tensor(seq[:-1])
            # Loss only on the answer: positions whose next token is in it.
            y[row, len(prompt) - 1 : len(seq) - 1] = torch.tensor(seq[len(prompt) :])
        yield x, y


def train(steps: int = 2000, bs: int = 32, seed: int = 0, n: int = 30000, log_every: int = 250) -> TinyLM:
    torch.manual_seed(seed)
    # Half the messages come from the training templates and half are
    # composed from loose clauses; the held-out templates are never used.
    data = samples(n // 2, seed, messages.TRAIN_TEMPLATES, noise=None) + samples(n - n // 2, seed + 1, None, noise=None)
    chars = sorted({ch for text, truth in data for ch in text + answer(truth)} | {SEP, END})
    model = TinyLM(Cfg(vocab="".join(chars)))
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / 100) * 0.5 * (1 + math.cos(math.pi * min(s, steps) / steps))
    )
    gen = batches(data, model, bs, np.random.default_rng(seed))
    started = time.monotonic()
    for step in range(1, steps + 1):
        model.train()
        x, y = next(gen)
        loss = F.cross_entropy(model(x).reshape(-1, model.head.out_features), y.reshape(-1), ignore_index=-100)
        opt.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        if step % log_every == 0:
            print(f"step {step} loss {loss.item():.4f} {time.monotonic() - started:.0f}s", flush=True)
    return model


def save(model: TinyLM, path: Path = MODEL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"cfg": asdict(model.c), "state": model.state_dict()}, path)


def load(path: Path = MODEL_PATH) -> TinyLM:
    blob = torch.load(path, map_location="cpu", weights_only=True)
    model = TinyLM(Cfg(**blob["cfg"]))
    model.load_state_dict(blob["state"])
    model.eval()
    return model


def score(extract_fn, data) -> dict:
    """Exact match on the whole record, and accuracy per field."""
    exact, per = 0, {full: 0 for _, full in FIELDS}
    for text, truth in data:
        want = parse_answer(answer(truth))
        got = extract_fn(text) or {}
        exact += got == want
        for k in per:
            per[k] += str(got.get(k)) == str(want[k])
    n = len(data)
    return {"n": n, "exact": round(exact / n, 3), "fields": {k: round(v / n, 3) for k, v in per.items()}}


def evaluate(model: TinyLM, n: int = 300) -> dict:
    from .extract_baseline import regex_extract

    sets = {
        "seen_templates": samples(n, 1001, messages.TRAIN_TEMPLATES),
        "held_out_templates": samples(n, 1002, messages.HELD_OUT_TEMPLATES),
        "held_out_templates_with_typos": samples(n, 1003, messages.HELD_OUT_TEMPLATES, noise=0.04),
    }

    def cascade(text: str) -> dict | None:
        # What the normalizer does: regex when it parses, the model otherwise.
        return regex_extract(text) or model.extract(text)

    return {
        name: {
            "slm": score(model.extract, data),
            "regex": score(regex_extract, data),
            "regex_then_slm": score(cascade, data),
        }
        for name, data in sets.items()
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=("train", "eval"))
    p.add_argument("--steps", type=int, default=2000)
    args = p.parse_args()
    if args.cmd == "train":
        model = train(steps=args.steps)
        save(model)
        print(f"saved {MODEL_PATH} ({sum(p.numel() for p in model.parameters()):,} parameters)")
    else:
        import json

        print(json.dumps(evaluate(load()), indent=2))


if __name__ == "__main__":
    main()
