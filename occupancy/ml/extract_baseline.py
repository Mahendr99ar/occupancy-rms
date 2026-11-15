"""Hand-written regex extraction for rate requests. It is the fallback when
the model service is down, and the baseline the small language model has to
beat.

Both extractors produce dates as day and month only (the messages never
carry a year) and share `with_year` to pick the year from the date the
message was received.
"""

from __future__ import annotations

import re
from datetime import date

from ..market.messages import COMPANIES, MEALS, MONTHS, MONTHS_LONG, ROOMS

MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(MONTHS)} | {m.lower(): i + 1 for i, m in enumerate(MONTHS_LONG)}


def with_year(day_month: str, received: date) -> date:
    """'14-12' -> the first 14 December on or after the received date."""
    d, m = (int(x) for x in day_month.split("-"))
    candidate = date(received.year, m, d)
    return candidate if candidate >= received else date(received.year + 1, m, d)


def regex_extract(text: str) -> dict | None:
    t = text.lower()
    out: dict = {}
    out["company"] = next((c for c in COMPANIES if c.lower() in t), None)
    for canonical, spellings in ROOMS.items():
        if any(re.search(rf"\b{re.escape(s.lower())}\b", t) for s in spellings):
            out["room_type"] = canonical
            break
    for canonical, spellings in MEALS.items():
        if any(s.lower() in t for s in spellings):
            out["meal_plan"] = canonical
    money = re.search(r"(?:inr|rs\.?|₹)\s*([\d,]+)|([\d,]+)/-", t)
    if money:
        out["rate"] = int((money.group(1) or money.group(2)).replace(",", ""))
    rooms = re.search(
        r"(?:qty|rooms:|block)\s*(\d+)|(\d+)\s*x\b|\b(\d+)\s+(?:rooms|deluxe|executive|exec|suite|jr|dlx|exe|ste)", t
    )
    if rooms:
        out["rooms"] = int(next(g for g in rooms.groups() if g))
    out.update(_dates(t) or {})
    keys = {"company", "room_type", "check_in", "check_out", "rate", "meal_plan", "rooms"}
    return out if keys <= out.keys() and all(out[k] is not None for k in keys) else None


def _dates(t: str) -> dict | None:
    m = re.search(r"(?:check in|arr)\s*(\d{1,2})[/.](\d{1,2})\s*(?:out|dep)\s*(\d{1,2})[/.](\d{1,2})", t)
    if m:
        a, b, c, d = m.groups()
        return {"check_in": f"{int(a)}-{int(b)}", "check_out": f"{int(c)}-{int(d)}"}
    month = "|".join(sorted(MONTH_NUM, key=len, reverse=True))
    m = re.search(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s*({month})?\s*(?:-|to)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s*({month})", t)
    if m:
        d1, m1, d2, m2 = m.groups()
        m1 = m1 or m2
        return {"check_in": f"{int(d1)}-{MONTH_NUM[m1]}", "check_out": f"{int(d2)}-{MONTH_NUM[m2]}"}
    return None


def finalize(fields: dict | None, received: date) -> dict | None:
    """Turns day-month dates into ISO dates using the received date."""
    if not fields:
        return None
    try:
        ci = with_year(fields["check_in"], received)
        co = with_year(fields["check_out"], ci)
    except (KeyError, ValueError):
        return None
    return {**fields, "check_in": ci.isoformat(), "check_out": co.isoformat()}
