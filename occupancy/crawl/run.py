"""Runs the three rate-shopping spiders once for the market's current
business day.

    python -m occupancy.crawl.run [--days 30]
"""

from __future__ import annotations

import argparse
import uuid

import requests
from scrapy.crawler import CrawlerProcess

from .. import config
from .spiders.rates import SPIDERS

SETTINGS = {
    "BOT_NAME": "occupancy",
    "ROBOTSTXT_OBEY": False,  # the mock sites have none; set True against real sites
    "USER_AGENT": "occupancy-rate-shopper/0.1 (+https://github.com/Mahendr99ar/occupancy-rms)",
    "CONCURRENT_REQUESTS": 8,
    "RETRY_ENABLED": True,
    "RETRY_TIMES": 5,
    "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
    "AUTOTHROTTLE_ENABLED": True,
    "AUTOTHROTTLE_START_DELAY": 0.05,
    "ITEM_PIPELINES": {"occupancy.crawl.pipelines.RedisStreamPipeline": 100},
    "EXTENSIONS": {"occupancy.crawl.ext.Resources": 0},
    "LOG_LEVEL": "WARNING",
    "TELNETCONSOLE_ENABLED": False,
    "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
}


def crawl(days: int = config.CRAWL_DAYS_AHEAD, market_url: str = config.MARKET_URL) -> str:
    """Blocks until all spiders finish. Twisted's reactor cannot be started
    twice, so this runs at most once per process."""
    today = requests.get(f"{market_url}/admin/clock", timeout=10).json()["today"]
    crawl_id = uuid.uuid4().hex[:12]
    process = CrawlerProcess(SETTINGS)
    for spider in SPIDERS:
        process.crawl(spider, market_url=market_url, business_date=today, days=days, crawl_id=crawl_id)
    process.start()
    return f"crawl {crawl_id} for {today} done"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=config.CRAWL_DAYS_AHEAD)
    p.add_argument("--market-url", default=config.MARKET_URL)
    args = p.parse_args()
    print(crawl(args.days, args.market_url))


if __name__ == "__main__":
    main()
