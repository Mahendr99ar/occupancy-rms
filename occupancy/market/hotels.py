"""The hotels in the simulated market: one client and its competitors.

All names are invented. The market is modelled on a business district
(weekday corporate demand, quieter weekends), which is why Monday to
Thursday carry the highest multipliers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AMENITIES = ("pool", "gym", "spa", "breakfast", "airport_shuttle", "banquet", "bar", "parking", "business_centre")


@dataclass(frozen=True)
class Hotel:
    id: str
    name: str
    stars: int
    rooms: int
    base_rate: int  # INR per night, weekday, no event
    distance_km: float  # from the business district centre
    review_score: float
    amenities: tuple[str, ...]
    description: str
    # How each OTA spells the name. The normalizer has to map these back.
    ota_names: dict[str, str] = field(default_factory=dict)
    event_aware: bool = True  # whether the hotel raises rates for known events


CLIENT = Hotel(
    id="H00",
    name="Saltpan Bay Residency",
    stars=4,
    rooms=120,
    base_rate=6800,
    distance_km=1.2,
    review_score=8.4,
    amenities=("pool", "gym", "breakfast", "banquet", "bar", "parking", "business_centre"),
    description="Business hotel near the trade centre with banquet halls, a rooftop pool and meeting rooms.",
    ota_names={
        "staykart": "Saltpan Bay Residency",
        "tripnest": "SALTPAN BAY RESIDENCY",
        "roombazaar": "Saltpan Bay Residency, Vashi",
    },
)

COMPETITORS = (
    Hotel(
        "H01",
        "Creekside Inn",
        3,
        80,
        4200,
        0.8,
        7.9,
        ("breakfast", "parking", "business_centre"),
        "Simple rooms for business travellers, walking distance from the station.",
        {"staykart": "Creekside Inn", "tripnest": "CREEKSIDE INN", "roombazaar": "Creekside Inn (Navi Mumbai)"},
    ),
    Hotel(
        "H02",
        "The Mangrove Grand",
        5,
        210,
        9800,
        1.5,
        8.9,
        ("pool", "gym", "spa", "breakfast", "airport_shuttle", "banquet", "bar", "parking", "business_centre"),
        "Five-star hotel with a spa, three restaurants and the largest banquet space in the area.",
        {"staykart": "The Mangrove Grand", "tripnest": "MANGROVE GRAND", "roombazaar": "The Mangrove Grand Hotel"},
    ),
    Hotel(
        "H03",
        "Harbourline Suites",
        4,
        140,
        7200,
        2.1,
        8.5,
        ("pool", "gym", "breakfast", "bar", "parking", "business_centre"),
        "Business suites with kitchenettes, a pool and meeting rooms near the expressway.",
        {"staykart": "Harbourline Suites", "tripnest": "HARBOURLINE SUITES", "roombazaar": "Harbourline Suites Vashi"},
    ),
    Hotel(
        "H04",
        "Sector 17 Business Hotel",
        3,
        95,
        4800,
        0.4,
        7.6,
        ("breakfast", "business_centre", "parking"),
        "Budget business hotel in the market area with a small conference room.",
        {
            "staykart": "Sector 17 Business Hotel",
            "tripnest": "SECTOR 17 BUSINESS HOTEL",
            "roombazaar": "Sector-17 Business Hotel",
        },
    ),
    Hotel(
        "H05",
        "Palm Court Vashi",
        4,
        110,
        6400,
        1.0,
        8.2,
        ("pool", "gym", "breakfast", "banquet", "parking", "business_centre"),
        "Four-star hotel with a pool, banquet hall and business centre close to the trade centre.",
        {"staykart": "Palm Court Vashi", "tripnest": "PALM COURT VASHI", "roombazaar": "Palm Court"},
        event_aware=False,
    ),
    Hotel(
        "H06",
        "Lagoon Stay",
        2,
        60,
        2600,
        3.4,
        7.1,
        ("parking",),
        "Budget rooms on the highway, popular with long-stay guests.",
        {"staykart": "Lagoon Stay", "tripnest": "LAGOON STAY", "roombazaar": "Lagoon Stay Navi Mumbai"},
        event_aware=False,
    ),
    Hotel(
        "H07",
        "Skyview Towers",
        4,
        160,
        7600,
        2.8,
        8.6,
        ("pool", "gym", "spa", "breakfast", "bar", "parking", "business_centre"),
        "High-rise four-star with a spa, a rooftop bar and views of the creek.",
        {"staykart": "Skyview Towers", "tripnest": "SKYVIEW TOWERS", "roombazaar": "Skyview Towers Hotel"},
    ),
    Hotel(
        "H08",
        "Orchid Corporate Inn",
        3,
        70,
        5000,
        0.9,
        8.0,
        ("breakfast", "gym", "business_centre", "parking"),
        "Corporate inn with a business centre and breakfast included, near the IT parks.",
        {"staykart": "Orchid Corporate Inn", "tripnest": "ORCHID CORPORATE INN", "roombazaar": "Orchid Corp Inn"},
    ),
)

ALL_HOTELS = (CLIENT, *COMPETITORS)
BY_ID = {h.id: h for h in ALL_HOTELS}

# Which OTA lists which hotel. Not every hotel is on every OTA.
LISTINGS = {
    "staykart": ("H00", "H01", "H02", "H03", "H04", "H05", "H07", "H08"),
    "tripnest": ("H00", "H01", "H02", "H03", "H05", "H06", "H07"),
    "roombazaar": ("H00", "H02", "H03", "H04", "H05", "H06", "H07", "H08"),
}
OTAS = tuple(LISTINGS)
