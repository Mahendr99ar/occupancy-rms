# Design decisions

Notes on the choices that shaped the system, including the ones that went wrong first.

## A simulated market instead of scraping real OTAs

Scraping Booking.com or MakeMyTrip would break their terms and would give data that cannot be checked. The project needs something it can measure against: the true demand, the true elasticity, the guests who were turned away. So `occupancy/market` simulates one city (a client hotel and eight competitors) and `occupancy/ota` serves it through three fake booking sites, a PMS, a channel manager and a booking engine, each with the kind of mess the real ones have:

- StayKart changes its HTML layout after a few business days and renames one listing.
- TripNest is a paginated JSON API that answers 429 when the crawler is too fast.
- RoomBazaar draws its prices as images, so they have to be read.
- About 4% of OTA prices break rate parity with the hotel's own rate.
- The channel manager fails 5% of pushes with a 503.

Every number in the README comes from this simulator, and the README says so. The simulator is the author's model of a market, so the revenue result says the engine works on that model. It does not say what it would earn for a real hotel.

## Common random numbers for the revenue comparison

Comparing the engine with fixed rates on two separate random runs would mostly measure noise. The simulator draws every random quantity from a generator seeded by (seed, purpose, date), so two copies of the market see the same potential guests on the same days. Only the prices differ, and only the pricing can explain a difference in revenue. Revenue is counted only for stay dates whose whole 30-day selling window was priced by the policy under test.

## Turned-away guests count as demand

The first version of the engine lost RevPAR against fixed rates (about -3%). The cause was the forecast. On a sold-out night the PMS shows 120 rooms sold, which is the hotel's capacity, not its demand. A pickup forecast built on those nights says "demand is about 120", the optimiser sees no reason to raise the rate, and the hotel keeps selling out at the old price.

Two changes fixed it:

1. The PMS exposes turnaways (requests declined because no room was left), the way a real PMS logs denials. The forecast adds them to pickup.
2. Sold-out stay dates are treated as censored: they count as a lower bound on demand, not as an observation of it.

With both changes the engine went from -3% to about +6% RevPAR on seed 7 (see the README for five seeds). Fixing a zero-pickup floor bug and anchoring expected demand on the price the historical pickup was sold at came first and got it to -1.7%.

The forecast is therefore of unconstrained demand, which can exceed 120. The backtest scores the capped value (`Forecast.sold`), because that is what can be compared with rooms actually sold.

## The pricing rule is simple on purpose

`pricing.recommend` searches a rate grid for the rate that maximises expected revenue, with demand scaled by a constant elasticity around the price the historical pickup was sold at. It then applies guardrails: a floor and ceiling relative to the base rate, at most a 20% move per day, and no move at all when almost nothing is left to sell into. Every rule that fires is recorded as a reason, and the reasons are what the explanation text is built from. A revenue manager can read why each number came out the way it did.

The policy uses elasticity 2.0 while the simulator's guests have 2.6. The engine was not tuned on the true value.

## Cancellation model without the deposit type

The cancellation model trains on the public Hotel Booking Demand dataset (Antonio, Almeida and Nunes, 2019). In that data 99.4% of the 14,587 bookings with deposit type "Non Refund" are marked cancelled. This is a known quirk of the source; the likely cause is group blocks released in bulk. With that column the model reached AUC 0.805, but it learned that non-refundable means certain to cancel. On the dashboard the "likely to cancel" list was then full of non-refundable bookings at 100%, which is backwards for any hotel. The feature was removed. The best AUC fell to 0.757 and the list now ranks bookings by lead time, segment and history, which is what a revenue manager would expect.

## A small model trained from scratch for the inbox

Corporate rate requests arrive as short, inconsistent emails. Sending each one to a hosted LLM would work, but it costs money per message and sends client rates outside the hotel. The task is narrow, so a character-level transformer of 653,632 parameters is trained from scratch (`occupancy/ml/slm.py`).

Three things mattered:

- **Held-out phrasings.** The model trains on six templates plus messages composed from loose clauses in random order. It is scored on two templates it has never seen. A first model trained on the six templates alone scored 0.04 exact match on the held-out ones: it had learned where the templates put each field, not what the fields look like. The current one scores 0.69.
- **Typos.** A regex baseline written against the templates scores 0.95 on clean held-out messages and 0.25 when 4% of letters are mistyped. The model scores 0.72 on the mistyped ones. The inbox has typos, so this is where a learned model earns its place.
- **Schema-constrained decoding.** Field names and separators are written in, not predicted, and each value may only use characters that can form a legal value (digits for the rate, one of three room types, and so on). The model still picks every value character.

The two are used together. On clean held-out text, every record the regex returned was correct, so the normalizer tries the regex first and sends only the messages it cannot parse to the model service. That combination scores 0.96 on clean held-out messages and 0.76 with typos, better than either alone. The dashboard shows which one read each request.

## LLM explanations are checked, not trusted

`intel/explain.py` can ask an LLM (Anthropic or any OpenAI-compatible server, including a local one) to explain a rate change in one or two sentences. The reply is accepted only if every number in it appears in the facts that were sent. Otherwise the template explanation is used. Without an API key everything still works on the template.

## Raw pages in MongoDB, clean rows in MySQL

The crawler stores every raw page and price image in MongoDB before parsing. When a parser breaks (the StayKart layout change), the raw pages can be re-parsed once it is fixed, and the feed monitor reads the crawl stats from there. Clean, typed rows go to MySQL, where uniqueness constraints make every write an upsert. Redis Streams sits between them. The normalizer is a consumer group that acknowledges messages only after the database commit, and reclaims messages a crashed worker read but never acknowledged.

## Things that are not done

- MongoDB was tested with mongomock only. No MongoDB server was available while building this, so the local runs use the in-process mock and run the crawl in the same process.
- The Docker image and compose file were written but not built here (no registry access).
- The LLM path is tested against a fake HTTP server, not a real provider.
- The engine's elasticity is fixed. `pricing.estimate_elasticity` exists but is not wired in.
- There are no user accounts. Approving a rate needs one shared bearer token.
