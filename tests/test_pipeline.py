"""Needs MySQL and Redis (docker compose up mysql redis). Uses the
occupancy_test database and Redis db 1."""

import json

from sqlalchemy import select

from occupancy.pipeline import normalize
from occupancy.pipeline.db import hotel_aliases, rate_observations, rejects, reservations

from .conftest import needs_mysql, needs_redis

pytestmark = [needs_mysql, needs_redis]


def publish(r, stream, *rows):
    for row in rows:
        r.xadd(stream, {"data": json.dumps(row)})


def rate(alias, value, **kw):
    return {
        "ota": "staykart",
        "alias": alias,
        "stay_date": "2026-10-10",
        "business_date": "2026-10-01",
        "rate": value,
        "raw_id": "r1",
        **kw,
    }


def booking(seq, status, **kw):
    return {
        "seq": seq,
        "id": "R1",
        "status": status,
        "booked_on": "2026-09-20",
        "arrival": "2026-10-10",
        "nights": 2,
        "adr": 6800,
        "channel": "staykart",
        "deposit_type": "No Deposit",
        "adults": 2,
        "children": 0,
        "special_requests": 0,
        "is_repeated_guest": 0,
        "previous_cancellations": 0,
        "market_segment": "Online TA",
        "cancelled_on": "2026-09-25" if status == "cancelled" else None,
        **kw,
    }


def test_replayed_messages_do_not_duplicate_rows(clean_db, clean_redis):
    publish(clean_redis, "rates", rate("Creekside Inn", 4300), rate("Creekside Inn", 4300), rate("Creekside Inn", 4400))
    normalize.run(once=True)
    with clean_db.connect() as c:
        rows = c.execute(select(rate_observations.c.rate)).all()
    assert rows == [(4400,)]


def test_an_older_change_cannot_undo_a_cancellation(clean_db, clean_redis):
    publish(clean_redis, "pms", booking(1, "confirmed"), booking(2, "cancelled"), booking(1, "confirmed"))
    normalize.run(once=True)
    with clean_db.connect() as c:
        status, seq = c.execute(select(reservations.c.status, reservations.c.pms_seq)).one()
    assert (status, seq) == ("cancelled", 2)


def test_a_slightly_different_name_is_matched_and_remembered(clean_db, clean_redis):
    publish(clean_redis, "rates", rate("Creekside Inn Vashi", 4300))
    normalize.run(once=True)
    with clean_db.connect() as c:
        hid = c.execute(select(rate_observations.c.hotel_id)).scalar_one()
        learned = c.execute(
            select(hotel_aliases.c.source).where(hotel_aliases.c.alias == "Creekside Inn Vashi")
        ).scalar()
    assert hid == "H01" and learned == "fuzzy"


def test_bad_rows_go_to_rejects_not_the_table(clean_db, clean_redis):
    publish(
        clean_redis,
        "rates",
        rate("Creekside Inn", 50),
        rate("Totally Unknown Hotel", 4300),
        rate("Creekside Inn", 4300, stay_date="not a date"),
    )
    normalize.run(once=True)
    with clean_db.connect() as c:
        assert c.execute(select(rate_observations)).all() == []
        reasons = sorted(r for (r,) in c.execute(select(rejects.c.reason)))
    assert len(reasons) == 3 and reasons[0] == "bad dates"


def test_unacknowledged_messages_are_reclaimed(clean_db, clean_redis, monkeypatch):
    r = clean_redis
    normalize.ensure_groups(r)
    publish(r, "rates", rate("Creekside Inn", 4300))
    r.xreadgroup(normalize.GROUP, "crashed-worker", {"rates": ">"}, count=10)  # read, never acked
    real = r.xautoclaim
    monkeypatch.setattr(type(r), "xautoclaim", lambda self, *a, **kw: real(*a, **{**kw, "min_idle_time": 0}))
    assert normalize.run(once=True) == 1
    assert r.xpending("rates", normalize.GROUP)["pending"] == 0
