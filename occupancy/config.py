"""Connection settings, all from the environment so the same code runs in
tests, on a laptop and in docker compose."""

from __future__ import annotations

import os

MYSQL_URL = os.environ.get("MYSQL_URL", "mysql+pymysql://occ:occ@127.0.0.1:3307/occupancy")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017/occupancy")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")
MARKET_URL = os.environ.get("MARKET_URL", "http://127.0.0.1:8500")
MODELS_URL = os.environ.get("MODELS_URL", "http://127.0.0.1:8600")
MODEL_DIR = os.environ.get("MODEL_DIR", os.path.join(os.path.dirname(__file__), "..", "models"))
CRAWL_DAYS_AHEAD = int(os.environ.get("CRAWL_DAYS_AHEAD", "30"))
API_TOKEN = os.environ.get("API_TOKEN", "change-me")
