"""Rate-shopping spiders, one per OTA.

Each spider asks its OTA for every stay date in the crawl window, keeps the
raw response in MongoDB, and yields one RateItem per hotel listed. They do
not decide which hotel an OTA's name refers to; that is the normalizer's
job, so a renamed listing is a data problem to fix in one place, not a
crawler change.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import scrapy

from ..items import RateItem

PRICE = re.compile(r"[\d,]+")


def money(text: str | None) -> int | None:
    if not text:
        return None
    m = PRICE.search(text.replace(" ", ""))
    return int(m.group().replace(",", "")) if m and m.group().replace(",", "") else None


class BaseRateSpider(scrapy.Spider):
    ota = ""

    def __init__(self, market_url: str, business_date: str, days: int = 30, crawl_id: str = "", **kw):
        super().__init__(**kw)
        self.market_url = market_url.rstrip("/")
        self.business_date = date.fromisoformat(business_date)
        self.days = int(days)
        self.crawl_id = crawl_id

    async def start(self):
        for request in self.start_requests():
            yield request

    def stay_dates(self):
        for i in range(self.days):
            yield self.business_date + timedelta(days=i)

    def item(self, alias: str, stay: date, rate: int | None, raw_id: str) -> RateItem:
        return RateItem(
            ota=self.ota,
            alias=alias.strip(),
            stay_date=stay.isoformat(),
            rate=rate,
            available=rate is not None,
            business_date=self.business_date.isoformat(),
            raw_id=raw_id,
            crawl_id=self.crawl_id,
        )

    def raw(self, response, stay: date) -> str:
        return self.crawler.raw_store.save(self.ota, response, stay, self.business_date, self.crawl_id)

    def layout_failure(self, stay: date, response) -> None:
        self.crawler.stats.inc_value(f"{self.ota}/layout_unknown")
        self.logger.error("unknown layout on %s for %s", response.url, stay)


class StayKartSpider(BaseRateSpider):
    """Server-rendered HTML. Two layouts are known: the old results table
    and the newer card grid. Anything else is counted as a layout failure
    rather than silently yielding nothing."""

    name = ota = "staykart"

    def start_requests(self):
        for stay in self.stay_dates():
            yield scrapy.Request(f"{self.market_url}/staykart/search?checkin={stay}", cb_kwargs={"stay": stay})

    def parse(self, response, stay: date):
        raw_id = self.raw(response, stay)
        rows = response.css("table.results tr")[1:]
        if rows:
            self.crawler.stats.inc_value("staykart/layout_v1")
            for row in rows:
                yield self.item(row.css("td.name::text").get(""), stay, money(row.css("td.price::text").get()), raw_id)
            return
        cards = response.css("div.card")
        if cards:
            self.crawler.stats.inc_value("staykart/layout_v2")
            for card in cards:
                amount = card.css("span.amount:not(.sold-out)::text").get()
                yield self.item(card.css("h3.hotel-title::text").get(""), stay, money(amount), raw_id)
            return
        self.layout_failure(stay, response)


class TripNestSpider(BaseRateSpider):
    """The page is an empty shell; prices come from a paginated JSON API
    that answers 429 when called too fast."""

    name = ota = "tripnest"
    custom_settings = {"DOWNLOAD_DELAY": 0.15, "CONCURRENT_REQUESTS_PER_DOMAIN": 1}

    def start_requests(self):
        for stay in self.stay_dates():
            yield self.api(stay, 1)

    def api(self, stay: date, page: int):
        return scrapy.Request(f"{self.market_url}/tripnest/api/rates?date={stay}&page={page}", cb_kwargs={"stay": stay})

    def parse(self, response, stay: date):
        raw_id = self.raw(response, stay)
        data = response.json()
        for row in data["results"]:
            nightly = row.get("nightly") or {}
            if nightly and nightly.get("currency") != "INR":
                self.crawler.stats.inc_value("tripnest/foreign_currency")
                continue
            yield self.item(row["property"], stay, nightly.get("amount") if row.get("available") else None, raw_id)
        if data.get("next_page"):
            yield self.api(stay, data["next_page"])


class RoomBazaarSpider(BaseRateSpider):
    """Prices are images. The spider downloads each one and reads it with
    the SVM price reader; a price it cannot read is reported as unreadable,
    never guessed."""

    name = ota = "roombazaar"

    def start_requests(self):
        for stay in self.stay_dates():
            yield scrapy.Request(f"{self.market_url}/roombazaar/listing?date={stay}", cb_kwargs={"stay": stay})

    def parse(self, response, stay: date):
        raw_id = self.raw(response, stay)
        for card in response.css("article.hotel"):
            alias = card.css("div.hn::text").get("")
            src = card.css("img.px::attr(src)").get()
            if src is None:
                yield self.item(alias, stay, None, raw_id)
                continue
            yield response.follow(
                src, callback=self.parse_price, cb_kwargs={"stay": stay, "alias": alias, "raw_id": raw_id}
            )

    def parse_price(self, response, stay: date, alias: str, raw_id: str):
        rate = self.crawler.price_reader.read(response.body)
        self.crawler.raw_store.save_image(self.ota, response.url, response.body, rate, self.business_date)
        if rate is None:
            self.crawler.stats.inc_value("roombazaar/unreadable_price")
            return
        yield self.item(alias, stay, rate, raw_id)


SPIDERS = (StayKartSpider, TripNestSpider, RoomBazaarSpider)
