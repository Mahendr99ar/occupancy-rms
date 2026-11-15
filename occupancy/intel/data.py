"""Reads the clean tables into the shapes the pricing engine needs.

Everything here takes an `as_of` business date and only uses what was known
on that date: bookings made by then, cancellations made by then, events
announced by then, rates observed that day. Backtests depend on that; a
forecast that peeks at later bookings looks great and means nothing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median

import numpy as np
import pandas as pd
from sqlalchemy import and_, select

from ..market.hotels import CLIENT
from ..pipeline.db import events, rate_observations, reservations, turnaways


def load_turnaways(conn) -> list[tuple[date, date]]:
    return [(d, s) for d, s in conn.execute(select(turnaways.c.day, turnaways.c.stay_date))]


def load_books(conn) -> Books:
    return Books(load_reservations(conn), load_turnaways(conn))


def load_reservations(conn) -> pd.DataFrame:
    df = pd.read_sql(select(reservations), conn)
    for c in ("booked_on", "arrival", "cancelled_on"):
        df[c] = [None if pd.isna(x) else x for x in pd.to_datetime(df[c]).dt.date]
    return df


class Books:
    """Reservations indexed by stay date, for fast 'rooms on the books for
    stay date S as of day D' questions. For each stay date it keeps the
    booking day and cancellation day (or infinity) of every reservation that
    covers it; the count is then one vectorised comparison."""

    NEVER = 10**9

    def __init__(self, res: pd.DataFrame, turnaways: list[tuple[date, date]] | None = None):
        covers: dict[date, list[tuple[int, int, float]]] = defaultdict(list)
        adrs = res.adr if "adr" in res else [0.0] * len(res)
        for booked, arrival, nights, cancelled, adr in zip(
            res.booked_on, res.arrival, res.nights, res.cancelled_on, adrs, strict=True
        ):
            c = self.NEVER if cancelled is None else cancelled.toordinal()
            for i in range(nights):
                covers[arrival + timedelta(days=i)].append((booked.toordinal(), c, float(adr)))
        self.covers = {d: tuple(np.array(col) for col in zip(*v, strict=True)) for d, v in covers.items()}
        self.first_booking = min(res.booked_on) if len(res) else None
        denied: dict[date, list[int]] = defaultdict(list)
        for day, stay in turnaways or []:
            denied[stay].append(day.toordinal())
        self.denied = {d: np.array(v) for d, v in denied.items()}

    def turned_away(self, stay: date, since: date, as_of: date) -> int:
        """Requests for `stay` refused for lack of rooms after `since`, up to `as_of`."""
        if stay not in self.denied:
            return 0
        days = self.denied[stay]
        return int(np.count_nonzero((days > since.toordinal()) & (days <= as_of.toordinal())))

    def otb(self, stay: date, as_of: date) -> int:
        if stay not in self.covers:
            return 0
        booked, cancelled, _ = self.covers[stay]
        t = as_of.toordinal()
        return int(np.count_nonzero((booked <= t) & (cancelled > t)))

    def pickup_rate(self, stay: date, since: date, as_of: date) -> float | None:
        """Average nightly rate of the rooms picked up for `stay` after
        `since` and still booked on `as_of`: the price that pickup was
        bought at."""
        if stay not in self.covers:
            return None
        booked, cancelled, adr = self.covers[stay]
        sel = (booked > since.toordinal()) & (booked <= as_of.toordinal()) & (cancelled > as_of.toordinal())
        return float(adr[sel].mean()) if sel.any() else None

    def final(self, stay: date) -> int:
        return self.otb(stay, stay + timedelta(days=1))


def on_books(res, stays: list[date], as_of: date) -> dict[date, int]:
    books = res if isinstance(res, Books) else Books(res)
    return {d: books.otb(d, as_of) for d in stays}


def room_revenue_on_books(res: pd.DataFrame, stays: list[date], as_of: date) -> dict[date, int]:
    alive = res[(res.booked_on <= as_of) & (res.status == "confirmed")]
    out: dict[date, int] = defaultdict(int)
    wanted = set(stays)
    for arrival, nights, adr in zip(alive.arrival, alive.nights, alive.adr, strict=True):
        for i in range(nights):
            d = arrival + timedelta(days=i)
            if d in wanted:
                out[d] += adr
    return dict(out)


def comp_rates(conn, as_of: date, stays: list[date], hotel_ids: list[str] | None = None) -> dict[date, dict[str, int]]:
    """Each competitor's rate per stay date as observed on `as_of`. Where
    OTAs disagree for the same hotel, the median of their prices is used."""
    q = select(rate_observations.c.hotel_id, rate_observations.c.stay_date, rate_observations.c.rate).where(
        and_(
            rate_observations.c.business_date == as_of,
            rate_observations.c.rate.is_not(None),
            rate_observations.c.hotel_id != CLIENT.id,
            rate_observations.c.stay_date.in_(stays),
        )
    )
    seen: dict[tuple[date, str], list[int]] = defaultdict(list)
    for hid, stay, rate in conn.execute(q):
        if hotel_ids is None or hid in hotel_ids:
            seen[(stay, hid)].append(rate)
    out: dict[date, dict[str, int]] = defaultdict(dict)
    for (stay, hid), rates in seen.items():
        out[stay][hid] = int(median(rates))
    return dict(out)


def client_ota_rates(conn, as_of: date) -> dict[tuple[date, str], int | None]:
    q = select(rate_observations.c.stay_date, rate_observations.c.ota, rate_observations.c.rate).where(
        and_(rate_observations.c.business_date == as_of, rate_observations.c.hotel_id == CLIENT.id)
    )
    return {(s, ota): rate for s, ota, rate in conn.execute(q)}


def known_events(conn, as_of: date) -> list[dict]:
    rows = conn.execute(select(events).where(events.c.announced <= as_of)).mappings()
    return [dict(r) for r in rows]


def event_on(evts: list[dict], d: date) -> dict | None:
    return next((e for e in evts if e["start"] <= d <= e["end"]), None)
