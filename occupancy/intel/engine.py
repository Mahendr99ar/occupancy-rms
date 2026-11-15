"""The daily pricing run, and pushing approved rates to the channel manager.

    python -m occupancy.intel.engine recommend   # writes pending recommendations
    python -m occupancy.intel.engine push        # sends approved ones

`plan` is the pure part: given what is known on a day, it returns
recommendations. The revenue evaluation calls it directly on simulator
data; `recommend` calls it on what the pipeline stored in MySQL.
"""

from __future__ import annotations

import argparse
import hashlib
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from statistics import median

import requests
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.mysql import insert

from .. import config
from ..market.hotels import CLIENT
from ..ml.compset import similar
from ..pipeline.db import engine, feed_checks, rate_observations, recommendations
from . import data
from .explain import explain
from .forecast import forecast, pickup_table
from .pricing import Policy, Recommendation, recommend

HORIZON = 30
COMPSET = [hid for hid, _ in similar(k=5)]


def plan(
    as_of: date,
    books: data.Books,
    events: list[dict],
    comps: dict[date, dict[str, int]],
    current: dict[date, int],
    policy: Policy,
    horizon: int = HORIZON,
) -> list[Recommendation]:
    stays = [as_of + timedelta(days=i) for i in range(horizon)]
    table = pickup_table(books, as_of, events)
    fc = forecast(books, as_of, stays, events, table)
    return [recommend(fc[s], current[s], comps.get(s, {}), policy) for s in stays]


def estimate_position(conn, as_of: date, days: int = 28) -> float | None:
    """The client's usual rate relative to its competitive set's median,
    from the last `days` business days of rate shopping."""
    q = select(
        rate_observations.c.business_date,
        rate_observations.c.stay_date,
        rate_observations.c.hotel_id,
        rate_observations.c.rate,
    ).where(
        and_(
            rate_observations.c.business_date > as_of - timedelta(days=days),
            rate_observations.c.business_date <= as_of,
            rate_observations.c.rate.is_not(None),
            rate_observations.c.hotel_id.in_([CLIENT.id, *COMPSET]),
        )
    )
    by_key: dict[tuple, dict[str, list[int]]] = {}
    for biz, stay, hid, rate in conn.execute(q):
        by_key.setdefault((biz, stay), {}).setdefault(hid, []).append(rate)
    ratios = []
    for hotels in by_key.values():
        mine = hotels.pop(CLIENT.id, None)
        if mine and hotels:
            ratios.append(max(mine) / median(median(v) for v in hotels.values()))
    return round(float(median(ratios)), 3) if ratios else None


def current_rates(conn, as_of: date, stays: list[date]) -> dict[date, int]:
    """What the client sells each stay date for right now: the last rate the
    engine pushed, else the rate most OTAs showed today, else the default.
    The most common OTA rate is used rather than the lowest so that one OTA
    discounting the hotel (a parity break) does not set the baseline."""
    pushed = {
        r.stay_date: r.recommended_rate
        for r in conn.execute(
            select(recommendations.c.stay_date, recommendations.c.recommended_rate)
            .where(and_(recommendations.c.status == "pushed", recommendations.c.stay_date.in_(stays)))
            .order_by(recommendations.c.business_date)
        )
    }
    seen: dict[date, list[int]] = {}
    for (stay, _ota), rate in data.client_ota_rates(conn, as_of).items():
        if rate:
            seen.setdefault(stay, []).append(rate)
    out = {}
    for s in stays:
        if s in pushed:
            out[s] = pushed[s]
        elif s in seen:
            out[s] = Counter(seen[s]).most_common(1)[0][0]
        else:
            out[s] = CLIENT.base_rate if s.weekday() < 4 else int(CLIENT.base_rate * 0.85)
    return out


def latest_business_date(conn) -> date | None:
    return conn.execute(select(func.max(rate_observations.c.business_date))).scalar()


def run_recommend(as_of: date | None = None, auto_within: float = 0.0, policy: Policy | None = None) -> dict:
    eng = engine()
    with eng.begin() as conn:
        as_of = as_of or latest_business_date(conn)
        if as_of is None:
            return {"error": "no rate observations yet"}
        policy = policy or Policy()
        pos = estimate_position(conn, as_of)
        if pos:
            policy.position = pos
        books = data.load_books(conn)
        evts = data.known_events(conn, as_of)
        stays = [as_of + timedelta(days=i) for i in range(HORIZON)]
        comps = data.comp_rates(conn, as_of, stays, COMPSET)
        recs = plan(as_of, books, evts, comps, current_rates(conn, as_of, stays), policy)
        written = auto = 0
        for rec in recs:
            text, _src = explain(rec)
            small = abs(rec.change) <= auto_within and rec.forecast.event is None
            status = "approved" if small and rec.recommended != rec.current else "pending"
            if rec.recommended == rec.current:
                status = "no_change"
            stmt = insert(recommendations).values(
                business_date=as_of,
                stay_date=rec.stay,
                current_rate=rec.current,
                recommended_rate=rec.recommended,
                comp_median=rec.comp_median,
                otb=rec.forecast.on_books,
                forecast_rooms=round(rec.forecast.rooms),
                reasons=rec.reasons,
                explanation=text,
                status=status,
                decided_by="auto" if status == "approved" else None,
            )
            # Re-running the same day refreshes undecided rows only.
            stmt = stmt.on_duplicate_key_update(
                recommended_rate=func.if_(
                    recommendations.c.status == "pending",
                    stmt.inserted.recommended_rate,
                    recommendations.c.recommended_rate,
                ),
                explanation=func.if_(
                    recommendations.c.status == "pending", stmt.inserted.explanation, recommendations.c.explanation
                ),
            )
            conn.execute(stmt)
            written += 1
            auto += status == "approved"
    return {
        "as_of": as_of.isoformat(),
        "recommendations": written,
        "auto_approved": auto,
        "position": policy.position,
        "elasticity": policy.elasticity,
    }


def idempotency_key(business_date: date, stay: date, rate: int) -> str:
    return hashlib.sha256(f"{CLIENT.id}|{business_date}|{stay}|{rate}".encode()).hexdigest()[:32]


def push_one(session: requests.Session, row, market_url: str, attempts: int = 5) -> dict:
    """POST to the channel manager, retrying timeouts and 5xx with
    exponential backoff. The idempotency key is derived from the decision,
    so a retry after a lost response cannot apply the rate twice."""
    key = idempotency_key(row.business_date, row.stay_date, row.recommended_rate)
    body = {"hotel_id": CLIENT.id, "stay_date": row.stay_date.isoformat(), "rate": row.recommended_rate}
    for n in range(attempts):
        try:
            r = session.post(f"{market_url}/cm/rates", json=body, headers={"Idempotency-Key": key}, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code < 500:
                raise RuntimeError(f"channel manager refused: {r.status_code} {r.text[:200]}")
        except requests.RequestException:
            pass
        time.sleep(0.2 * 2**n)
    raise RuntimeError("channel manager unavailable after retries")


def run_push(market_url: str = config.MARKET_URL) -> dict:
    eng, sess = engine(), requests.Session()
    pushed = failed = mismatched = 0
    with eng.begin() as conn:
        rows = conn.execute(select(recommendations).where(recommendations.c.status == "approved")).all()
    for row in rows:
        try:
            result = push_one(sess, row, market_url)
        except RuntimeError as e:
            failed += 1
            with eng.begin() as conn:
                conn.execute(recommendations.update().where(recommendations.c.id == row.id).values(status="failed"))
                _check(conn, row.business_date, "channel_manager", "push", "fail", str(row.stay_date), str(e))
            continue
        # Check the rate is what the booking engine now sells.
        live = sess.get(f"{market_url}/be/availability", params={"date": row.stay_date.isoformat()}, timeout=10).json()
        ok = live.get("rate") == row.recommended_rate
        pushed += 1
        mismatched += not ok
        with eng.begin() as conn:
            conn.execute(
                recommendations.update()
                .where(recommendations.c.id == row.id)
                .values(
                    status="pushed",
                    pushed_at=datetime.now(UTC).replace(tzinfo=None),
                    confirmation=result["confirmation"],
                )
            )
            if not ok:
                _check(
                    conn,
                    row.business_date,
                    "booking_engine",
                    "rate_live",
                    "fail",
                    str(row.stay_date),
                    f"pushed {row.recommended_rate}, booking engine shows {live.get('rate')}",
                )
    return {"pushed": pushed, "failed": failed, "not_live_after_push": mismatched}


def _check(conn, biz, feed, name, status, value, detail):
    conn.execute(
        feed_checks.insert().values(
            at=datetime.now(UTC).replace(tzinfo=None),
            business_date=biz,
            feed=feed,
            check_name=name,
            status=status,
            value=value,
            detail=detail[:300],
        )
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=("recommend", "push"))
    p.add_argument("--auto-within", type=float, default=0.0, help="auto-approve changes up to this share, e.g. 0.05")
    args = p.parse_args()
    print(run_recommend(auto_within=args.auto_within) if args.cmd == "recommend" else run_push())


if __name__ == "__main__":
    main()
