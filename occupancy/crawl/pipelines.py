"""Hands crawled rates to the normalizer through a Redis stream. The stream
decouples the two: the crawler finishes fast, the normalizer catches up, and
a normalizer restart loses nothing because unacknowledged messages stay in
the stream."""

from __future__ import annotations

import json

from ..pipeline.db import redis_client

STREAM = "rates"


class RedisStreamPipeline:
    def __init__(self, crawler):
        self.crawler = crawler
        self.r = redis_client()

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_item(self, item):
        self.r.xadd(STREAM, {"data": json.dumps(dict(item))}, maxlen=500_000, approximate=True)
        self.crawler.stats.inc_value(f"{item['ota']}/published")
        return item
