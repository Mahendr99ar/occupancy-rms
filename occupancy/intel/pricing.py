"""Rate recommendations for each open stay date.

For a stay date the engine knows: rooms on the books, the pickup forecast at
today's rate, the competitors' rates (from rate shopping) and any announced
event. Rooms already sold do not care about tomorrow's price; only the
pickup still to come does, so the engine prices the remaining pickup:

    pickup(p)   = pickup(p_now) * exp(-elasticity * (p - p_now) / reference)
    revenue(p)  = p * min(rooms_left, pickup(p))
    recommend   = argmax revenue(p) over a grid, then guardrails

`reference` is the competitor median scaled by the hotel's usual position
against it, so the elasticity reads as "share of pickup lost per 100% price
premium over where the hotel normally sits".

Guardrails keep a human comfortable approving the output: a floor and a
ceiling from the hotel's base rate, a cap on how far one day's change can
move, and rounding to 50 rupees. Each one that fires is listed in the
reasons, so nobody has to guess why a number looks the way it does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from statistics import median

import numpy as np

from ..market.hotels import CLIENT
from .forecast import Forecast

MIN_PICKUP = 0.5  # rooms; below this the price has nothing to act on


@dataclass
class Policy:
    elasticity: float = 2.0
    position: float = 1.25  # the client's usual rate / competitor median
    floor: float = 0.65  # x base rate
    ceiling: float = 1.8  # x base rate
    max_step: float = 0.20  # max change from the current rate in one decision
    step: int = 50


@dataclass
class Recommendation:
    stay: date
    current: int
    recommended: int
    comp_median: int | None
    forecast: Forecast
    rooms_left: int
    reasons: list[dict] = field(default_factory=list)

    @property
    def change(self) -> float:
        return self.recommended / self.current - 1


def recommend(
    fc: Forecast, current: int, comps: dict[str, int], policy: Policy, capacity: int = CLIENT.rooms
) -> Recommendation:
    comp_med = int(median(comps.values())) if comps else None
    reference = (comp_med or current / policy.position) * policy.position
    rooms_left = max(0, capacity - fc.on_books)
    base = CLIENT.base_rate
    grid = np.arange(int(base * policy.floor), int(base * policy.ceiling) + 1, policy.step)

    # Historical pickup was bought at its own price; demand at price p is
    # scaled from that price, not from today's rate. Without this the engine
    # chases its own discounts: it lowers the rate, still sees yesterday's
    # pickup, and lowers again.
    anchor = fc.price_basis or current

    def expected(p: float) -> float:
        return fc.pickup * math.exp(-policy.elasticity * (p - anchor) / reference)

    revenue = [p * min(rooms_left, expected(p)) for p in grid]
    if rooms_left == 0:
        best = int(grid[-1])
    elif expected(current) < MIN_PICKUP:
        # Nothing left to sell into: any price earns the same, so hold.
        best = current
    else:
        best = int(grid[int(np.argmax(revenue))])
    pickup_now = expected(current)
    reasons = [
        {
            "code": "forecast",
            "on_books": fc.on_books,
            "pickup": round(pickup_now, 1),
            "forecast_occupancy": round(min(1.0, (fc.on_books + pickup_now) / capacity), 2),
            "lead_days": fc.lead,
        },
    ]
    if comp_med:
        reasons.append(
            {
                "code": "competitors",
                "median": comp_med,
                "position_now": round(current / comp_med, 2),
                "hotels": len(comps),
            }
        )
    if fc.event:
        reasons.append({"code": "event", "name": fc.event})
    if rooms_left == 0:
        reasons.append({"code": "sold_out"})
    elif pickup_now < MIN_PICKUP:
        reasons.append({"code": "no_pickup_expected"})

    lo, hi = current * (1 - policy.max_step), current * (1 + policy.max_step)
    best = int(round(best / policy.step) * policy.step)
    if best > hi:
        best = int(hi // policy.step * policy.step)  # round toward the current rate, never past the cap
        reasons.append({"code": "guardrail_max_step", "limit": best})
    elif best < lo:
        best = int(-(-lo // policy.step) * policy.step)
        reasons.append({"code": "guardrail_max_step", "limit": best})
    return Recommendation(fc.stay, current, best, comp_med, fc, rooms_left, reasons)


def estimate_elasticity(books_pickup: list[tuple[float, float, int, int]]) -> tuple[float, int]:
    """Least squares on log(pickup) against the price index (rate / competitor
    median), with fixed effects for weekday and lead bucket. Input rows are
    (pickup, price_index, weekday, lead_bucket). Returns the elasticity and
    the number of rows used. Days with zero pickup are dropped, which biases
    the estimate a little toward zero; the clamp below keeps it sane."""
    rows = [(math.log(p), idx, w, b) for p, idx, w, b in books_pickup if p > 0 and idx > 0]
    if len(rows) < 50:
        return Policy.elasticity, len(rows)
    y = np.array([r[0] for r in rows])
    cols = [np.array([r[1] for r in rows])]
    for w in range(1, 7):
        cols.append(np.array([1.0 if r[2] == w else 0.0 for r in rows]))
    for b in range(1, 6):
        cols.append(np.array([1.0 if r[3] == b else 0.0 for r in rows]))
    x = np.column_stack([np.ones(len(rows)), *cols])
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    # d log(pickup) / d index; the policy uses premium over its reference.
    return float(np.clip(-coef[1], 0.3, 6.0)), len(rows)
