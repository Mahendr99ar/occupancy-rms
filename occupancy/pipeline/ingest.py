"""Pollers for the hotel's own systems. Each one reads from where it
stopped last time (a cursor kept in MySQL), publishes what is new to a Redis
stream, and only then moves the cursor. A crash between the two repeats a
page, which the normalizer's upserts absorb; it never skips one.
"""

from __future__ import annotations

import json

import requests

from .. import config
from .db import engine, get_state, redis_client, set_state


def _publish(r, stream: str, rows: list[dict]) -> None:
    pipe = r.pipeline()
    for row in rows:
        pipe.xadd(stream, {"data": json.dumps(row)}, maxlen=500_000, approximate=True)
    pipe.execute()


def _paged(path: str, stream: str, cursor: str, market_url: str, page: int) -> int:
    eng, r, total = engine(), redis_client(), 0
    while True:
        with eng.begin() as conn:
            after = int(get_state(conn, cursor, "0"))
        body = requests.get(f"{market_url}{path}", params={"after": after, "limit": page}, timeout=30).json()
        if not body["items"]:
            return total
        _publish(r, stream, body["items"])
        with eng.begin() as conn:
            set_state(conn, cursor, str(body["last_seq"]))
        total += len(body["items"])
        if not body["has_more"]:
            return total


def pms_changes(market_url: str = config.MARKET_URL, page: int = 1000) -> int:
    return _paged("/pms/changes", "pms", "pms_seq", market_url, page)


def pms_turnaways(market_url: str = config.MARKET_URL, page: int = 2000) -> int:
    return _paged("/pms/turnaways", "turnaways", "turnaway_seq", market_url, page)


def inbox(market_url: str = config.MARKET_URL) -> int:
    eng, r = engine(), redis_client()
    with eng.begin() as conn:
        after = get_state(conn, "inbox_after", "")
    msgs = requests.get(f"{market_url}/pms/inbox", params={"after": after}, timeout=30).json()["messages"]
    if msgs:
        _publish(r, "inbox", msgs)
        with eng.begin() as conn:
            set_state(conn, "inbox_after", msgs[-1]["id"])
    return len(msgs)


def events(market_url: str = config.MARKET_URL) -> int:
    items = requests.get(f"{market_url}/events", timeout=30).json()["events"]
    _publish(redis_client(), "events", items)
    return len(items)


def all_feeds(market_url: str = config.MARKET_URL) -> dict:
    return {
        "pms": pms_changes(market_url),
        "turnaways": pms_turnaways(market_url),
        "inbox": inbox(market_url),
        "events": events(market_url),
    }
