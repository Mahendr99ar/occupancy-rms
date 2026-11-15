import numpy as np
import pytest

from occupancy.ml import price_reader as pr
from occupancy.ota import price_image as pim


def test_indian_digit_grouping():
    assert pim.price_text(7450) == "₹7,450"
    assert pim.price_text(125000) == "₹1,25,000"
    assert pim.price_text(950) == "₹950"


@pytest.fixture(scope="module")
def reader():
    return pr.PriceReader.train(n=800, seed=3)


def test_reads_prices_in_a_font_it_never_trained_on(reader):
    result = pr.evaluate(reader, n=150, seed=11, fonts=(pim.HELD_OUT_FONT,))
    assert result["exact"] >= 0.9


def test_returns_none_rather_than_guessing(reader):
    blank = pim.render("", np.random.default_rng(0), pim.TRAIN_FONTS[0])
    assert reader.read(blank) is None
