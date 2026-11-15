"""Tests that need MySQL or Redis use separate test databases and skip when
those services are not reachable. Set MYSQL_TEST_URL / REDIS_TEST_URL to
point elsewhere."""

import os

# Assigned, not defaulted: these tests drop tables and flush Redis, so they
# must never pick up the MYSQL_URL / REDIS_URL of a running stack.
os.environ["MYSQL_URL"] = os.environ.get("MYSQL_TEST_URL", "mysql+pymysql://occ:occ@127.0.0.1:3307/occupancy_test")
os.environ["REDIS_URL"] = os.environ.get("REDIS_TEST_URL", "redis://127.0.0.1:6380/1")
os.environ["MONGO_URL"] = "mongomock://occupancy_test"
os.environ.setdefault("MODELS_URL", "http://127.0.0.1:9")  # nothing listens: tests use the fallbacks
os.environ.setdefault("API_TOKEN", "test-token")
os.environ.setdefault("CM_FAILURE_RATE", "0")

import pytest  # noqa: E402


def _mysql_ok() -> bool:
    try:
        from sqlalchemy import create_engine, text

        with create_engine(os.environ["MYSQL_URL"]).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _redis_ok() -> bool:
    try:
        import redis

        return redis.Redis.from_url(os.environ["REDIS_URL"]).ping()
    except Exception:
        return False


needs_mysql = pytest.mark.skipif(not _mysql_ok(), reason="MySQL not reachable")
needs_redis = pytest.mark.skipif(not _redis_ok(), reason="Redis not reachable")


@pytest.fixture
def clean_db():
    from occupancy.pipeline.db import engine, meta

    eng = engine()
    meta.drop_all(eng)
    from occupancy.pipeline.db import create_all

    create_all(eng)
    return eng


@pytest.fixture
def clean_redis():
    from occupancy.pipeline.db import redis_client

    r = redis_client()
    r.flushdb()
    return r
