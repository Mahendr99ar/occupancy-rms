"""Which bookings will cancel.

Trained on the public Hotel Booking Demand data (Antonio, de Almeida and
Nunes, Data in Brief 22, 2019): 119,390 bookings at a city hotel and a
resort hotel in Portugal, arrivals July 2015 to August 2017. The split is
by arrival date, train before 2017 and test from 2017, because a random
split would let the model learn from the future.

Only features a PMS has at booking time are used. `reservation_status` and
its date are the outcome itself, and `assigned_room_type`, `booking_changes`
and `days_in_waiting_list` can change after booking, so they are left out.

Compared: the majority class, logistic regression, a decision tree, a linear
SVM and gradient-boosted trees.

    python -m occupancy.ml.cancellation   # trains, evaluates, saves the best
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.svm import LinearSVC
from sklearn.tree import DecisionTreeClassifier

from .. import config

DATA = Path(__file__).resolve().parents[2] / "data" / "hotel_bookings.csv"
MODEL_PATH = Path(config.MODEL_DIR) / "cancellation.joblib"
NUMERIC = [
    "lead_time",
    "adr",
    "nights",
    "adults",
    "children",
    "special_requests",
    "is_repeated_guest",
    "previous_cancellations",
    "arrival_month",
    "arrival_weekday",
]
# deposit_type is left out on purpose. In this dataset almost every
# "Non Refund" booking is marked cancelled (a known quirk of the source data,
# most likely group blocks released in bulk), so a model trained on it learns
# that non-refundable means certain to cancel. A hotel's own data says the
# opposite, and the score would rank the safest bookings as the riskiest.
CATEGORICAL = ["segment"]
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}


def _segment(s: str) -> str:
    # The dataset's segments, folded into the three the PMS reports.
    if s in ("Corporate",):
        return "Corporate"
    if s in ("Direct",):
        return "Direct"
    return "Online TA" if s == "Online TA" else "Other"


def load_public(path: Path = DATA) -> pd.DataFrame:
    raw = pd.read_csv(path)
    arrival = pd.to_datetime(
        dict(year=raw.arrival_date_year, month=raw.arrival_date_month.map(MONTHS), day=raw.arrival_date_day_of_month)
    )
    return pd.DataFrame(
        {
            "lead_time": raw.lead_time,
            "adr": raw.adr,
            "nights": raw.stays_in_weekend_nights + raw.stays_in_week_nights,
            "adults": raw.adults,
            "children": raw.children.fillna(0) + raw.babies,
            "special_requests": raw.total_of_special_requests,
            "is_repeated_guest": raw.is_repeated_guest,
            "previous_cancellations": raw.previous_cancellations,
            "arrival_month": arrival.dt.month,
            "arrival_weekday": arrival.dt.day_of_week,
            "deposit_type": raw.deposit_type.replace({"Refundable": "No Deposit"}),
            "segment": raw.market_segment.map(_segment),
            "arrival": arrival,
            "y": raw.is_canceled,
        }
    )


def from_pms(res: pd.DataFrame) -> pd.DataFrame:
    """The same features from the PMS reservation table. ADR in the public
    data is in euros; the hotel's is in rupees, so it is scaled by the
    ratio of medians before scoring (recorded with the model)."""
    arrival = pd.to_datetime(res.arrival)
    return pd.DataFrame(
        {
            "lead_time": [(a - b).days for a, b in zip(res.arrival, res.booked_on, strict=True)],
            "adr": res.adr,
            "nights": res.nights,
            "adults": res.adults,
            "children": res.children,
            "special_requests": res.special_requests,
            "is_repeated_guest": res.is_repeated_guest,
            "previous_cancellations": res.previous_cancellations,
            "arrival_month": arrival.dt.month,
            "arrival_weekday": arrival.dt.day_of_week,
            "deposit_type": res.deposit_type,
            "segment": res.market_segment,
        }
    )


def _prep():
    return ColumnTransformer(
        [("num", StandardScaler(), NUMERIC), ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL)]
    )


MODELS = {
    "majority": lambda: make_pipeline(_prep(), DummyClassifier(strategy="most_frequent")),
    "logistic": lambda: make_pipeline(_prep(), LogisticRegression(max_iter=2000)),
    "decision_tree": lambda: make_pipeline(
        _prep(), DecisionTreeClassifier(max_depth=8, min_samples_leaf=50, random_state=0)
    ),
    "linear_svm": lambda: make_pipeline(_prep(), LinearSVC(C=0.5, dual="auto", max_iter=5000)),
    "gradient_boosting": lambda: make_pipeline(_prep(), HistGradientBoostingClassifier(max_iter=300, random_state=0)),
}


def _scores(model, x) -> np.ndarray:
    est = model[-1]
    if hasattr(est, "predict_proba"):
        return model.predict_proba(x)[:, 1]
    return model.decision_function(x)


def compare(df: pd.DataFrame | None = None) -> tuple[dict, dict]:
    df = df if df is not None else load_public()
    train, test = df[df.arrival < "2017-01-01"], df[df.arrival >= "2017-01-01"]
    feats = NUMERIC + CATEGORICAL
    results, fitted = {}, {}
    for name, make in MODELS.items():
        model = make().fit(train[feats], train.y)
        pred = model.predict(test[feats])
        s = _scores(model, test[feats])
        results[name] = {
            "accuracy": round(accuracy_score(test.y, pred), 3),
            "roc_auc": None if name == "majority" else round(roc_auc_score(test.y, s), 3),
        }
        fitted[name] = model
    results["_split"] = {
        "train_rows": len(train),
        "test_rows": len(test),
        "test_cancel_rate": round(float(test.y.mean()), 3),
    }
    return results, fitted


def train_and_save() -> dict:
    df = load_public()
    results, fitted = compare(df)
    best = max(
        (k for k in results if not k.startswith("_") and results[k]["roc_auc"]), key=lambda k: results[k]["roc_auc"]
    )
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": fitted[best], "name": best, "adr_median": float(df.adr.median())}, MODEL_PATH)
    results["_saved"] = best
    return results


class CancelModel:
    def __init__(self, path: Path = MODEL_PATH):
        blob = joblib.load(path)
        self.model, self.name, self.adr_median = blob["model"], blob["name"], blob["adr_median"]

    def predict(self, res: pd.DataFrame) -> np.ndarray:
        x = from_pms(res)
        if len(x):
            x["adr"] = x.adr * (self.adr_median / max(float(np.median(x.adr)), 1.0))
        return self.model.predict_proba(x[NUMERIC + CATEGORICAL])[:, 1] if len(x) else np.array([])


if __name__ == "__main__":
    result = train_and_save()
    (MODEL_PATH.parent / "cancellation_metrics.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
