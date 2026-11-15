from datetime import date

import httpx

from occupancy.intel import explain
from occupancy.intel.forecast import Forecast
from occupancy.intel.pricing import Recommendation


def rec():
    fc = Forecast(date(2026, 10, 6), 7, 87, 30.0, "Fintech Summit 2026", 6800)
    return Recommendation(date(2026, 10, 6), 6800, 8150, 9550, fc, 33, [{"code": "forecast"}])


def test_template_states_only_known_facts():
    f = explain.facts_for(rec())
    text = explain.template(f)
    assert "₹8,150" in text and "Fintech Summit 2026" in text and explain.check(text, f)


def test_check_rejects_invented_numbers():
    f = explain.facts_for(rec())
    assert not explain.check("Raise to 8150 because demand is up 40%.", f)


def test_llm_text_is_used_only_when_it_passes(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    def reply(text):
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
            )
        )

    good, src = explain.explain(rec(), reply("Raise to ₹8,150: the Fintech Summit 2026 week is filling."))
    assert src == "llm" and "8,150" in good
    _, src = explain.explain(rec(), reply("Raise by 35% since rivals charge 12,000."))
    assert src == "template"
