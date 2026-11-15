"""Things every spider needs, attached to the crawler once: the raw store,
the price reader, and a record of each run's stats for the feed monitor."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from bson import ObjectId
from scrapy import signals

from .. import config
from ..ml.price_reader import PriceReader
from ..pipeline.db import mongo


class RawStore:
    def __init__(self, db):
        self.db = db

    def save(self, ota, response, stay, business_date, crawl_id) -> str:
        doc = {
            "_id": str(ObjectId()),
            "ota": ota,
            "url": response.url,
            "status": response.status,
            "stay_date": stay.isoformat(),
            "business_date": business_date.isoformat(),
            "crawl_id": crawl_id,
            "fetched_at": datetime.now(UTC),
            "body": response.text,
        }
        self.db.raw_pages.insert_one(doc)
        return doc["_id"]

    def save_image(self, ota, url, body, read_as, business_date) -> None:
        self.db.raw_images.insert_one(
            {
                "ota": ota,
                "url": url,
                "png": body,
                "read_as": read_as,
                "business_date": business_date.isoformat(),
                "fetched_at": datetime.now(UTC),
            }
        )


class Resources:
    def __init__(self, crawler):
        self.crawler = crawler
        db = mongo()
        crawler.raw_store = RawStore(db)
        crawler.price_reader = PriceReader.load(os.path.join(config.MODEL_DIR, "price_reader.joblib"))
        self.db = db
        crawler.signals.connect(self.closed, signal=signals.spider_closed)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def closed(self, spider, reason):
        stats = {
            k.replace(".", "_").replace("/", "__"): v
            for k, v in self.crawler.stats.get_stats().items()
            if isinstance(v, (int, float, str))
        }
        self.db.crawl_runs.insert_one(
            {
                "ota": spider.ota,
                "crawl_id": spider.crawl_id,
                "business_date": spider.business_date.isoformat(),
                "reason": reason,
                "finished_at": datetime.now(UTC),
                "stats": stats,
            }
        )
