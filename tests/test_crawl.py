import json
from datetime import date

from scrapy.http import HtmlResponse, Request, TextResponse

from occupancy.crawl.spiders.rates import RoomBazaarSpider, StayKartSpider, TripNestSpider, money


class Stats:
    def __init__(self):
        self.values = {}

    def inc_value(self, k, n=1):
        self.values[k] = self.values.get(k, 0) + n


class Raw:
    def save(self, *a):
        return "raw-1"

    def save_image(self, *a):
        pass


class Crawler:
    def __init__(self):
        self.stats, self.raw_store = Stats(), Raw()


def spider(cls):
    s = cls(market_url="http://m", business_date="2026-10-01", days=1, crawl_id="c1")
    s.crawler = Crawler()
    return s


def html(url, body):
    return HtmlResponse(url=url, body=body.encode(), encoding="utf-8", request=Request(url))


STAY = date(2026, 10, 3)


def test_money_parsing():
    assert money("₹ 6,800") == 6800 and money("Sold out") is None and money(None) is None


def test_staykart_reads_both_layouts_and_flags_unknown_ones():
    s = spider(StayKartSpider)
    v1 = html(
        "http://m/a",
        '<table class="results"><tr><th>H</th></tr><tr><td class="name">Creekside Inn</td>'
        '<td class="price">&#8377; 4,300</td></tr><tr><td class="name">Lagoon</td><td class="price">Sold out</td></tr></table>',
    )
    items = list(s.parse(v1, STAY))
    assert [(i["alias"], i["rate"], i["available"]) for i in items] == [
        ("Creekside Inn", 4300, True),
        ("Lagoon", None, False),
    ]
    v2 = html(
        "http://m/b",
        '<div class="card"><h3 class="hotel-title">Palm Court Vashi</h3>'
        '<span class="amount" data-currency="INR">6400</span></div>',
    )
    assert list(s.parse(v2, STAY))[0]["rate"] == 6400
    assert list(s.parse(html("http://m/c", "<div>redesigned again</div>"), STAY)) == []
    assert s.crawler.stats.values["staykart/layout_unknown"] == 1


def test_tripnest_follows_pages_and_skips_other_currencies():
    s = spider(TripNestSpider)
    body = {
        "results": [
            {"property": "CREEKSIDE INN", "nightly": {"amount": 4300, "currency": "INR"}, "available": True},
            {"property": "X", "nightly": {"amount": 50, "currency": "USD"}, "available": True},
        ],
        "page": 1,
        "next_page": 2,
    }
    out = list(
        s.parse(
            TextResponse(
                url="http://m/t", body=json.dumps(body).encode(), encoding="utf-8", request=Request("http://m/t")
            ),
            STAY,
        )
    )
    assert out[0]["rate"] == 4300 and "page=2" in out[-1].url
    assert s.crawler.stats.values["tripnest/foreign_currency"] == 1


def test_roombazaar_requests_each_price_image():
    s = spider(RoomBazaarSpider)
    page = html(
        "http://m/r",
        '<article class="hotel"><div class="hn">Palm Court</div><img class="px" src="/roombazaar/p/x.png"></article>'
        '<article class="hotel"><div class="hn">Lagoon</div><div class="na">Not available</div></article>',
    )
    out = list(s.parse(page, STAY))
    assert out[0].url.endswith("/roombazaar/p/x.png") and out[1]["available"] is False
