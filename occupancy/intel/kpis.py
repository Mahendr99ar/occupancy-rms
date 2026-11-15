"""The numbers a revenue manager reads every morning, computed from MySQL.

  Occupancy  rooms sold / rooms available
  ADR        room revenue / rooms sold           (average daily rate)
  RevPAR     room revenue / rooms available      (= occupancy x ADR)
  Pickup     rooms booked in the last N days for future stay dates, net of
             cancellations made in the same days
  Pace       rooms on the books now vs on the same weekday four weeks ago at
             the same number of days out

Used by the API, the Excel report and the slide deck, so all three agree.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from ..market.hotels import CLIENT
from ..pipeline.db import feed_checks, rate_requests, recommendations
from . import data
from .engine import COMPSET, current_rates


def stay_rows(res, stays: list[date]) -> dict[date, tuple[int, int]]:
    """(rooms sold, room revenue) per stay date from confirmed bookings."""
    out = {d: [0, 0] for d in stays}
    alive = res[res.status == "confirmed"]
    for arrival, nights, adr in zip(alive.arrival, alive.nights, alive.adr, strict=True):
        for i in range(nights):
            d = arrival + timedelta(days=i)
            if d in out:
                out[d][0] += 1
                out[d][1] += adr
    return {d: (v[0], v[1]) for d, v in out.items()}


def summarize(rows: dict[date, tuple[int, int]]) -> dict:
    sold = sum(r[0] for r in rows.values())
    revenue = sum(r[1] for r in rows.values())
    available = CLIENT.rooms * len(rows)
    return {
        "occupancy": round(sold / available, 4) if available else 0,
        "adr": round(revenue / sold) if sold else 0,
        "revpar": round(revenue / available) if available else 0,
        "rooms_sold": sold,
        "revenue": revenue,
    }


def overview(conn, as_of: date) -> dict:
    res = data.load_reservations(conn)
    books = data.load_books(conn)
    past = [as_of - timedelta(days=i) for i in range(1, 29)]
    ahead = [as_of + timedelta(days=i) for i in range(30)]
    last7 = res[(res.booked_on > as_of - timedelta(days=7)) & (res.booked_on <= as_of)]
    cancelled7 = res[res.cancelled_on.map(lambda d: d is not None and as_of - timedelta(days=7) < d <= as_of)]
    pace_now = sum(books.otb(s, as_of) for s in ahead)
    pace_then = sum(books.otb(s - timedelta(days=28), as_of - timedelta(days=28)) for s in ahead)
    return {
        "as_of": as_of.isoformat(),
        "last_28_days": summarize(stay_rows(res, past)),
        "next_30_days_on_books": summarize(stay_rows(res, ahead)),
        "pickup_7_days": int(last7.nights.sum() - cancelled7.nights.sum()),
        "pace_vs_4_weeks_ago": round(pace_now / pace_then - 1, 3) if pace_then else None,
    }


def calendar(conn, as_of: date, days: int = 30) -> list[dict]:
    stays = [as_of + timedelta(days=i) for i in range(days)]
    books = data.load_books(conn)
    evts = data.known_events(conn, as_of)
    comps = data.comp_rates(conn, as_of, stays, COMPSET)
    now = current_rates(conn, as_of, stays)
    recs = {
        r.stay_date: r for r in conn.execute(select(recommendations).where(recommendations.c.business_date == as_of))
    }
    out = []
    for s in stays:
        rec = recs.get(s)
        ev = data.event_on(evts, s)
        c = comps.get(s, {})
        out.append(
            {
                "date": s.isoformat(),
                "weekday": s.strftime("%a"),
                "on_books": books.otb(s, as_of),
                "occupancy_on_books": round(books.otb(s, as_of) / CLIENT.rooms, 3),
                "forecast_rooms": rec.forecast_rooms if rec else None,
                "current_rate": now[s],
                "recommended_rate": rec.recommended_rate if rec else None,
                "comp_median": rec.comp_median if rec else None,
                "comp_low": min(c.values()) if c else None,
                "comp_high": max(c.values()) if c else None,
                "event": ev["name"] if ev else None,
                "status": rec.status if rec else None,
                "id": rec.id if rec else None,
                "explanation": rec.explanation if rec else None,
                "reasons": rec.reasons if rec else [],
            }
        )
    return out


def latest_checks(conn) -> list[dict]:
    last = conn.execute(select(func.max(feed_checks.c.at))).scalar()
    if last is None:
        return []
    rows = conn.execute(
        select(feed_checks).where(feed_checks.c.at >= last - timedelta(minutes=5)).order_by(feed_checks.c.id)
    ).mappings()
    seen, out = set(), []
    for r in reversed(list(rows)):
        key = (r["feed"], r["check_name"])
        if key not in seen:
            seen.add(key)
            out.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()})
    return list(reversed(out))


def requests_list(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(select(rate_requests).order_by(rate_requests.c.received.desc()).limit(limit)).mappings()
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]


def upcoming_arrivals(conn, as_of: date, days: int = 14):
    res = data.load_reservations(conn)
    return res[(res.status == "confirmed") & res.arrival.map(lambda d: as_of <= d < as_of + timedelta(days=days))]
