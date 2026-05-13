#!/usr/bin/env python3
"""Local continuous runner — no proxies, no GitHub Actions.

Runs the same check_once.py logic on a loop on your laptop. Sends real
Slack alerts via Apprise. Works without ISP proxies for short-to-medium
durations because curl_cffi's chrome impersonation passes Akamai's
first-pass check from datacenter IPs.

USAGE:
    APPRISE_URLS="slack://A/B/C" python run_locally.py
    APPRISE_URLS="slack://A/B/C" python run_locally.py --interval 5    # check every 5 minutes
    APPRISE_URLS="slack://A/B/C" python run_locally.py --once         # one cycle and exit

The PROXIES env var is OPTIONAL. If unset, the scraper goes direct via
curl_cffi (verified to pass Akamai's first-pass check on initial requests).
If set, the scraper rotates through your proxy pool — recommended for
sustained scale where the same IP making hundreds of requests per day
would eventually flag.
"""
import argparse
import os
import sys
import time
import json
from datetime import datetime, timedelta

# Make PROXIES optional. If not set, we stub it out and patch the scraper
# to send requests without a proxy.
os.environ.setdefault("PROXIES", "stub.example.com:8080:user:pass")
NO_PROXY = "PROXIES" not in os.environ or os.environ["PROXIES"].startswith("stub.example.com")

from loguru import logger
from pydantic import TypeAdapter
from tinydb import TinyDB
from curl_cffi import requests as curl_requests

from scraper import WalmartPriceScraper
from config import ProductConfig
from alerts import send_alert, notifier
from storage import (
    prune_old_entries,
    get_baseline_price,
    get_last_alert_time,
    decide,
)

# If no real proxies, monkey-patch curl_requests.get to drop the proxy kwarg.
if NO_PROXY:
    _orig_get = curl_requests.get
    def _direct_get(url, **kwargs):
        kwargs.pop("proxy", None)
        return _orig_get(url, **kwargs)
    curl_requests.get = _direct_get
    logger.info("No real PROXIES set — going direct via curl_cffi chrome impersonation")


def run_one_cycle():
    """One full pass: scrape, compare, alert, persist."""
    with open("products.json") as f:
        data = json.load(f)
    products = TypeAdapter(list[ProductConfig]).validate_python(data["products"])

    db = TinyDB("price_history.json")
    prune_old_entries(db)
    scraper = WalmartPriceScraper()
    now = datetime.now()

    successes = failures = alerts_sent = suppressed = 0

    for product in products:
        result = scraper.get_price(product.item_id)
        if not (result and result.price is not None):
            logger.warning(f"FAIL: {product.name} ({product.item_id})")
            failures += 1
            continue

        baseline = get_baseline_price(db, product.item_id)
        last_alert_at = get_last_alert_time(db, product.item_id)
        should_alert, reason = decide(result.price, baseline, last_alert_at, now)

        successes += 1
        delivered = False

        if should_alert:
            assert baseline is not None
            logger.success(f"DROP! {product.name}: ${result.price:.2f} | {reason}")
            delivered = send_alert(result, product, baseline)
            if delivered:
                alerts_sent += 1
        elif baseline is not None and result.price < baseline:
            logger.info(f"{product.name}: ${result.price:.2f} | suppressed: {reason}")
            suppressed += 1
        else:
            logger.info(f"{product.name}: ${result.price:.2f} | {reason}")

        # Mark alerted only on confirmed delivery; matches check_once.py semantics.
        record = result.model_dump(mode="json")
        record["alerted"] = delivered
        db.insert(record)

    logger.info(
        f"Cycle done. {successes} ok, {failures} failed, "
        f"{alerts_sent} alerts sent, {suppressed} drops suppressed."
    )
    return successes, failures, alerts_sent


def main():
    parser = argparse.ArgumentParser(description="Continuous Walmart price monitor (local).")
    parser.add_argument("--interval", type=int, default=30,
                        help="Minutes between cycles (default: 30)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle and exit (useful for testing)")
    args = parser.parse_args()

    if len(notifier) == 0:
        logger.error("APPRISE_URLS is empty. Set it to your Slack webhook in "
                     "slack://A/B/C format, then re-run. See README for details.")
        sys.exit(2)

    logger.info(f"Apprise notifier loaded with {len(notifier)} channel(s)")
    logger.info(f"Interval: every {args.interval} minute(s){' (--once mode)' if args.once else ''}")

    if args.once:
        run_one_cycle()
        return

    cycle_num = 0
    while True:
        cycle_num += 1
        logger.info(f"=== Starting cycle {cycle_num} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
        try:
            run_one_cycle()
        except Exception as e:
            logger.error(f"Cycle {cycle_num} crashed: {e}")
        next_run = datetime.now() + timedelta(minutes=args.interval)
        logger.info(f"Sleeping until {next_run.strftime('%H:%M:%S')} (next cycle in {args.interval} min)")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, format="{time:HH:mm:ss} | {level: <7} | {message}")
    main()
