"""The model service: the trained models behind one small FastAPI app, so
the crawler, the normalizer and the dashboard API share one copy of each
model in memory and one place to version them.

    uvicorn occupancy.api.models:app --port 8600
"""

from __future__ import annotations

import os
from functools import lru_cache

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config

app = FastAPI(title="Occupancy model service", version="1.0")


@lru_cache(maxsize=1)
def slm():
    from ..ml import slm as m

    return m.load()


@lru_cache(maxsize=1)
def price_reader():
    from ..ml.price_reader import PriceReader

    return PriceReader.load(os.path.join(config.MODEL_DIR, "price_reader.joblib"))


@lru_cache(maxsize=1)
def cancel_model():
    from ..ml.cancellation import CancelModel

    return CancelModel()


class ExtractIn(BaseModel):
    text: str = Field(min_length=5, max_length=600)


class Booking(BaseModel):
    id: str
    booked_on: str
    arrival: str
    nights: int = Field(ge=1, le=30)
    adr: float = Field(gt=0)
    adults: int = 1
    children: int = 0
    special_requests: int = 0
    is_repeated_guest: int = 0
    previous_cancellations: int = 0
    deposit_type: str = "No Deposit"
    market_segment: str = "Online TA"


@app.get("/health")
def health():
    found = {
        name: os.path.exists(os.path.join(config.MODEL_DIR, f))
        for name, f in (
            ("slm", "slm.pt"),
            ("price_reader", "price_reader.joblib"),
            ("cancellation", "cancellation.joblib"),
        )
    }
    return {"ok": all(found.values()), "models": found}


@app.post("/extract")
def extract(body: ExtractIn):
    """Rate request text -> fields, from the small language model. Dates
    come back as day-month; the caller adds the year from the received
    date."""
    fields = slm().extract(body.text)
    if fields is None:
        raise HTTPException(422, "the model's output did not parse")
    return {"fields": fields, "model": "slm"}


@app.post("/read-price")
async def read_price(request: Request):
    body = await request.body()
    if not body:
        raise HTTPException(400, "send the PNG as the request body")
    return {"rate": price_reader().read(body)}


@app.post("/cancel-risk")
def cancel_risk(bookings: list[Booking]):
    if not bookings:
        return {"risk": {}}
    df = pd.DataFrame([b.model_dump() for b in bookings])
    for c in ("booked_on", "arrival"):
        df[c] = pd.to_datetime(df[c]).dt.date
    probs = cancel_model().predict(df)
    return {
        "risk": {b.id: round(float(p), 3) for b, p in zip(bookings, probs, strict=True)},
        "model": cancel_model().name,
    }
