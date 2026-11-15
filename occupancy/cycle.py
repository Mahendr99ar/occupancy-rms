"""One full cycle of the pipeline, in order:

crawl OTAs -> pull PMS bookings, turnaways, inbox, events -> normalize
-> feed checks -> recommendations -> push approved rates

  python -m occupancy.cycle                  # once
  python -m occupancy.cycle --advance        # move the market one day first (demo/test)
  python -m occupancy.cycle --every 900      # forever, every 15 minutes
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import requests

from . import config
from .intel import engine as pricing
from .pipeline import ingest, monitor, normalize


def run_once(advance: bool = False, auto_within: float = 0.0, days: int = config.CRAWL_DAYS_AHEAD) -> dict:
    t0 = time.monotonic()
    out: dict = {}
    if advance:
        out["market_day"] = requests.post(f"{config.MARKET_URL}/admin/advance", timeout=60).json()["today"]
    if config.MONGO_URL.startswith("mongomock://"):
        # An in-memory Mongo lives inside one process, so the crawl has to run
        # here for the feed checks to see its raw pages. Once per process only.
        from .crawl.run import crawl

        out["crawl"] = [crawl(days)]
    else:
        done = subprocess.run(
            [sys.executable, "-m", "occupancy.crawl.run", "--days", str(days)], capture_output=True, text=True
        )
        out["crawl"] = done.stdout.strip().splitlines()[-1:] or [done.stderr.strip()[-300:]]
    out["ingest"] = ingest.all_feeds()
    out["normalized"] = normalize.run(once=True)
    checks = monitor.run(days_ahead=days)
    out["checks"] = {s: sum(c["status"] == s for c in checks) for s in ("pass", "warn", "fail")}
    out["recommend"] = pricing.run_recommend(auto_within=auto_within)
    out["push"] = pricing.run_push()
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--advance", action="store_true")
    p.add_argument("--auto-within", type=float, default=0.0)
    p.add_argument("--every", type=int, default=0, help="seconds between cycles; 0 runs once")
    args = p.parse_args()
    if args.every and config.MONGO_URL.startswith("mongomock://"):
        p.error("--every needs a real MongoDB; with mongomock run one cycle per process")
    while True:
        try:
            print(json.dumps(run_once(args.advance, args.auto_within), default=str), flush=True)
        except Exception as e:  # keep the loop alive; the feed checks will show the gap
            print(json.dumps({"error": repr(e)}), flush=True)
            if not args.every:
                raise
        if not args.every:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
