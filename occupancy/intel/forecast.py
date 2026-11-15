"""Room demand forecast by the additive pickup method, the standard first
model in hotel revenue management.

For a stay date `lead` days away, the forecast is the rooms already on the
books plus the rooms that similar past stay dates picked up between the same
lead and arrival:

    forecast = on_books(stay, today) + mean over past stays s' with the
               same weekday of [ final(s') - on_books(s', s' - lead) ]

Past stay dates that fell on a known event are averaged separately, since an
expo week picks up very differently from a normal week. When there is no
event history yet, normal pickup is scaled by a prior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from ..market.hotels import CLIENT
from .data import Books, event_on

LOOKBACK_WEEKS = 8
SOLD_OUT = 0.97  # share of rooms at which a stay date counts as sold out
EVENT_PRIOR = 1.3


@dataclass
class Forecast:
    stay: date
    lead: int
    on_books: int
    pickup: float  # rooms expected to book from now to arrival, at `price_basis`
    event: str | None
    price_basis: float | None = None  # average rate the historical pickup was sold at

    @property
    def rooms(self) -> float:
        """Unconstrained demand: can exceed the hotel's rooms. Pricing uses this."""
        return self.on_books + self.pickup

    @property
    def sold(self) -> float:
        """Rooms the hotel would sell at the basis rate, capped by capacity."""
        return min(self.rooms, CLIENT.rooms)


def pickup_table(
    books: Books, as_of: date, events: list[dict], max_lead: int = 60, capacity: int = CLIENT.rooms
) -> dict:
    """Average pickup by (weekday, lead, is_event), from stay dates in the
    last LOOKBACK_WEEKS weeks that had already happened by `as_of`."""
    past = [as_of - timedelta(days=i) for i in range(1, LOOKBACK_WEEKS * 7 + 1)]
    open_: dict[tuple, list[tuple[float, float | None]]] = {}
    full: dict[tuple, list[tuple[float, float | None]]] = {}
    for s in past:
        final = books.otb(s, as_of)
        is_event = event_on(events, s) is not None
        # A stay date that sold out tells us demand was at least its pickup,
        # not what it was: the guests turned away never show up in the PMS.
        target = full if final >= SOLD_OUT * capacity else open_
        for lead in range(0, max_lead + 1):
            snap = s - timedelta(days=lead)
            if books.first_booking is None or snap < books.first_booking:
                break
            # Demand = rooms picked up + requests turned away for lack of rooms.
            demand = final - books.otb(s, snap) + books.turned_away(s, snap, as_of)
            target.setdefault((s.weekday(), lead, is_event), []).append((demand, books.pickup_rate(s, snap, as_of)))
    table = {}
    for k in open_.keys() | full.keys():
        uncensored, censored = open_.get(k, []), full.get(k, [])
        use = uncensored if len(uncensored) >= 2 else uncensored + censored
        pick = mean(p for p, _ in use)
        if len(uncensored) >= 2 and censored:
            # Sold-out dates still count, as a lower bound.
            pick = max(pick, mean(p for p, _ in censored))
        rates = [r for _, r in use + censored if r]
        table[k] = (pick, mean(rates) if rates else None)
    return table


def forecast(
    books: Books, as_of: date, stays: list[date], events: list[dict], table: dict | None = None
) -> dict[date, Forecast]:
    table = table if table is not None else pickup_table(books, as_of, events)
    otb = {s: books.otb(s, as_of) for s in stays}
    out = {}
    for s in stays:
        lead = (s - as_of).days
        ev = event_on(events, s)
        key = (s.weekday(), lead, ev is not None)
        if key in table:
            pick, basis = table[key]
        else:
            pick, basis = table.get((s.weekday(), lead, False), (0.0, None))
            pick *= EVENT_PRIOR if ev else 1.0
        out[s] = Forecast(s, lead, otb[s], max(0.0, pick), ev["name"] if ev else None, basis)
    return out


def backtest(books: Books, events: list[dict], days: list[date], leads=(1, 3, 7, 14, 30)) -> dict:
    """Mean absolute error of rooms sold (the capped forecast) against what happened,
    next to two baselines: 'final = rooms on the books now', and 'final =
    what the most recent same-weekday stay date ended with'."""
    errors: dict[int, list[float]] = {lead: [] for lead in leads}
    naive: dict[int, list[float]] = {lead: [] for lead in leads}
    last_week: dict[int, list[float]] = {lead: [] for lead in leads}
    for as_of in days:
        known = [e for e in events if e["announced"] <= as_of]
        table = pickup_table(books, as_of, known, max(leads))
        stays = [as_of + timedelta(days=lead) for lead in leads]
        fc = forecast(books, as_of, stays, known, table)
        final = {s: books.final(s) for s in stays}
        for lead, s in zip(leads, stays, strict=True):
            errors[lead].append(abs(fc[s].sold - final[s]))
            naive[lead].append(abs(fc[s].on_books - final[s]))
            # Most recent same-weekday stay date that had already happened.
            prev = s - timedelta(days=7 * (lead // 7 + 1))
            last_week[lead].append(abs(books.otb(prev, as_of) - final[s]))
    return {
        lead: {
            "pickup_mae": round(mean(errors[lead]), 1),
            "on_books_mae": round(mean(naive[lead]), 1),
            "same_weekday_mae": round(mean(last_week[lead]), 1),
        }
        for lead in leads
    }
