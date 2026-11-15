"""The revenue manager's API and dashboard (Flask).

    flask --app occupancy.api.app run --port 8000

Read endpoints are open on the internal network; anything that changes a
rate needs `Authorization: Bearer <API_TOKEN>`.
"""

from __future__ import annotations

import hmac
from datetime import UTC, date, datetime
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from sqlalchemy import select

from .. import config
from ..intel import kpis
from ..intel.engine import latest_business_date, push_one
from ..market.hotels import BY_ID, CLIENT
from ..ml.compset import similar
from ..pipeline.db import engine, recommendations

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"


def _authorized() -> bool:
    if config.API_TOKEN in ("", "change-me"):
        return False  # an unset token must not mean anyone can push rates
    got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(got) and hmac.compare_digest(got, config.API_TOKEN)


def _as_of(conn) -> date:
    arg = request.args.get("as_of")
    if arg:
        try:
            return date.fromisoformat(arg)
        except ValueError:
            abort(400, "as_of must be YYYY-MM-DD")
    d = latest_business_date(conn)
    if d is None:
        abort(503, "no data yet: run a crawl and the normalizer first")
    return d


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)  # the dashboard folder is served below

    @app.get("/")
    def index():
        return send_from_directory(DASHBOARD, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(DASHBOARD, name)

    @app.get("/api/overview")
    def overview():
        with engine().connect() as conn:
            as_of = _as_of(conn)
            return jsonify(
                hotel={"id": CLIENT.id, "name": CLIENT.name, "rooms": CLIENT.rooms}, **kpis.overview(conn, as_of)
            )

    @app.get("/api/calendar")
    def calendar():
        with engine().connect() as conn:
            return jsonify(days=kpis.calendar(conn, _as_of(conn)))

    @app.get("/api/feeds")
    def feeds():
        with engine().connect() as conn:
            return jsonify(checks=kpis.latest_checks(conn))

    @app.get("/api/compset")
    def compset():
        return jsonify(
            hotels=[
                {"id": hid, "name": BY_ID[hid].name, "stars": BY_ID[hid].stars, "similarity": sim}
                for hid, sim in similar(k=8)
            ]
        )

    @app.get("/api/requests")
    def rate_requests():
        with engine().connect() as conn:
            return jsonify(requests=kpis.requests_list(conn))

    @app.get("/api/at-risk")
    def at_risk():
        with engine().connect() as conn:
            arrivals = kpis.upcoming_arrivals(conn, _as_of(conn))
        payload = [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
            for row in arrivals[
                [
                    "id",
                    "booked_on",
                    "arrival",
                    "nights",
                    "adr",
                    "adults",
                    "children",
                    "special_requests",
                    "is_repeated_guest",
                    "previous_cancellations",
                    "deposit_type",
                    "market_segment",
                    "channel",
                ]
            ].to_dict("records")
        ]
        try:
            r = requests.post(
                f"{config.MODELS_URL}/cancel-risk",
                json=[{k: v for k, v in p.items() if k != "channel"} for p in payload],
                timeout=15,
            )
            r.raise_for_status()
            risk = r.json()["risk"]
        except requests.RequestException:
            from ..ml.cancellation import CancelModel

            probs = CancelModel().predict(arrivals)
            risk = {i: round(float(p), 3) for i, p in zip(arrivals.id, probs, strict=True)}
        rows = sorted(({**p, "risk": risk.get(p["id"])} for p in payload), key=lambda p: -(p["risk"] or 0))
        expected = sum(p["risk"] * p["nights"] for p in rows if p["risk"] is not None)
        return jsonify(bookings=rows[:40], expected_cancelled_room_nights=round(expected, 1), total=len(rows))

    @app.post("/api/recommendations/<int:rec_id>/decision")
    def decide(rec_id: int):
        if not _authorized():
            abort(401)
        body = request.get_json(silent=True) or {}
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            abort(400, "decision must be approve or reject")
        eng = engine()
        with eng.begin() as conn:
            row = conn.execute(select(recommendations).where(recommendations.c.id == rec_id).with_for_update()).first()
            if row is None:
                abort(404)
            if row.status != "pending":
                return jsonify(error=f"already {row.status}"), 409
            values = {"status": "approved" if decision == "approve" else "rejected", "decided_by": "revenue_manager"}
            if decision == "approve" and body.get("rate") is not None:
                rate = int(body["rate"])
                if not (0.5 * CLIENT.base_rate <= rate <= 2.0 * CLIENT.base_rate):
                    abort(422, "rate outside 50%..200% of the base rate")
                values["recommended_rate"] = rate
            conn.execute(recommendations.update().where(recommendations.c.id == rec_id).values(**values))
        if decision == "reject":
            return jsonify(id=rec_id, status="rejected")
        with eng.begin() as conn:
            row = conn.execute(select(recommendations).where(recommendations.c.id == rec_id)).first()
        try:
            result = push_one(requests.Session(), row, config.MARKET_URL)
        except RuntimeError as e:
            with eng.begin() as conn:
                conn.execute(recommendations.update().where(recommendations.c.id == rec_id).values(status="failed"))
            return jsonify(id=rec_id, status="failed", error=str(e)), 502
        with eng.begin() as conn:
            conn.execute(
                recommendations.update()
                .where(recommendations.c.id == rec_id)
                .values(
                    status="pushed",
                    pushed_at=datetime.now(UTC).replace(tzinfo=None),
                    confirmation=result["confirmation"],
                )
            )
        return jsonify(id=rec_id, status="pushed", confirmation=result["confirmation"])

    @app.get("/api/reports/weekly.xlsx")
    def report_xlsx():
        from ..reports.excel import build

        with engine().connect() as conn:
            path = build(conn, _as_of(conn))
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get("/api/reports/weekly.pptx")
    def report_pptx():
        from ..reports.slides import build

        with engine().connect() as conn:
            path = build(conn, _as_of(conn))
        return send_file(path, as_attachment=True, download_name=path.name)

    return app


app = create_app()
