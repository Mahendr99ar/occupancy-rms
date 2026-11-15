import scrapy


class RateItem(scrapy.Item):
    ota = scrapy.Field()
    alias = scrapy.Field()  # the hotel name as the OTA prints it
    stay_date = scrapy.Field()
    rate = scrapy.Field()  # None when sold out
    available = scrapy.Field()
    business_date = scrapy.Field()
    raw_id = scrapy.Field()  # MongoDB id of the page this came from
    crawl_id = scrapy.Field()
