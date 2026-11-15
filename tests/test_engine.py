from datetime import date, timedelta

import pandas as pd

from occupancy.intel import data
from occupancy.intel.forecast import Forecast, forecast, pickup_table
from occupancy.intel.pricing import Policy, recommend


def books(rows, turnaways=None):
    df = pd.DataFrame(rows, columns=["booked_on", "arrival", "nights", "cancelled_on", "adr"])
    return data.Books(df, turnaways)


D = date(2026, 10, 1)


def test_on_books_respects_booking_and_cancellation_days():
    b = books([(D, D + timedelta(days=5), 2, None, 6000), (D, D + timedelta(days=5), 1, D + timedelta(days=2), 6000)])
    stay = D + timedelta(days=5)
    assert b.otb(stay, D) == 2 and b.otb(stay, D + timedelta(days=2)) == 1
    assert b.otb(stay + timedelta(days=1), D) == 1  # second night of the 2-night stay


def test_turnaways_count_as_demand_in_the_pickup():
    stay = D + timedelta(days=14)
    rows = [(stay - timedelta(days=3), stay, 1, None, 6000)] * 2
    plain = pickup_table(books(rows), stay + timedelta(days=1), [], max_lead=3)
    denied = pickup_table(books(rows, [(stay - timedelta(days=2), stay)] * 5), stay + timedelta(days=1), [], max_lead=3)
    key = (stay.weekday(), 3, False)
    assert denied[key][0] > plain[key][0]


def test_forecast_adds_pickup_to_what_is_on_the_books():
    stay = D + timedelta(days=21)
    rows = [(stay - timedelta(days=5), stay, 1, None, 6000)] * 3
    b = books(rows)
    fc = forecast(b, stay - timedelta(days=7), [stay], [], {(stay.weekday(), 7, False): (10.0, 6000.0)})[stay]
    assert fc.on_books == 0 and fc.rooms == 10.0


def fc(otb, pickup, lead=10):
    return Forecast(D, lead, otb, pickup, None, 6800)


def test_raises_when_demand_exceeds_rooms_left():
    r = recommend(fc(100, 60), 6800, {"a": 6500, "b": 7000}, Policy(position=1.0))
    assert r.recommended > 6800


def test_holds_when_nothing_is_left_to_sell_into():
    r = recommend(fc(40, 0.1, lead=0), 6800, {"a": 6500}, Policy(position=1.0))
    assert r.recommended == 6800 and any(x["code"] == "no_pickup_expected" for x in r.reasons)


def test_one_day_move_is_capped():
    r = recommend(fc(119, 300), 6800, {"a": 9000}, Policy(position=1.0, max_step=0.1))
    assert r.recommended <= 7480 and any(x["code"] == "guardrail_max_step" for x in r.reasons)
