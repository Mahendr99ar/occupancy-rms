from datetime import timedelta

from occupancy.market.hotels import CLIENT
from occupancy.market.sim import Config, Market


def test_same_seed_same_market():
    a, b = Market(Config(seed=3)), Market(Config(seed=3))
    a.run(20)
    b.run(20)
    assert [r.public() for r in a.reservations] == [r.public() for r in b.reservations]


def test_higher_rate_sells_fewer_rooms_from_the_same_guests():
    cheap, dear = Market(Config(seed=5)), Market(Config(seed=5))
    for m, factor in ((cheap, 0.85), (dear, 1.25)):
        for i in range(70):
            d = m.today + timedelta(days=i)
            m.set_rate(d, int(m.static_rate(d) * factor))
        m.run(30)
    sold = lambda m: sum(m.rooms_sold(m.cfg.start + timedelta(days=i)) for i in range(30))  # noqa: E731
    assert sold(cheap) > sold(dear)


def test_never_sells_more_rooms_than_it_has_and_logs_turnaways():
    m = Market(Config(seed=1, base_demand=400))
    m.run(30)
    assert max(m.rooms_sold(m.cfg.start + timedelta(days=i)) for i in range(60)) <= CLIENT.rooms
    assert m.turnaways, "a hotel this oversubscribed must turn guests away"


def test_cancellations_free_the_room():
    m = Market(Config(seed=2))
    m.run(40)
    cancelled = [r for r in m.reservations if r.status == "cancelled"]
    assert cancelled
    r = cancelled[0]
    live = sum(1 for x in m.reservations if x.status == "confirmed" and r.arrival in x.stay_dates())
    assert m.rooms_sold(r.arrival) == live


def test_events_are_only_known_after_announcement():
    m = Market()
    e = m.events[1]
    assert e not in m.known_events(e.announced - timedelta(days=1))
    assert e in m.known_events(e.announced)
