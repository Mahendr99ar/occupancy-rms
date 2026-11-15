"""Turns stream messages into clean MySQL rows.

    python -m occupancy.pipeline.normalize        # runs until stopped
    python -m occupancy.pipeline.normalize --once # drains the streams, then exits

Reads four Redis streams as one consumer group. A message is acknowledged
only after its rows are committed, so a crash leaves it pending and the next
start claims it again. Every write is an upsert keyed on the business
identity of the row, which is what makes that replay harmless.

Bad input never stops the loop: it goes to the `rejects` table with the
reason, where the feed monitor counts it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from datetime import UTC, date, datetime, timedelta

import requests
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from .. import config
from ..market.hotels import OTAS
from ..market.sim import HORIZON
from .db import (
    engine,
    events,
    hotel_aliases,
    hotels,
    rate_observations,
    rate_requests,
    redis_client,
    rejects,
    reservations,
    turnaways,
)

STREAMS = ("rates", "pms", "turnaways", "events", "inbox")
GROUP = "normalizer"
NOISE = re.compile(r"\b(hotel|vashi|navi mumbai|the)\b|[^a-z0-9 ]")


def _key(name: str) -> str:
    return " ".join(NOISE.sub(" ", name.lower()).split())


class Aliases:
    """OTA hotel names to hotel ids. Exact matches come from the table;
    an unseen name is matched against known names after stripping noise
    words ("Hotel", the city), and is accepted only above a high similarity,
    then stored so the decision is visible and reversible."""

    THRESHOLD = 0.86

    def __init__(self, conn):
        self.exact = {(r.ota, r.alias): r.hotel_id for r in conn.execute(select(hotel_aliases))}
        self.names = {_key(r.name): r.id for r in conn.execute(select(hotels))}
        for (_, alias), hid in self.exact.items():
            self.names.setdefault(_key(alias), hid)

    def resolve(self, conn, ota: str, alias: str) -> str | None:
        if (ota, alias) in self.exact:
            return self.exact[(ota, alias)]
        key = _key(alias)
        best = difflib.get_close_matches(key, list(self.names), n=1, cutoff=self.THRESHOLD)
        if not best:
            return None
        hid = self.names[best[0]]
        conn.execute(
            insert(hotel_aliases).values(ota=ota, alias=alias, hotel_id=hid, source="fuzzy").prefix_with("IGNORE")
        )
        self.exact[(ota, alias)] = hid
        return hid


def reject(conn, source: str, reason: str, payload: dict) -> None:
    conn.execute(
        rejects.insert().values(
            at=datetime.now(UTC).replace(tzinfo=None), source=source, reason=reason[:200], payload=payload
        )
    )


def handle_rate(conn, aliases: Aliases, m: dict) -> None:
    if m.get("ota") not in OTAS:
        return reject(conn, "rates", "unknown ota", m)
    try:
        stay, biz = date.fromisoformat(m["stay_date"]), date.fromisoformat(m["business_date"])
    except (KeyError, TypeError, ValueError):
        return reject(conn, "rates", "bad dates", m)
    if not 0 <= (stay - biz).days <= HORIZON:
        return reject(conn, "rates", "stay date outside booking window", m)
    rate = m.get("rate")
    if rate is not None and not (500 <= int(rate) <= 100_000):
        return reject(conn, "rates", f"rate {rate} outside 500..100000", m)
    hid = aliases.resolve(conn, m["ota"], m.get("alias") or "")
    if hid is None:
        return reject(conn, "rates", f"unknown hotel name {m.get('alias')!r}", m)
    stmt = insert(rate_observations).values(
        ota=m["ota"],
        hotel_id=hid,
        stay_date=stay,
        business_date=biz,
        rate=rate,
        available=rate is not None,
        observed_at=datetime.now(UTC).replace(tzinfo=None),
        raw_id=m.get("raw_id"),
    )
    conn.execute(
        stmt.on_duplicate_key_update(
            rate=stmt.inserted.rate,
            available=stmt.inserted.available,
            observed_at=stmt.inserted.observed_at,
            raw_id=stmt.inserted.raw_id,
        )
    )


RES_COLS = (
    "booked_on",
    "arrival",
    "nights",
    "adr",
    "channel",
    "deposit_type",
    "adults",
    "children",
    "special_requests",
    "is_repeated_guest",
    "previous_cancellations",
    "market_segment",
)


def handle_pms(conn, m: dict) -> None:
    try:
        row = {c: m[c] for c in RES_COLS}
        row["id"], row["status"], row["pms_seq"] = m["id"], m["status"], int(m["seq"])
        row["cancelled_on"] = m.get("cancelled_on")
    except (KeyError, TypeError, ValueError):
        return reject(conn, "pms", "missing reservation fields", m)
    if row["status"] not in ("confirmed", "cancelled") or row["nights"] < 1 or row["adr"] <= 0:
        return reject(conn, "pms", "invalid reservation", m)
    stmt = insert(reservations).values(**row)
    newer = stmt.inserted.pms_seq > reservations.c.pms_seq
    from sqlalchemy import func

    conn.execute(
        stmt.on_duplicate_key_update(
            # Only a newer change may overwrite: a replayed older message must
            # not resurrect a cancelled booking. pms_seq goes last on purpose.
            status=func.if_(newer, stmt.inserted.status, reservations.c.status),
            cancelled_on=func.if_(newer, stmt.inserted.cancelled_on, reservations.c.cancelled_on),
            pms_seq=func.greatest(reservations.c.pms_seq, stmt.inserted.pms_seq),
        )
    )


def handle_turnaway(conn, m: dict) -> None:
    try:
        row = {
            "seq": int(m["seq"]),
            "day": date.fromisoformat(m["day"]),
            "stay_date": date.fromisoformat(m["stay_date"]),
        }
    except (KeyError, TypeError, ValueError):
        return reject(conn, "turnaways", "bad turnaway", m)
    conn.execute(insert(turnaways).values(**row).prefix_with("IGNORE"))


def handle_event(conn, m: dict) -> None:
    try:
        row = {
            "name": m["name"],
            "start": date.fromisoformat(m["start"]),
            "end": date.fromisoformat(m["end"]),
            "announced": date.fromisoformat(m["announced"]),
        }
    except (KeyError, TypeError, ValueError):
        return reject(conn, "events", "bad event", m)
    stmt = insert(events).values(**row)
    conn.execute(stmt.on_duplicate_key_update(start=stmt.inserted.start, end=stmt.inserted.end))


def extract(text: str) -> tuple[dict | None, str]:
    """Structured fields from a free-text rate request. The regex parser
    goes first: when it returns a full record on clean text it has been
    right every time in the evaluation. Messages it cannot parse, mostly
    the ones with typos, go to the small language model behind the model
    service."""
    from ..ml.extract_baseline import regex_extract

    fields = regex_extract(text)
    if fields:
        return fields, "regex"
    try:
        resp = requests.post(f"{config.MODELS_URL}/extract", json={"text": text}, timeout=10)
        resp.raise_for_status()
        return resp.json()["fields"], "slm"
    except (requests.RequestException, KeyError, ValueError):
        return None, "none"


def valid_request(f: dict | None) -> bool:
    if not f:
        return False
    try:
        ci, co = date.fromisoformat(f["check_in"]), date.fromisoformat(f["check_out"])
        return ci < co <= ci + timedelta(days=30) and 500 <= int(f["rate"]) <= 100_000 and 1 <= int(f["rooms"]) <= 100
    except (KeyError, TypeError, ValueError):
        return False


def handle_inbox(conn, m: dict) -> None:
    from ..ml.extract_baseline import finalize

    raw, how = extract(m["text"])
    fields = finalize(raw, date.fromisoformat(m["received"]))
    stmt = insert(rate_requests).values(
        message_id=m["id"],
        received=date.fromisoformat(m["received"]),
        text=m["text"],
        fields=fields,
        extractor=how,
        valid=valid_request(fields),
    )
    conn.execute(stmt.prefix_with("IGNORE"))


def ensure_groups(r) -> None:
    for s in STREAMS:
        try:
            r.xgroup_create(s, GROUP, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP: already there
            if "BUSYGROUP" not in str(e):
                raise


def process(batch, eng, r) -> int:
    n = 0
    with eng.begin() as conn:
        aliases = Aliases(conn)
        for stream, entries in batch:
            for _msg_id, fields in entries:
                m = json.loads(fields["data"])
                if stream == "rates":
                    handle_rate(conn, aliases, m)
                elif stream == "pms":
                    handle_pms(conn, m)
                elif stream == "turnaways":
                    handle_turnaway(conn, m)
                elif stream == "events":
                    handle_event(conn, m)
                elif stream == "inbox":
                    handle_inbox(conn, m)
                n += 1
    # Acknowledge only after the commit above.
    for stream, entries in batch:
        if entries:
            r.xack(stream, GROUP, *[e[0] for e in entries])
    return n


def run(once: bool = False, consumer: str = "worker-1") -> int:
    eng, r = engine(), redis_client()
    ensure_groups(r)
    total = 0
    # Messages a crashed worker read but never acknowledged.
    for s in STREAMS:
        _, claimed, *_ = r.xautoclaim(s, GROUP, consumer, min_idle_time=60_000, start_id="0-0", count=1000)
        if claimed:
            total += process([(s, claimed)], eng, r)
    while True:
        batch = r.xreadgroup(GROUP, consumer, {s: ">" for s in STREAMS}, count=500, block=1000)
        if not batch:
            if once:
                return total
            continue
        total += process(batch, eng, r)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    print(f"normalized {run(once=args.once)} messages")


if __name__ == "__main__":
    main()
