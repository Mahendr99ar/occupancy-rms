import re
from datetime import timedelta

import pytest

from occupancy.market.sim import Config, Market
from occupancy.ota import app as ota_app


@pytest.fixture
def client():
    m = Market(Config(seed=4))
    m.run(10)
    app = ota_app.create_app(m)
    return app.test_client(), m


def stay(m, days=3):
    return (m.today + timedelta(days=days)).isoformat()


def test_staykart_changes_layout_after_the_configured_day(client):
    c, m = client
    assert b'table class="results"' in c.get(f"/staykart/search?checkin={stay(m)}").data
    c.post(f"/admin/advance?days={ota_app.LAYOUT_CHANGE_AFTER}")
    html = c.get(f"/staykart/search?checkin={stay(m)}").data
    assert b'class="card"' in html and b"Hotel Creekside Inn, Vashi" in html


def test_tripnest_paginates_and_rate_limits(client, monkeypatch):
    c, m = client
    first = c.get(f"/tripnest/api/rates?date={stay(m)}&page=1").json
    assert len(first["results"]) == 3 and first["next_page"] == 2
    codes = [c.get(f"/tripnest/api/rates?date={stay(m)}&page=1").status_code for _ in range(20)]
    assert 429 in codes


def test_roombazaar_price_images_are_signed(client):
    c, m = client
    html = c.get(f"/roombazaar/listing?date={stay(m)}").data.decode()
    src = re.findall(r'src="([^"]+)"', html)[0]
    ok = c.get(src)
    assert ok.status_code == 200 and ok.mimetype == "image/png"
    tampered = src.replace(".png", "").rsplit(".", 1)[0] + ".0000000000000000.png"
    assert c.get(tampered).status_code == 404


def test_channel_manager_is_idempotent_and_validates(client):
    c, m = client
    d = stay(m, 5)
    body = {"hotel_id": "H00", "stay_date": d, "rate": 7350}
    assert c.post("/cm/rates", json=body).status_code == 400  # no key
    first = c.post("/cm/rates", json=body, headers={"Idempotency-Key": "k1"}).json
    again = c.post("/cm/rates", json={**body, "rate": 9999}, headers={"Idempotency-Key": "k1"}).json
    assert first == again and m.client_rate(m.today + timedelta(days=5)) == 7350
    assert c.post("/cm/rates", json={**body, "rate": 50}, headers={"Idempotency-Key": "k2"}).status_code == 422
    assert c.get(f"/be/availability?date={d}").json["rate"] == 7350


def test_pms_changes_page_through_every_booking_once(client):
    c, m = client
    seen, after = [], 0
    while True:
        page = c.get(f"/pms/changes?after={after}&limit=250").json
        seen += [i["seq"] for i in page["items"]]
        after = page["last_seq"]
        if not page["has_more"]:
            break
    assert seen == list(range(1, len(m.changes) + 1))
    assert "truth" not in c.get("/pms/inbox").json["messages"][0]
