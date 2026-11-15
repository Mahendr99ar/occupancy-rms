"""Finds the hotels most like the client, to use as its competitive set.

Each hotel becomes one vector: star rating, size, distance from the business
district and review score (standardised), its amenities (one-hot), and the
TF-IDF of its listing description, scaled so the text does not drown the
rest. Similarity is cosine; the store is a MongoDB collection of vectors,
and search is a brute-force dot product, which is the right tool for a few
hundred hotels (an approximate index pays off in the millions).
"""

from __future__ import annotations

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from ..market.hotels import ALL_HOTELS, AMENITIES, CLIENT, Hotel

TEXT_WEIGHT = 0.6


def vectors(hotels=ALL_HOTELS) -> tuple[list[str], np.ndarray]:
    num = np.array([[h.stars, np.log(h.rooms), h.distance_km, h.review_score] for h in hotels], dtype=float)
    num = (num - num.mean(axis=0)) / (num.std(axis=0) + 1e-9)
    amen = np.array([[1.0 if a in h.amenities else 0.0 for a in AMENITIES] for h in hotels])
    text = TfidfVectorizer(stop_words="english").fit_transform([h.description for h in hotels]).toarray()
    v = np.hstack([num, amen, TEXT_WEIGHT * text])
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return [h.id for h in hotels], v


def similar(hotel: Hotel = CLIENT, k: int = 5, hotels=ALL_HOTELS) -> list[tuple[str, float]]:
    ids, v = vectors(hotels)
    me = v[ids.index(hotel.id)]
    sims = v @ me
    order = [i for i in np.argsort(-sims) if ids[i] != hotel.id]
    return [(ids[i], round(float(sims[i]), 3)) for i in order[:k]]


def store(db, hotels=ALL_HOTELS) -> int:
    ids, v = vectors(hotels)
    for hid, vec in zip(ids, v, strict=True):
        db.hotel_vectors.replace_one({"_id": hid}, {"_id": hid, "vector": vec.tolist()}, upsert=True)
    return len(ids)


def search(db, hotel_id: str, k: int = 5) -> list[tuple[str, float]]:
    docs = list(db.hotel_vectors.find())
    me = np.array(next(d["vector"] for d in docs if d["_id"] == hotel_id))
    scored = [(d["_id"], float(np.dot(me, d["vector"]))) for d in docs if d["_id"] != hotel_id]
    return [(h, round(s, 3)) for h, s in sorted(scored, key=lambda t: -t[1])[:k]]
