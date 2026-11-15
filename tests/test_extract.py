from datetime import date
from pathlib import Path

import pytest

from occupancy.market import messages
from occupancy.ml import slm
from occupancy.ml.extract_baseline import finalize, regex_extract, with_year


def test_year_comes_from_the_received_date():
    assert with_year("3-1", date(2026, 12, 20)) == date(2027, 1, 3)
    assert with_year("24-12", date(2026, 12, 20)) == date(2026, 12, 24)
    f = finalize({"check_in": "30-12", "check_out": "2-1"}, date(2026, 12, 1))
    assert (f["check_in"], f["check_out"]) == ("2026-12-30", "2027-01-02")


def test_regex_baseline_reads_a_plain_message():
    got = regex_extract("Acme Pharma: 3 deluxe, 14-16 Dec, INR 7,450, CP")
    assert got == {
        "company": "Acme Pharma",
        "room_type": "Deluxe",
        "meal_plan": "CP",
        "rate": 7450,
        "rooms": 3,
        "check_in": "14-12",
        "check_out": "16-12",
    }


def test_answer_format_round_trips():
    import numpy as np

    text, truth = messages.make(np.random.default_rng(1), date(2026, 5, 1))
    parsed = slm.parse_answer(slm.answer(truth))
    assert parsed["rate"] == truth["rate"] and parsed["company"] == truth["company"]
    assert slm.parse_answer("company=x;rate=5") is None


@pytest.mark.skipif(not Path(slm.MODEL_PATH).exists(), reason="train with: python -m occupancy.ml.slm train")
def test_trained_model_reads_mistyped_messages_the_regex_cannot():
    model = slm.load()
    data = slm.samples(25, 77, messages.HELD_OUT_TEMPLATES, noise=0.04)
    assert slm.score(model.extract, data)["exact"] > slm.score(regex_extract, data)["exact"] + 0.2
