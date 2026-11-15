"""Does the engine earn more than the hotel's old fixed rates?

Two copies of the same market, same seed, same warm-up. From the test start
one keeps the static weekday/weekend rates and the other lets the engine set
every open stay date each morning (all recommendations applied, as if
approved). Because the simulator keys randomness by date, both see the same
potential guests; only the prices differ.

Revenue is counted for stay dates from test start + HORIZON to the end of
the test: every booking made for them within the engine's 30-day horizon was
priced by the policy under test. Bookings made further out than 30 days are
priced at the static rates in both copies. This is a simulation result, and the
README says so.

    python -m occupancy.intel.evaluate --weeks 12
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from statistics import median

import pandas as pd

from ..market.hotels import BY_ID, CLIENT
from ..market.sim import Config, Market
from . import data
from .engine import COMPSET, HORIZON, plan
from .pricing import Policy


def snapshot(m: Market) -> tuple[data.Books, list[dict], dict, dict]:
    df = pd.DataFrame(
        [
            {
                "booked_on": r.booked_on,
                "arrival": r.arrival,
                "nights": r.nights,
                "cancelled_on": r.cancelled_on,
                "adr": r.adr,
            }
            for r in m.reservations
        ]
    )
    books = data.Books(df, m.turnaways)
    evts = [{"name": e.name, "start": e.start, "end": e.end, "announced": e.announced} for e in m.known_events()]
    stays = [m.today + timedelta(days=i) for i in range(HORIZON)]
    comps = {s: {hid: r for hid in COMPSET if (r := m.comp_rate(BY_ID[hid], s)) is not None} for s in stays}
    current = {s: m.client_rate(s) for s in stays}
    return books, evts, comps, current


def position(m: Market) -> float:
    """Client rate / competitive-set median over the warm-up, which is what
    the engine would estimate from rate shopping."""
    ratios = []
    for back in range(1, 29):
        day = m.today - timedelta(days=back)
        for lead in (3, 10, 20):
            s = day + timedelta(days=lead)
            rates = [r for hid in COMPSET if (r := m.comp_rate(BY_ID[hid], s, day)) is not None]
            if rates:
                ratios.append(m.static_rate(s) / median(rates))
    return float(median(ratios))


def engine_policy(policy: Policy):
    def apply(m: Market) -> None:
        books, evts, comps, current = snapshot(m)
        for rec in plan(m.today, books, evts, comps, current, policy):
            m.set_rate(rec.stay, rec.recommended)

    return apply


def run(weeks: int = 12, warmup: int = 120, seed: int = 7, elasticity: float = 2.0) -> dict:
    cfg = Config(seed=seed)
    static, priced = Market(cfg), Market(cfg)
    static.run(warmup)
    priced.run(warmup)
    policy = Policy(elasticity=elasticity, position=position(priced))
    days = weeks * 7
    static.run(days)
    priced.run(days, policy=engine_policy(policy))
    start = cfg.start + timedelta(days=warmup + HORIZON)
    end = static.today - timedelta(days=1)
    a, b = static.kpis(start, end), priced.kpis(start, end)
    return {
        "seed": seed,
        "stay_dates": f"{start} to {end}",
        "policy": {"elasticity": elasticity, "position": round(policy.position, 3)},
        "static": {k: round(v, 3) for k, v in a.items()},
        "engine": {k: round(v, 3) for k, v in b.items()},
        "revpar_change": round(b["revpar"] / a["revpar"] - 1, 4),
        "rooms": CLIENT.rooms,
    }


def forecast_backtest(seed: int = 7, warmup: int = 120, days: int = 56) -> dict:
    """Forecast error on a static-rate market: every morning for `days`
    days after a long run, forecast stay dates 1..30 days out and compare
    with what they ended with. The market runs 30 days past the last
    forecast so every stay date is final."""
    from .forecast import backtest

    m = Market(Config(seed=seed))
    m.run(warmup + days + 31)
    books, *_ = snapshot(m)
    evts = [{"name": e.name, "start": e.start, "end": e.end, "announced": e.announced} for e in m.events]
    as_of = [m.cfg.start + timedelta(days=warmup + i) for i in range(days)]
    from .forecast import SOLD_OUT

    finals = [books.final(d + timedelta(days=k)) for d in as_of for k in (1, 30)]
    return {
        "seed": seed,
        "forecast_days": f"{as_of[0]} to {as_of[-1]}",
        # Sold-out dates are easy to forecast once capped; say how many there were.
        "sold_out_share": round(sum(f >= SOLD_OUT * CLIENT.rooms for f in finals) / len(finals), 3),
        "by_lead": backtest(books, evts, as_of),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--backtest", action="store_true", help="forecast error instead of the revenue comparison")
    p.add_argument("--weeks", type=int, default=12)
    p.add_argument("--seeds", type=int, nargs="*", default=[7])
    p.add_argument("--elasticity", type=float, default=2.0)
    args = p.parse_args()
    if args.backtest:
        print(json.dumps([forecast_backtest(s) for s in args.seeds], indent=2, default=str))
        return
    print(json.dumps([run(args.weeks, seed=s, elasticity=args.elasticity) for s in args.seeds], indent=2, default=str))


if __name__ == "__main__":
    main()
