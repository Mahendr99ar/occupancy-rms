"""A deterministic hotel market, one business day at a time.

What the simulator decides:
  * how many guests want a room on each stay date (season, weekday, events),
  * how many of them book the client hotel, given its rate against the
    competitors' median rate (price elasticity),
  * which of those bookings cancel later, and when,
  * what every competitor charges for every stay date, every day,
  * which corporate rate requests arrive in the revenue manager's inbox.

What it does not decide: the client hotel's rate. That comes from outside,
through the channel manager, from whatever pricing policy is being tested.

Randomness is keyed by date, never drawn from one running stream. Two runs
with the same seed see the same potential guests and the same coin flips,
whatever rates they set, so a difference in revenue between two pricing
policies comes from the policies and not from luck (common random numbers).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np

from .hotels import CLIENT, COMPETITORS, LISTINGS
from .messages import rate_request

HORIZON = 60  # days ahead that can be booked
QUALITY_PREMIUM = 1.3  # what guests will pay over the competitor median for the client hotel
DOW = (1.10, 1.18, 1.18, 1.12, 0.92, 0.70, 0.74)  # Mon..Sun; weekday corporate demand
CHANNELS = ("Direct", "staykart", "tripnest", "roombazaar", "Corporate")
CHANNEL_SHARE = (0.18, 0.30, 0.22, 0.18, 0.12)
OTA_COMMISSION = {"Direct": 0.0, "Corporate": 0.0, "staykart": 0.18, "tripnest": 0.15, "roombazaar": 0.17}


def _seed(*parts) -> int:
    digest = hashlib.blake2b(repr(parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def rng(*parts) -> np.random.Generator:
    return np.random.default_rng(_seed(*parts))


@dataclass(frozen=True)
class Event:
    name: str
    start: date
    end: date  # inclusive
    multiplier: float
    announced: date  # first business day the event is public

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass
class Reservation:
    id: str
    hotel_id: str
    booked_on: date
    arrival: date
    nights: int
    adr: int
    channel: str
    deposit_type: str
    adults: int
    children: int
    special_requests: int
    is_repeated_guest: int
    previous_cancellations: int
    market_segment: str
    status: str = "confirmed"
    cancelled_on: date | None = None
    # Decided at booking time so the outcome does not depend on later rates.
    will_cancel_on: date | None = field(default=None, repr=False)

    @property
    def lead_time(self) -> int:
        return (self.arrival - self.booked_on).days

    def stay_dates(self) -> list[date]:
        return [self.arrival + timedelta(days=i) for i in range(self.nights)]

    def public(self) -> dict:
        d = asdict(self)
        d.pop("will_cancel_on")
        return {k: v.isoformat() if isinstance(v, date) else v for k, v in d.items()}


@dataclass
class Config:
    seed: int = 7
    start: date = date(2026, 6, 1)
    base_demand: float = 110.0  # potential room-nights an average weekday sends the client
    elasticity: float = 2.6  # demand falls by about this share per 100% price premium
    lost_noise: float = 0.0


def make_events(cfg: Config, days: int = 400) -> list[Event]:
    """Invented trade fairs, conferences and concerts. Each is announced
    some weeks before it starts; a pricing engine only knows about it from
    the announcement date."""
    names = (
        "Trade Expo",
        "Pharma Conference",
        "Auto Components Fair",
        "Music Festival",
        "Fintech Summit",
        "Jewellery Show",
        "Education Fair",
        "Logistics Expo",
    )
    r = rng(cfg.seed, "events")
    events = []
    d = cfg.start + timedelta(days=int(r.integers(10, 25)))
    i = 0
    while d < cfg.start + timedelta(days=days):
        length = int(r.integers(2, 5))
        mult = float(np.round(r.uniform(1.25, 1.75), 2))
        notice = int(r.integers(14, 45))
        events.append(
            Event(
                f"{names[i % len(names)]} {d.year}", d, d + timedelta(days=length - 1), mult, d - timedelta(days=notice)
            )
        )
        d += timedelta(days=int(r.integers(18, 40)))
        i += 1
    return events


def season(d: date) -> float:
    # Business districts slow down in the monsoon and around year end.
    return {
        6: 0.86,
        7: 0.84,
        8: 0.88,
        9: 0.95,
        10: 1.02,
        11: 1.06,
        12: 0.92,
        1: 1.04,
        2: 1.08,
        3: 1.06,
        4: 0.98,
        5: 0.92,
    }[d.month]


def booking_curve() -> np.ndarray:
    """Share of a stay date's final demand that books at each lead time."""
    lead = np.arange(HORIZON + 1)
    w = np.exp(-lead / 11.0)
    w[:2] *= 1.6  # walk-ins and same-day bookings
    return w / w.sum()


CURVE = booking_curve()
CURVE_LEFT = CURVE[::-1].cumsum()[::-1]  # share booked at this lead or earlier (further out)


class Market:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.today = self.cfg.start
        self.events = make_events(self.cfg)
        self.client_rates: dict[date, int] = {}
        self.reservations: list[Reservation] = []
        self.inbox: list[dict] = []
        self.denied: dict[date, int] = {}
        self._sold: dict[date, int] = {}
        self._n = 0
        # Every booking and cancellation, in order. The PMS API pages through
        # this log, so a client can resume from the last sequence it saw.
        self.changes: list[str] = []
        self._by_id: dict[str, Reservation] = {}
        # Guests who asked for a stay date the hotel could not fit them into.
        # Booking engines log these as "regrets" or "denials"; without them a
        # sold-out night looks like a night with exactly that much demand.
        self.turnaways: list[tuple[date, date]] = []

    # ---------------------------------------------------------------- demand

    def event_multiplier(self, d: date) -> float:
        m = 1.0
        for e in self.events:
            if e.covers(d):
                m = max(m, e.multiplier)
        return m

    def market_demand(self, d: date) -> float:
        return self.cfg.base_demand * DOW[d.weekday()] * season(d) * self.event_multiplier(d)

    def known_events(self, on: date | None = None) -> list[Event]:
        on = on or self.today
        return [e for e in self.events if e.announced <= on]

    # ----------------------------------------------------------- competitors

    def comp_rate(self, hotel, stay: date, on: date | None = None) -> int | None:
        """Competitor's rate for a stay date as seen on business day `on`.
        None means sold out."""
        on = on or self.today
        lead = (stay - on).days
        if lead < 0 or lead > HORIZON:
            return None
        pressure = self.market_demand(stay) / (self.cfg.base_demand * 1.0)
        if not hotel.event_aware or self.event_known(stay, on) is None:
            pressure /= self.event_multiplier(stay)
        arrived = CURVE_LEFT[lead]  # share of the stay date's demand already booked
        filled = min(0.98, 0.62 * pressure * arrived + 0.08)
        weekday_adj = 1.0 if stay.weekday() < 4 else 0.86
        r = rng(self.cfg.seed, "comp", hotel.id, stay.toordinal(), on.toordinal())
        rate = hotel.base_rate * weekday_adj * season(stay) * (0.82 + 0.45 * filled) * r.normal(1.0, 0.025)
        if hotel.event_aware and self.event_known(stay, on):
            rate *= 1.12
        if filled > 0.93 and r.random() < 0.5:
            return None
        return int(round(rate / 50.0) * 50)

    def event_known(self, stay: date, on: date) -> Event | None:
        for e in self.events:
            if e.covers(stay) and e.announced <= on:
                return e
        return None

    def comp_median(self, stay: date, on: date | None = None) -> float:
        rates = [r for h in COMPETITORS if (r := self.comp_rate(h, stay, on)) is not None]
        return float(np.median(rates)) if rates else float(CLIENT.base_rate)

    # ---------------------------------------------------------------- client

    def static_rate(self, stay: date) -> int:
        """What the client charged before using a pricing engine: one rate
        for weekdays, one for weekends."""
        return CLIENT.base_rate if stay.weekday() < 4 else int(CLIENT.base_rate * 0.85)

    def client_rate(self, stay: date) -> int:
        return self.client_rates.get(stay, self.static_rate(stay))

    def set_rate(self, stay: date, rate: int) -> None:
        self.client_rates[stay] = int(rate)

    def rooms_sold(self, d: date) -> int:
        return self._sold.get(d, 0)

    def ota_rate(self, ota: str, hotel_id: str, stay: date, on: date | None = None) -> int | None:
        """The rate an OTA displays. Usually the hotel's own rate (parity),
        but OTAs sometimes discount the client out of their own commission,
        which breaks parity and is something the hotel wants to know."""
        on = on or self.today
        if hotel_id == CLIENT.id:
            base = self.client_rate(stay)
            if self.rooms_sold(stay) >= CLIENT.rooms:
                return None
            r = rng(self.cfg.seed, "parity", ota, stay.toordinal(), on.toordinal())
            if r.random() < 0.04:
                return int(round(base * r.uniform(0.88, 0.95) / 50) * 50)
            return base
        from .hotels import BY_ID

        return self.comp_rate(BY_ID[hotel_id], stay, on)

    # ------------------------------------------------------------------ step

    def _cancel_probability(self, lead: int, channel: str, deposit: str, prev: int) -> float:
        z = -1.9 + 0.42 * math.log1p(lead) + (0.55 if channel in OTA_COMMISSION and OTA_COMMISSION[channel] else 0)
        z += -2.4 if deposit == "Non Refund" else 0.0
        z += 0.9 * min(prev, 2)
        return 1 / (1 + math.exp(-z))

    def _new_reservation(self, stay: date, lead: int, k: int) -> Reservation:
        r = rng(self.cfg.seed, "res", self.today.toordinal(), stay.toordinal(), k)
        nights = int(r.choice([1, 2, 3], p=[0.55, 0.30, 0.15]))
        channel = str(r.choice(CHANNELS, p=CHANNEL_SHARE))
        deposit = "Non Refund" if r.random() < (0.08 if channel == "Direct" else 0.14) else "No Deposit"
        prev = int(r.choice([0, 1, 2], p=[0.92, 0.06, 0.02]))
        rates = [self.client_rate(stay + timedelta(days=i)) for i in range(nights)]
        discount = 0.9 if deposit == "Non Refund" else 1.0
        self._n += 1
        res = Reservation(
            id=f"R{self.today:%y%m%d}{self._n:06d}",
            hotel_id=CLIENT.id,
            booked_on=self.today,
            arrival=stay,
            nights=nights,
            adr=int(sum(rates) / nights * discount),
            channel=channel,
            deposit_type=deposit,
            adults=int(r.choice([1, 2], p=[0.62, 0.38])),
            children=int(r.random() < 0.06),
            special_requests=int(r.choice([0, 1, 2], p=[0.6, 0.3, 0.1])),
            is_repeated_guest=int(r.random() < (0.22 if channel in ("Direct", "Corporate") else 0.03)),
            previous_cancellations=prev,
            market_segment="Corporate"
            if channel == "Corporate"
            else ("Direct" if channel == "Direct" else "Online TA"),
        )
        p = self._cancel_probability(lead, channel, deposit, prev)
        if r.random() < p and lead > 0:
            res.will_cancel_on = self.today + timedelta(days=int(r.integers(0, lead)))
        return res

    def _fits(self, stay: date, nights: int) -> bool:
        return all(self.rooms_sold(stay + timedelta(days=i)) < CLIENT.rooms for i in range(nights))

    def _book(self, res: Reservation, delta: int) -> None:
        for d in res.stay_dates():
            self._sold[d] = self._sold.get(d, 0) + delta

    def step(self) -> dict:
        """Play one business day: bookings for every open stay date, then
        cancellations, then the inbox. Returns a summary of the day."""
        new, cancelled = 0, 0
        for lead in range(HORIZON + 1):
            stay = self.today + timedelta(days=lead)
            r = rng(self.cfg.seed, "arrivals", self.today.toordinal(), stay.toordinal())
            # Potential guests for the client if it were the cheapest option;
            # each one books with a probability that falls with the price premium.
            lam = self.market_demand(stay) * CURVE[lead] * 1.35
            potential = int(r.poisson(lam))
            # Guests judge the client against the competitors' median, allowing
            # for it being a better hotel than the median competitor.
            premium = self.client_rate(stay) / (self.comp_median(stay) * QUALITY_PREMIUM) - 1
            accept = min(1.0, 0.55 * math.exp(-self.cfg.elasticity * premium))
            coins = r.random(potential)
            for k in range(potential):
                if coins[k] >= accept:
                    continue
                res = self._new_reservation(stay, lead, k)
                if not self._fits(stay, res.nights):
                    self.denied[stay] = self.denied.get(stay, 0) + 1
                    self.turnaways.append((self.today, stay))
                    continue
                self.reservations.append(res)
                self._by_id[res.id] = res
                self.changes.append(res.id)
                self._book(res, +1)
                new += 1

        for res in self.reservations:
            if res.status == "confirmed" and res.will_cancel_on == self.today:
                res.status, res.cancelled_on = "cancelled", self.today
                self.changes.append(res.id)
                self._book(res, -1)
                cancelled += 1

        r = rng(self.cfg.seed, "inbox", self.today.toordinal())
        if r.random() < 0.35:
            self.inbox.append(rate_request(r, self.today, len(self.inbox)))

        summary = {
            "date": self.today.isoformat(),
            "new": new,
            "cancelled": cancelled,
            "sold_today": self.rooms_sold(self.today),
        }
        self.today += timedelta(days=1)
        return summary

    def run(self, days: int, policy=None) -> list[dict]:
        """Advance `days` business days. `policy(market)` may set rates
        before each day's bookings."""
        log = []
        for _ in range(days):
            if policy is not None:
                policy(self)
            log.append(self.step())
        return log

    def reservation(self, res_id: str) -> Reservation:
        return self._by_id[res_id]

    # --------------------------------------------------------------- results

    def kpis(self, start: date, end: date) -> dict:
        """Occupancy, ADR and RevPAR for stay dates in [start, end]."""
        nights = (end - start).days + 1
        sold = revenue = 0
        for res in self.reservations:
            if res.status == "cancelled":
                continue
            for d in res.stay_dates():
                if start <= d <= end:
                    sold += 1
                    revenue += res.adr
        available = CLIENT.rooms * nights
        return {
            "room_nights": sold,
            "occupancy": sold / available,
            "adr": revenue / sold if sold else 0.0,
            "revpar": revenue / available,
            "revenue": revenue,
        }


__all__ = ["Market", "Config", "Event", "Reservation", "HORIZON", "LISTINGS", "rng"]
