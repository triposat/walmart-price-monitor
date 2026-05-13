# force_alert.py — sends one Slack alert with test data matching the post's example.
# Run with: APPRISE_URLS="slack://..." python force_alert.py
import os
import sys

if "APPRISE_URLS" not in os.environ:
    print("APPRISE_URLS env var is not set.")
    print('Run: APPRISE_URLS="slack://A/B/C" python force_alert.py')
    sys.exit(1)

# Stub PROXIES so config.py loads without raising.
os.environ.setdefault("PROXIES", "stub.example.com:8080:user:pass")

from scraper import PriceResult
from config import ProductConfig
from alerts import send_alert

product = ProductConfig(
    item_id="11381374703",
    name="Apple AirPods 4",
)

result = PriceResult(
    item_id="11381374703",
    title="Apple AirPods 4",
    brand="Apple",
    price=99.00,
    was_price=129.99,
    unit_price_display="$99.00/count",
    offer_type="rollback",
    is_reduced=True,
    seller_name="Walmart.com",
    seller_type="INTERNAL",
    availability="In stock",
    availability_code="IN_STOCK",
    rating="4.3",
    review_count=70988,
)

# prior_price = the 24-hour rolling baseline the scraper has seen, NOT wasPrice.
# Scenario: the scraper has been polling AirPods 4 for a week; the 24h low was
# sitting at $103.00; Walmart dropped the live price to $99.00; the scraper
# detects the drop. wasPrice ($129.99) shows separately as Walmart's marketing
# anchor in the "vs Was" line.
send_alert(result, product, prior_price=103.00)
print("Done. Check Slack.")
