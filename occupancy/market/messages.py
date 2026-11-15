"""Corporate rate requests, the way they arrive in a revenue manager's inbox:
short, inconsistent, full of abbreviations.

Each message comes with the structured answer it encodes, so the extraction
model can be trained and scored. Templates are split into two groups: the
model trains on TRAIN_TEMPLATES only and is tested on HELD_OUT_TEMPLATES, so
the score says something about phrasings it has never seen.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

COMPANIES = (
    "Acme Pharma",
    "Nexa Logistics",
    "Bluefin Capital",
    "Kestrel Motors",
    "Indus Textiles",
    "Vertex Infotech",
    "Sahyadri Foods",
    "Orbit Telecom",
    "Coastal Freight",
    "Lotus Biotech",
    "Granite Builders",
    "Monsoon Media",
)
ROOMS = {
    "Deluxe": ("deluxe", "Deluxe Room", "DLX", "deluxe king"),
    "Executive": ("executive", "Exec room", "EXE", "executive twin"),
    "Suite": ("suite", "Jr suite", "STE", "junior suite"),
}
MEALS = {
    "EP": ("room only", "EP", "without breakfast"),
    "CP": ("with breakfast", "CP", "incl. bfast", "breakfast included"),
    "MAP": ("breakfast + dinner", "MAP", "half board"),
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_LONG = (
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
)


def _ord(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _money(r: np.random.Generator, x: int) -> str:
    style = int(r.integers(0, 5))
    return [f"INR {x:,}", f"Rs.{x}", f"Rs {x:,}/-", f"{x}/- per night", f"₹{x:,}"][style]


def _dates(r: np.random.Generator, ci: date, co: date) -> str:
    style = int(r.integers(0, 4))
    if style == 0:
        return (
            f"{ci.day}-{co.day} {MONTHS[co.month - 1]}"
            if ci.month == co.month
            else f"{ci.day} {MONTHS[ci.month - 1]} - {co.day} {MONTHS[co.month - 1]}"
        )
    if style == 1:
        return (
            f"{_ord(ci.day)} to {_ord(co.day)} {MONTHS_LONG[co.month - 1]}"
            if ci.month == co.month
            else f"{_ord(ci.day)} {MONTHS_LONG[ci.month - 1]} to {_ord(co.day)} {MONTHS_LONG[co.month - 1]}"
        )
    if style == 2:
        return f"check in {ci:%d/%m} out {co:%d/%m}"
    return f"arr {ci:%d.%m} dep {co:%d.%m}"


TRAIN_TEMPLATES = (
    "Hi team, pls load {company} rate for {rooms} {room} rooms {dates} at {money} {meal}. Thanks",
    "Dear Sir, {company} needs {rooms} x {room} {dates}, agreed rate {money} {meal}.",
    "{company}: {rooms} {room}, {dates}, {money}, {meal}",
    "Request from {company} - block {rooms} {room} for {dates}. Rate {money} ({meal}).",
    "Can you confirm {money} {meal} for {company}? {rooms} rooms, {room}, {dates}.",
    "pls update corp rate {company} {room} {money} {meal} {dates} qty {rooms}",
)
HELD_OUT_TEMPLATES = (
    "Good morning. For our guests from {company} we need {rooms} {room} rooms, {dates}, at {money}, {meal}. Regards",
    "RATE LOAD | {company} | {room} | {dates} | {money} | {meal} | rooms: {rooms}",
)


GREETINGS = ("", "Hi, ", "Hello team, ", "Dear Revenue Team, ", "Hi Priya, ", "pls note: ", "Urgent - ", "FYI ")
SIGNOFFS = ("", " Thanks", " Thx", ". Pls confirm", " - sent from mobile", ". Best")
SEPARATORS = (", ", " ", "; ", " / ", ". ")


def composed(r: np.random.Generator, f: dict) -> str:
    """Build a message from loose clauses in a random order, so the
    extraction model learns what the fields look like rather than where a
    template puts them. None of the held-out phrasing is used here."""

    def pick(*xs):
        return xs[int(r.integers(0, len(xs)))]

    clauses = [
        pick(f"{f['company']}", f"for {f['company']}", f"client {f['company']}", f"{f['company']} corp rate"),
        pick(
            f"{f['rooms']} {f['room']}",
            f"{f['rooms']} x {f['room']}",
            f"{f['room']} x{f['rooms']}",
            f"{f['room']}, qty {f['rooms']}",
            f"{f['rooms']} rooms {f['room']}",
            f"{f['room']} rooms: {f['rooms']}",
        ),
        pick(f["dates"], f"stay {f['dates']}", f"dates {f['dates']}"),
        pick(f["money"], f"@ {f['money']}", f"rate {f['money']}", f"nett {f['money']}"),
        pick(f["meal"], f"({f['meal']})", f"basis {f['meal']}"),
    ]
    head, rest = clauses[0], clauses[1:]
    r.shuffle(rest)
    if r.random() < 0.3:  # company does not always come first
        pos = int(r.integers(0, len(rest) + 1))
        body = rest[:pos] + [head] + rest[pos:]
    else:
        body = [head] + rest
    sep = pick(*SEPARATORS)
    return pick(*GREETINGS) + sep.join(body) + pick(*SIGNOFFS)


def typos(text: str, r: np.random.Generator, p: float) -> str:
    """Typing slips on letters only (dropped, doubled or swapped), never on
    digits, so the true answer stays recoverable from the text."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isalpha() and r.random() < p:
            kind = int(r.integers(0, 3))
            if kind == 0:
                i += 1
                continue
            if kind == 1:
                out.append(ch + ch)
                i += 1
                continue
            if i + 1 < len(text) and text[i + 1].isalpha():
                out.append(text[i + 1] + ch)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def make(r: np.random.Generator, today: date, templates=TRAIN_TEMPLATES, noise: float = 0.0) -> tuple[str, dict]:
    """`templates=None` composes the message from clauses instead."""
    ci = today + timedelta(days=int(r.integers(5, 60)))
    co = ci + timedelta(days=int(r.integers(1, 5)))
    room = str(r.choice(list(ROOMS)))
    meal = str(r.choice(list(MEALS)))
    rate = int(r.integers(45, 140)) * 50
    rooms = int(r.integers(1, 16))
    company = str(r.choice(COMPANIES))
    fields = dict(
        company=company,
        rooms=rooms,
        room=str(r.choice(ROOMS[room])),
        dates=_dates(r, ci, co),
        money=_money(r, rate),
        meal=str(r.choice(MEALS[meal])),
    )
    text = composed(r, fields) if templates is None else str(r.choice(templates)).format(**fields)
    if noise:
        text = typos(text, r, noise)
    truth = {
        "company": company,
        "room_type": room,
        "check_in": ci.isoformat(),
        "check_out": co.isoformat(),
        "rate": rate,
        "meal_plan": meal,
        "rooms": rooms,
    }
    return text, truth


def rate_request(r: np.random.Generator, today: date, n: int) -> dict:
    text, truth = make(r, today, TRAIN_TEMPLATES + HELD_OUT_TEMPLATES, noise=0.04)
    return {"id": f"M{today:%y%m%d}{n:04d}", "received": today.isoformat(), "text": text, "truth": truth}
