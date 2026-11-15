"""One or two sentences that tell the revenue manager why a rate moved.

A large language model writes the sentence from a fixed set of facts. The
reply is checked before it is shown: every number in it must appear in the
facts, and it must not be longer than two sentences. If the model is not
configured, is down, or fails the check, a template writes the sentence
instead. The template is always right and a little dull; the model is
friendlier but never trusted to add facts.

Providers: any OpenAI-compatible chat endpoint (OpenAI, Groq, a local
Ollama) or Anthropic, chosen by environment variables:

    LLM_PROVIDER=openai|anthropic  LLM_API_KEY=...  LLM_MODEL=...  LLM_BASE_URL=...
"""

from __future__ import annotations

import json
import os
import re

import httpx

from ..market.hotels import CLIENT

PROMPT = """You explain hotel rate changes to a revenue manager.
Write at most two short sentences. Use only these facts and do not add any other numbers.
Facts (JSON): {facts}"""

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def facts_for(rec) -> dict:
    f = {
        "stay_date": rec.stay.strftime("%a %d %b"),
        "current_rate": rec.current,
        "recommended_rate": rec.recommended,
        "change_percent": round(rec.change * 100),
        "rooms_on_books": rec.forecast.on_books,
        "forecast_occupancy_percent": round(min(1.0, rec.forecast.rooms / CLIENT.rooms) * 100),
        "days_out": rec.forecast.lead,
    }
    if rec.comp_median:
        f["competitor_median"] = rec.comp_median
    if rec.forecast.event:
        f["event"] = rec.forecast.event
    if any(r["code"] == "guardrail_max_step" for r in rec.reasons):
        f["capped_by_daily_limit"] = True
    return f


def template(f: dict) -> str:
    move = (
        "Hold"
        if f["recommended_rate"] == f["current_rate"]
        else ("Raise" if f["recommended_rate"] > f["current_rate"] else "Lower")
    )
    parts = [f"{move} {f['stay_date']} to ₹{f['recommended_rate']:,} (now ₹{f['current_rate']:,})."]
    why = [f"forecast {f['forecast_occupancy_percent']}% occupancy with {f['rooms_on_books']} rooms on the books"]
    if "competitor_median" in f:
        why.append(f"competitor median ₹{f['competitor_median']:,}")
    if "event" in f:
        why.append(f"{f['event']} that week")
    parts.append(
        "Based on "
        + ", ".join(why)
        + ("; capped by the daily change limit." if f.get("capped_by_daily_limit") else ".")
    )
    return " ".join(parts)


def _allowed_numbers(f: dict) -> set[str]:
    allowed = set()
    for v in f.values():
        for n in NUMBER.findall(str(v)):
            allowed.add(n.replace(",", ""))
        if isinstance(v, int):
            allowed.add(str(v))
            allowed.add(str(abs(v)))
    return allowed


def check(text: str, f: dict) -> bool:
    if not text or text.count(".") > 3 or len(text) > 400:
        return False
    allowed = _allowed_numbers(f)
    return all(n.replace(",", "") in allowed for n in NUMBER.findall(text))


def _call(prompt: str, client: httpx.Client) -> str:
    provider = os.environ.get("LLM_PROVIDER", "")
    key, model = os.environ.get("LLM_API_KEY", ""), os.environ.get("LLM_MODEL", "")
    if provider == "anthropic":
        r = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": model, "max_tokens": 120, "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    r = client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "max_tokens": 120, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]},
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def explain(rec, client: httpx.Client | None = None) -> tuple[str, str]:
    """Returns (text, source) where source is 'llm' or 'template'."""
    f = facts_for(rec)
    if os.environ.get("LLM_PROVIDER"):
        try:
            with client or httpx.Client(timeout=15) as c:
                text = _call(PROMPT.format(facts=json.dumps(f)), c)
            if check(text, f):
                return text, "llm"
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            pass
    return template(f), "template"
