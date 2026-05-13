# check_once.py: single-shot price check, designed to be invoked by cron or GitHub Actions.
#
# Storage + decision logic lives in storage.py. This module is the runner only:
# load products, fetch prices, persist readings, deliver alerts, and exit with
# a code GitHub Actions or systemd can use to fire failure notifications.

import sys
import json
from datetime import datetime
from loguru import logger
from pydantic import TypeAdapter
from tinydb import TinyDB

from scraper import WalmartPriceScraper
from config import ProductConfig
from alerts import send_alert
from storage import (
    prune_old_entries,
    get_baseline_price,
    get_last_alert_time,
    decide,
)


logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")

ProductList = TypeAdapter(list[ProductConfig])


def main():
    with open("products.json") as f:
        data = json.load(f)
    products = ProductList.validate_python(data["products"])

    logger.info(f"Checking {len(products)} products")

    db = TinyDB("price_history.json")
    prune_old_entries(db)
    scraper = WalmartPriceScraper()
    now = datetime.now()

    successes = failures = drops_alerted = drops_suppressed = 0

    for product in products:
        result = scraper.get_price(product.item_id)

        if not (result and result.price is not None):
            logger.warning(f"Failed to get price for {product.name} ({product.item_id})")
            failures += 1
            continue

        current = result.price
        baseline = get_baseline_price(db, product.item_id)
        last_alert_at = get_last_alert_time(db, product.item_id)

        should_alert, reason = decide(current, baseline, last_alert_at, now)
        successes += 1
        delivered = False

        if should_alert:
            # decide() only returns True when baseline is not None,
            # so the assertion is safe and silences type checkers.
            assert baseline is not None
            logger.success(
                f"PRICE DROP! {product.name}: ${current:.2f} | {reason}"
            )
            delivered = send_alert(result, product, baseline)
            if delivered:
                drops_alerted += 1
        elif baseline is not None and current < baseline:
            # A drop happened but did not meet the threshold or the cooldown.
            logger.info(f"{product.name}: ${current:.2f} | suppressed: {reason}")
            drops_suppressed += 1
        else:
            logger.info(f"{product.name}: ${current:.2f} | {reason}")

        # Mark alerted only on confirmed delivery. A failed alert leaves
        # alerted=False so the next cycle can retry once the cooldown logic
        # sees no recent successful alert.
        record = result.model_dump(mode="json")
        record["alerted"] = delivered
        db.insert(record)

    logger.info(
        f"Cycle done. {successes} ok, {failures} failed, "
        f"{drops_alerted} alert(s) sent, {drops_suppressed} drop(s) suppressed."
    )

    if successes == 0 and failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
