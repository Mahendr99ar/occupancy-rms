"""Checks on every feed after each cycle, written to `feed_checks` and shown
on the dashboard. A recommendation is only as good as the rates it was
built on, so these run before the pricing engine and the dashboard shows
the latest result next to every number.

    python -m occupancy.pipeline.monitor
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import median

from sqlalchemy import and_, func, select

from .. import config
from ..market.hotels import CLIENT, LISTINGS
from .db import engine, feed_checks, mongo, rate_observations, recommendations, rejects

FRESH_MINUTES = 90
COVERAGE_MIN = 0.9
ANOMALY = 0.35  # a rate this far (as a share) from its own recent median is flagged


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def run(as_of: date | None = None, days_ahead: int = config.CRAWL_DAYS_AHEAD, db=None) -> list[dict]:
    eng = engine()
    out: list[dict] = []

    def add(feed, name, status, value="", detail=""):
        out.append(
            {"feed": feed, "check_name": name, "status": status, "value": str(value)[:40], "detail": detail[:300]}
        )

    with eng.begin() as conn:
        as_of = as_of or conn.execute(select(func.max(rate_observations.c.business_date))).scalar()
        if as_of is None:
            return [
                {
                    "feed": "rates",
                    "check_name": "any_data",
                    "status": "fail",
                    "value": "0",
                    "detail": "no rate observations yet",
                }
            ]
        obs = conn.execute(select(rate_observations).where(rate_observations.c.business_date == as_of)).all()

        # Freshness and coverage, per OTA.
        for ota, hotels in LISTINGS.items():
            rows = [o for o in obs if o.ota == ota]
            expected = len(hotels) * days_ahead
            cov = len(rows) / expected if expected else 0
            add(
                ota,
                "coverage",
                "pass" if cov >= COVERAGE_MIN else "fail",
                f"{cov:.0%}",
                f"{len(rows)} of {expected} hotel-dates observed for {as_of}",
            )
            if rows:
                age = (_now() - max(o.observed_at for o in rows)).total_seconds() / 60
                add(
                    ota,
                    "freshness",
                    "pass" if age <= FRESH_MINUTES else "warn",
                    f"{age:.0f} min",
                    f"newest observation {age:.0f} minutes old",
                )

        # Rates far from the same hotel's recent median for the same lead.
        hist = conn.execute(
            select(
                rate_observations.c.hotel_id,
                rate_observations.c.stay_date,
                rate_observations.c.business_date,
                rate_observations.c.rate,
            ).where(
                and_(
                    rate_observations.c.business_date >= as_of - timedelta(days=8),
                    rate_observations.c.business_date < as_of,
                    rate_observations.c.rate.is_not(None),
                )
            )
        ).all()
        by_lead: dict[tuple[str, int], list[int]] = defaultdict(list)
        for hid, stay, biz, rate in hist:
            by_lead[(hid, (stay - biz).days)].append(rate)
        flagged = []
        for o in obs:
            ref = by_lead.get((o.hotel_id, (o.stay_date - as_of).days))
            if o.rate and ref and len(ref) >= 3 and abs(o.rate / median(ref) - 1) > ANOMALY:
                flagged.append(f"{o.ota}:{o.hotel_id}:{o.stay_date}")
        add("rates", "price_anomalies", "pass" if not flagged else "warn", len(flagged), ", ".join(flagged[:6]))

        # Rate parity: every OTA should sell the client at the rate it loaded.
        mine: dict[date, dict[str, int]] = defaultdict(dict)
        for o in obs:
            if o.hotel_id == CLIENT.id and o.rate:
                mine[o.stay_date][o.ota] = o.rate
        breaks = []
        for stay, by_ota in mine.items():
            top = max(by_ota.values())
            breaks += [f"{ota} {stay} {rate}<{top}" for ota, rate in by_ota.items() if rate < top]
        add(CLIENT.id, "rate_parity", "pass" if not breaks else "warn", len(breaks), "; ".join(breaks[:5]))

        since = _now() - timedelta(days=1)
        rej = conn.execute(
            select(rejects.c.source, rejects.c.reason, func.count())
            .where(rejects.c.at >= since)
            .group_by(rejects.c.source, rejects.c.reason)
        ).all()
        total = sum(n for *_, n in rej)
        add(
            "normalizer",
            "rejected_rows",
            "pass" if total == 0 else "warn",
            total,
            "; ".join(f"{s}: {r} ({n})" for s, r, n in rej[:4]),
        )

        failed = conn.execute(
            select(func.count()).select_from(recommendations).where(recommendations.c.status == "failed")
        ).scalar()
        add("channel_manager", "failed_pushes", "pass" if not failed else "fail", failed)

    # Crawl-level signals the database cannot see: layouts, unreadable images.
    try:
        db = db if db is not None else mongo()
        runs = list(db.crawl_runs.find({"business_date": as_of.isoformat()}))
        stats: dict[str, float] = defaultdict(float)
        for r in runs:
            for k, v in r.get("stats", {}).items():
                if isinstance(v, (int, float)):
                    stats[k] += v
        unknown = stats.get("staykart__layout_unknown", 0)
        add(
            "staykart",
            "layout",
            "pass" if not unknown else "fail",
            "v2" if stats.get("staykart__layout_v2") else "v1" if stats.get("staykart__layout_v1") else "?",
            f"{int(unknown)} pages in an unknown layout",
        )
        unread = stats.get("roombazaar__unreadable_price", 0)
        add(
            "roombazaar",
            "price_images",
            "pass" if not unread else "warn",
            int(unread),
            "prices the reader could not read",
        )
    except Exception as e:  # MongoDB down is itself worth reporting, not crashing on
        add("mongodb", "reachable", "fail", "", str(e)[:200])

    with eng.begin() as conn:
        for c in out:
            conn.execute(feed_checks.insert().values(at=_now(), business_date=as_of, **c))
    return out


if __name__ == "__main__":
    for c in run():
        print(f"{c['status']:4} {c['feed']:15} {c['check_name']:16} {c['value']:>8}  {c['detail']}")
