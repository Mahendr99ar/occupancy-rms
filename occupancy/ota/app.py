"""The outside world, served over HTTP from one process:

  /staykart     an OTA with server-rendered HTML; its layout changes on a set day
  /tripnest     an OTA whose page loads rates from a paginated JSON API, rate limited
  /roombazaar   an OTA that draws every price as an image
  /pms          the client hotel's property management system (bookings, inbox)
  /cm           the channel manager that takes rate updates
  /be           the hotel's booking engine, to check that a pushed rate is live
  /events       an events listing service
  /admin        clock control for tests and demos

All of it reads one Market. A lock serialises access, since the clock thread
and request threads touch the same state.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from datetime import date, timedelta

import numpy as np
from flask import Blueprint, Flask, Response, abort, jsonify, render_template_string, request

from ..market.hotels import BY_ID, LISTINGS
from ..market.sim import HORIZON, Config, Market, rng
from . import price_image

LAYOUT_CHANGE_AFTER = int(os.environ.get("STAYKART_LAYOUT_CHANGE_AFTER", "5"))  # business days after the app starts
TRIPNEST_PAGE = 3
TRIPNEST_RATE = float(os.environ.get("TRIPNEST_MAX_RPS", "8"))
CM_FAILURE_RATE = float(os.environ.get("CM_FAILURE_RATE", "0.05"))
SECRET = os.environ.get("MARKET_SECRET", "dev-only-secret").encode()


def _stay(arg: str | None) -> date:
    try:
        return date.fromisoformat(arg or "")
    except ValueError:
        abort(400, "checkin must be YYYY-MM-DD")


def create_app(market: Market | None = None, tick_seconds: float | None = None) -> Flask:
    app = Flask(__name__)
    m = market or Market(Config())
    lock = threading.RLock()
    app.config["market"] = m
    app.config["lock"] = lock
    layout_change = m.today + timedelta(days=LAYOUT_CHANGE_AFTER)

    def visible(ota: str, stay: date):
        lead = (stay - m.today).days
        if lead < 0 or lead > HORIZON:
            return []
        out = []
        for hid in LISTINGS[ota]:
            out.append((BY_ID[hid], m.ota_rate(ota, hid, stay)))
        return out

    # ------------------------------------------------------------- StayKart
    sk = Blueprint("staykart", __name__, url_prefix="/staykart")

    @sk.get("/search")
    def staykart_search():
        stay = _stay(request.args.get("checkin"))
        with lock:
            rows = visible("staykart", stay)
            v2 = m.today >= layout_change
        if v2:
            tpl = """<html><body><main id="listing">{% for h, rate in rows %}
<div class="card" data-id="sk-{{ h.id[1:] }}"><h3 class="hotel-title">{{ renamed.get(h.id, h.ota_names['staykart']) }}</h3>
<p class="stars">{{ h.stars }}-star</p>{% if rate %}<span class="amount" data-currency="INR">{{ rate }}</span>
{% else %}<span class="amount sold-out">Sold out</span>{% endif %}</div>{% endfor %}</main></body></html>"""
        else:
            tpl = """<html><body><table class="results"><tr><th>Hotel</th><th>Price</th></tr>
{% for h, rate in rows %}<tr><td class="name">{{ h.ota_names['staykart'] }}</td>
<td class="price">{% if rate %}&#8377; {{ "{:,}".format(rate) }}{% else %}Sold out{% endif %}</td></tr>{% endfor %}
</table></body></html>"""
        # The redesign also renamed one listing; the normalizer has to cope.
        return render_template_string(tpl, rows=rows, renamed={"H01": "Hotel Creekside Inn, Vashi"})

    # ------------------------------------------------------------- TripNest
    tn = Blueprint("tripnest", __name__, url_prefix="/tripnest")
    buckets: dict[str, list[float]] = {}

    @tn.get("/hotels")
    def tripnest_page():
        # The page itself has no prices; a browser would fetch them with JS.
        return (
            "<html><body><div id='app' data-api='/tripnest/api/rates'></div>"
            "<script src='/tripnest/static/app.js'></script></body></html>"
        )

    @tn.get("/api/rates")
    def tripnest_rates():
        now = time.monotonic()
        who = request.remote_addr or "?"
        recent = [t for t in buckets.get(who, []) if now - t < 1.0]
        if len(recent) >= TRIPNEST_RATE:
            return jsonify(error="rate limited"), 429, {"Retry-After": "1"}
        buckets[who] = [*recent, now]
        stay = _stay(request.args.get("date"))
        page = int(request.args.get("page", 1))
        with lock:
            rows = visible("tripnest", stay)
        chunk = rows[(page - 1) * TRIPNEST_PAGE : page * TRIPNEST_PAGE]
        data = [
            {
                "property": h.ota_names["tripnest"],
                "nightly": {"amount": rate, "currency": "INR"} if rate else None,
                "available": rate is not None,
            }
            for h, rate in chunk
        ]
        more = page * TRIPNEST_PAGE < len(rows)
        return jsonify(results=data, page=page, next_page=page + 1 if more else None)

    # ----------------------------------------------------------- RoomBazaar
    rb = Blueprint("roombazaar", __name__, url_prefix="/roombazaar")

    def token(hid: str, stay: date, on: date) -> str:
        msg = f"{hid}|{stay.isoformat()}|{on.isoformat()}".encode()
        return msg.hex() + "." + hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:16]

    @rb.get("/listing")
    def roombazaar_listing():
        stay = _stay(request.args.get("date"))
        with lock:
            rows = visible("roombazaar", stay)
            on = m.today
        tpl = """<html><body><section class="hotels">{% for h, rate in rows %}
<article class="hotel"><div class="hn">{{ h.ota_names['roombazaar'] }}</div>
{% if rate %}<img class="px" src="/roombazaar/p/{{ tok(h.id) }}.png" alt="price">{% else %}<div class="na">Not available</div>{% endif %}
</article>{% endfor %}</section></body></html>"""
        return render_template_string(tpl, rows=rows, tok=lambda hid: token(hid, stay, on))

    @rb.get("/p/<tok>.png")
    def roombazaar_price(tok: str):
        try:
            raw, sig = tok.split(".")
            msg = bytes.fromhex(raw)
        except ValueError:
            abort(404)
        if not hmac.compare_digest(sig, hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:16]):
            abort(404)
        hid, stay_s, on_s = msg.decode().split("|")
        stay, on = date.fromisoformat(stay_s), date.fromisoformat(on_s)
        with lock:
            rate = m.ota_rate("roombazaar", hid, stay, on)
        if rate is None:
            abort(404)
        r = rng(m.cfg.seed, "priceimg", hid, stay.toordinal(), on.toordinal())
        return Response(price_image.png(price_image.render(price_image.price_text(rate), r)), mimetype="image/png")

    # ------------------------------------------------------------------ PMS
    pms = Blueprint("pms", __name__, url_prefix="/pms")

    @pms.get("/changes")
    def pms_changes():
        after = int(request.args.get("after", 0))
        limit = min(int(request.args.get("limit", 500)), 2000)
        with lock:
            ids = m.changes[after : after + limit]
            items = [{**m.reservation(i).public(), "seq": after + n + 1} for n, i in enumerate(ids)]
            total = len(m.changes)
        return jsonify(items=items, last_seq=after + len(items), has_more=after + len(items) < total)

    @pms.get("/turnaways")
    def pms_turnaways():
        after = int(request.args.get("after", 0))
        limit = min(int(request.args.get("limit", 2000)), 5000)
        with lock:
            rows = m.turnaways[after : after + limit]
            total = len(m.turnaways)
        items = [
            {"seq": after + n + 1, "day": d.isoformat(), "stay_date": s.isoformat()} for n, (d, s) in enumerate(rows)
        ]
        return jsonify(items=items, last_seq=after + len(items), has_more=after + len(items) < total)

    @pms.get("/inventory")
    def pms_inventory():
        stay = _stay(request.args.get("date"))
        with lock:
            return jsonify(date=stay.isoformat(), rooms=BY_ID["H00"].rooms, sold=m.rooms_sold(stay))

    @pms.get("/inbox")
    def pms_inbox():
        after = request.args.get("after", "")
        with lock:
            msgs = [{k: v for k, v in msg.items() if k != "truth"} for msg in m.inbox if msg["id"] > after]
        return jsonify(messages=msgs)

    # ------------------------------------------------------ channel manager
    cm = Blueprint("cm", __name__, url_prefix="/cm")
    applied: dict[str, dict] = {}

    @cm.post("/rates")
    def cm_rates():
        key = request.headers.get("Idempotency-Key")
        if not key:
            return jsonify(error="Idempotency-Key header required"), 400
        if key in applied:
            return jsonify(applied[key]), 200
        body = request.get_json(silent=True) or {}
        try:
            stay = date.fromisoformat(body["stay_date"])
            rate = int(body["rate"])
        except (KeyError, ValueError, TypeError):
            return jsonify(error="stay_date and rate are required"), 422
        if body.get("hotel_id") != "H00":
            return jsonify(error="unknown hotel"), 404
        if not 1000 <= rate <= 50000:
            return jsonify(error="rate outside 1000..50000"), 422
        if np.random.default_rng().random() < CM_FAILURE_RATE:
            return jsonify(error="upstream timeout"), 503
        with lock:
            m.set_rate(stay, rate)
        result = {
            "confirmation": "CM-" + key[:12],
            "stay_date": stay.isoformat(),
            "rate": rate,
            "channels": ["booking_engine", "staykart", "tripnest", "roombazaar"],
        }
        applied[key] = result
        return jsonify(result), 200

    # ------------------------------------------------------- booking engine
    be = Blueprint("be", __name__, url_prefix="/be")

    @be.get("/availability")
    def be_availability():
        stay = _stay(request.args.get("date"))
        with lock:
            sold = m.rooms_sold(stay)
            return jsonify(date=stay.isoformat(), rate=m.client_rate(stay), available=BY_ID["H00"].rooms - sold)

    # --------------------------------------------------------------- events
    @app.get("/events")
    def events():
        with lock:
            items = [
                {
                    "name": e.name,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "announced": e.announced.isoformat(),
                }
                for e in m.known_events()
            ]
        return jsonify(events=items)

    # ---------------------------------------------------------------- admin
    @app.get("/admin/clock")
    def clock():
        with lock:
            return jsonify(today=m.today.isoformat(), start=m.cfg.start.isoformat())

    @app.post("/admin/advance")
    def advance():
        days = int(request.args.get("days", 1))
        with lock:
            log = m.run(days)
        return jsonify(today=m.today.isoformat(), days=log)

    @app.get("/admin/inbox-truth")
    def inbox_truth():
        with lock:
            return jsonify(messages=list(m.inbox))

    for bp in (sk, tn, rb, pms, cm, be):
        app.register_blueprint(bp)

    if tick_seconds:

        def ticker():
            while True:
                time.sleep(tick_seconds)
                with lock:
                    m.step()

        threading.Thread(target=ticker, daemon=True).start()
    return app


def main() -> None:
    warmup = int(os.environ.get("MARKET_WARMUP_DAYS", "120"))
    m = Market(Config(seed=int(os.environ.get("MARKET_SEED", "7"))))
    m.run(warmup)
    tick = float(os.environ.get("TICK_SECONDS", "0")) or None
    create_app(m, tick).run(host="0.0.0.0", port=int(os.environ.get("PORT", "8500")), threaded=True)


if __name__ == "__main__":
    main()
