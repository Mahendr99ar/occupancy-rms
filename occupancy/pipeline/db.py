"""MySQL schema for clean data, and helpers to open the stores.

MySQL holds what the pricing engine and the dashboard read: one row per
observation, reservation, event, request and recommendation, with keys that
make every write an upsert. MongoDB holds what came off the wire, untouched,
so a parser bug can be fixed and the raw pages parsed again.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine

from .. import config
from ..market.hotels import ALL_HOTELS, CLIENT

meta = MetaData()

hotels = Table(
    "hotels",
    meta,
    Column("id", String(8), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("stars", Integer, nullable=False),
    Column("rooms", Integer, nullable=False),
    Column("is_client", Boolean, nullable=False, default=False),
)

hotel_aliases = Table(
    "hotel_aliases",
    meta,
    Column("ota", String(32), primary_key=True),
    Column("alias", String(160), primary_key=True),
    Column("hotel_id", String(8), nullable=False),
    Column("source", String(16), nullable=False, default="seed"),  # seed | fuzzy
)

rate_observations = Table(
    "rate_observations",
    meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ota", String(32), nullable=False),
    Column("hotel_id", String(8), nullable=False),
    Column("stay_date", Date, nullable=False),
    Column("business_date", Date, nullable=False),
    Column("rate", Integer),  # NULL when sold out
    Column("available", Boolean, nullable=False),
    Column("observed_at", DateTime, nullable=False),
    Column("raw_id", String(40)),
    # One observation per OTA, hotel and stay date per business day: a
    # re-crawl or a replayed stream message updates instead of duplicating.
    UniqueConstraint("ota", "hotel_id", "stay_date", "business_date", name="uq_obs"),
)

reservations = Table(
    "reservations",
    meta,
    Column("id", String(24), primary_key=True),
    Column("booked_on", Date, nullable=False),
    Column("arrival", Date, nullable=False),
    Column("nights", Integer, nullable=False),
    Column("adr", Integer, nullable=False),
    Column("channel", String(24), nullable=False),
    Column("deposit_type", String(16), nullable=False),
    Column("adults", Integer, nullable=False),
    Column("children", Integer, nullable=False),
    Column("special_requests", Integer, nullable=False),
    Column("is_repeated_guest", Integer, nullable=False),
    Column("previous_cancellations", Integer, nullable=False),
    Column("market_segment", String(24), nullable=False),
    Column("status", String(16), nullable=False),
    Column("cancelled_on", Date),
    Column("pms_seq", Integer, nullable=False),
)

turnaways = Table(
    "turnaways",
    meta,
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("day", Date, nullable=False),
    Column("stay_date", Date, nullable=False),
)

events = Table(
    "events",
    meta,
    Column("name", String(120), primary_key=True),
    Column("start", Date, nullable=False),
    Column("end", Date, nullable=False),
    Column("announced", Date, nullable=False),
)

rate_requests = Table(
    "rate_requests",
    meta,
    Column("message_id", String(24), primary_key=True),
    Column("received", Date, nullable=False),
    Column("text", Text, nullable=False),
    Column("fields", JSON),
    Column("extractor", String(16)),
    Column("valid", Boolean, nullable=False, default=False),
    Column("status", String(16), nullable=False, default="new"),
)

recommendations = Table(
    "recommendations",
    meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("business_date", Date, nullable=False),
    Column("stay_date", Date, nullable=False),
    Column("current_rate", Integer, nullable=False),
    Column("recommended_rate", Integer, nullable=False),
    Column("comp_median", Integer),
    Column("otb", Integer, nullable=False),
    Column("forecast_rooms", Integer, nullable=False),
    Column("reasons", JSON, nullable=False),
    Column("explanation", Text),
    Column("status", String(16), nullable=False, default="pending"),
    Column("decided_by", String(64)),
    Column("pushed_at", DateTime),
    Column("confirmation", String(64)),
    UniqueConstraint("business_date", "stay_date", name="uq_rec"),
)

rejects = Table(
    "rejects",
    meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime, nullable=False),
    Column("source", String(32), nullable=False),
    Column("reason", String(200), nullable=False),
    Column("payload", JSON),
)

feed_checks = Table(
    "feed_checks",
    meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime, nullable=False),
    Column("business_date", Date, nullable=False),
    Column("feed", String(32), nullable=False),
    Column("check_name", String(40), nullable=False),
    Column("status", String(8), nullable=False),
    Column("value", String(40)),
    Column("detail", String(300)),
)

pipeline_state = Table(
    "pipeline_state",
    meta,
    Column("key", String(64), primary_key=True),
    Column("value", String(200), nullable=False),
)


@lru_cache(maxsize=4)
def engine(url: str | None = None) -> Engine:
    """One engine per URL per process; creates missing tables on first use
    so every entry point can start against an empty database."""
    eng = create_engine(url or config.MYSQL_URL, pool_pre_ping=True, pool_recycle=300, future=True)
    create_all(eng)
    return eng


def create_all(eng: Engine) -> None:
    from sqlalchemy.dialects.mysql import insert

    meta.create_all(eng)
    with eng.begin() as conn:
        for h in ALL_HOTELS:
            conn.execute(
                insert(hotels)
                .values(id=h.id, name=h.name, stars=h.stars, rooms=h.rooms, is_client=h.id == CLIENT.id)
                .prefix_with("IGNORE")
            )
            for ota, alias in h.ota_names.items():
                conn.execute(
                    insert(hotel_aliases)
                    .values(ota=ota, alias=alias, hotel_id=h.id, source="seed")
                    .prefix_with("IGNORE")
                )


def get_state(conn, key: str, default: str = "") -> str:
    row = conn.execute(pipeline_state.select().where(pipeline_state.c.key == key)).first()
    return row.value if row else default


def set_state(conn, key: str, value: str) -> None:
    from sqlalchemy.dialects.mysql import insert

    stmt = insert(pipeline_state).values(key=key, value=value)
    conn.execute(stmt.on_duplicate_key_update(value=stmt.inserted.value))


_MOCK_CLIENT = None


def mongo(url: str | None = None):
    global _MOCK_CLIENT
    url = url or config.MONGO_URL
    if url.startswith("mongomock://"):
        import mongomock

        # One client per process, or every call would see an empty database.
        _MOCK_CLIENT = _MOCK_CLIENT or mongomock.MongoClient()
        return _MOCK_CLIENT[url.split("/")[-1] or "occupancy"]
    from pymongo import MongoClient

    client = MongoClient(url)
    return client.get_default_database("occupancy")


def redis_client(url: str | None = None):
    import redis

    return redis.Redis.from_url(url or config.REDIS_URL, decode_responses=True)
