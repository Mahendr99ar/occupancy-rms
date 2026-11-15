from datetime import date

from sqlalchemy import select

from occupancy.api import app as api
from occupancy.pipeline.db import recommendations

from .conftest import needs_mysql

pytestmark = needs_mysql
AUTH = {"Authorization": "Bearer test-token"}


def add_rec(eng):
    with eng.begin() as c:
        return c.execute(
            recommendations.insert().values(
                business_date=date(2026, 10, 1),
                stay_date=date(2026, 10, 9),
                current_rate=6800,
                recommended_rate=7400,
                otb=80,
                forecast_rooms=110,
                reasons=[],
                status="pending",
            )
        ).inserted_primary_key[0]


def test_decisions_need_the_token(clean_db):
    rid = add_rec(clean_db)
    client = api.create_app().test_client()
    assert client.post(f"/api/recommendations/{rid}/decision", json={"decision": "approve"}).status_code == 401
    bad = {"Authorization": "Bearer test-tokem"}
    assert (
        client.post(f"/api/recommendations/{rid}/decision", json={"decision": "approve"}, headers=bad).status_code
        == 401
    )


def test_approve_pushes_once_and_records_the_confirmation(clean_db, monkeypatch):
    rid = add_rec(clean_db)
    pushed = []
    monkeypatch.setattr(
        api, "push_one", lambda s, row, url: pushed.append(row.recommended_rate) or {"confirmation": "CM-1"}
    )
    client = api.create_app().test_client()
    res = client.post(f"/api/recommendations/{rid}/decision", json={"decision": "approve", "rate": 7300}, headers=AUTH)
    assert res.json == {"id": rid, "status": "pushed", "confirmation": "CM-1"} and pushed == [7300]
    again = client.post(f"/api/recommendations/{rid}/decision", json={"decision": "approve"}, headers=AUTH)
    assert again.status_code == 409 and pushed == [7300]


def test_an_override_far_from_the_base_rate_is_refused(clean_db):
    rid = add_rec(clean_db)
    client = api.create_app().test_client()
    res = client.post(f"/api/recommendations/{rid}/decision", json={"decision": "approve", "rate": 680}, headers=AUTH)
    assert res.status_code == 422
    with clean_db.connect() as c:
        assert c.execute(select(recommendations.c.status)).scalar() == "pending"
