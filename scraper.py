# scraper.py: Walmart scraper with curl_cffi TLS impersonation and ISP proxy rotation

import json
import random
import time
from datetime import datetime
from itertools import cycle

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_random, retry_if_exception_type
from loguru import logger
from pydantic import BaseModel, Field

from config import PROXIES, REQUEST_TIMEOUT, MAX_RETRIES


class PriceResult(BaseModel):
    item_id: str
    title: str
    brand: str | None = None
    price: float | None = None
    was_price: float | None = None        # prior price when Walmart shows a markdown
    list_price: float | None = None       # MSRP / list price when present
    unit_price_display: str | None = None # "$1.14/100 ct" for groceries
    buybox_price_display: str | None = None  # marketplace buybox winner (when not Walmart-direct)
    is_reduced: bool = False
    offer_type: str | None = None         # "rollback" | "clearance" | "reducedPrice" | None
    seller_name: str | None = None
    seller_type: str | None = None        # "INTERNAL" (Walmart) or marketplace
    availability: str = "Unknown"         # human-readable display
    availability_code: str | None = None  # raw enum for downstream filtering
    rating: str | None = None
    review_count: int | None = None
    is_price_event: bool = False          # priceFlip or specialBuy event
    timestamp: datetime = Field(default_factory=datetime.now)


class RetryableError(Exception):
    """Temporary server-side error or anti-bot challenge.

    Raising this signals the retry decorator to try again with the next
    proxy in the pool. Permanent errors raise a plain Exception instead.
    """


# Walmart embeds the full product payload in a single Next.js JSON blob at
# props.pageProps.initialData.data.product. This is the canonical price
# source; Walmart has no JSON-LD or Open Graph price meta on product pages
# and no public API for unauthenticated reads.
NEXT_DATA_SELECTOR = "script#__NEXT_DATA__"

# Walmart's soft challenge returns HTTP 200 with a challenge body, so the
# body must be inspected on every successful response.
#
# These two markers are challenge-page specific. Generic terms like
# "perimeterx" or "px-captcha" would false-positive on real product pages
# because Walmart's Content Security Policy header lists *.perimeterx.net
# (and similar) as allowed third-party domains on every page it serves.
CHALLENGE_MARKERS = (
    "activate and hold the button",  # Akamai "Press & Hold" UI text
    "<title>robot or human",         # title tag, only on the challenge page
)

# The hard block is HTTP 307 -> /blocked?url=<base64>. After curl_cffi
# follows the redirect, the final URL contains this path.
BLOCKED_PATH = "/blocked"

# Defensive CSS fallback used only if __NEXT_DATA__ is missing.
PRICE_SELECTORS = [
    '[itemprop="price"]',
    '[data-automation-id="product-price"] span.f1',
    'span[data-testid="price-wrap"]',
    'span[data-seo-id="hero-price"]',
]


def _coerce_price(node):
    """Pull a float price from a Walmart price node, tolerating missing fields."""
    if not isinstance(node, dict):
        return None
    price = node.get("price")
    if price is None:
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def _extract_offer_type(price_display_codes):
    """Return the most specific offer flag, or None.

    priceDisplayCodes carries boolean-ish flags. `rollback` is Walmart's
    branded sale label, `clearance` is for clearance items, and
    `reducedPrice` is the generic markdown flag. The most specific label
    is returned first so reporting matches what Walmart shows the user.
    """
    if not isinstance(price_display_codes, dict):
        return None
    for key in ("rollback", "clearance", "reducedPrice"):
        if price_display_codes.get(key):
            return key
    return None


def extract_price_text(tag):
    if tag is None:
        return None
    text = tag.get("content") or tag.get_text(strip=True)
    if not text:
        return None
    try:
        return float(str(text).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


class WalmartPriceScraper:
    def __init__(self):
        # Empty PROXIES means run in direct mode (curl_cffi TLS impersonation
        # alone). Suitable for short-term testing; configure real ISP proxies
        # for sustained scale.
        # Shuffle on startup so a burned proxy is not retried first every cycle.
        # A real production version would track per-proxy health and quarantine
        # repeatedly-failing IPs; this is a low-cost first defense.
        self._proxy_pool = cycle(random.sample(PROXIES, len(PROXIES))) if PROXIES else None
        if self._proxy_pool is None:
            logger.warning(
                "No PROXIES configured — running in direct mode. "
                "Akamai may flag this IP after sustained requests."
            )

    def _get_next_proxy(self):
        if self._proxy_pool is None:
            return None
        return next(self._proxy_pool).url

    @retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_random(min=3, max=10),
        retry=retry_if_exception_type(RetryableError),
    )
    def fetch_product_page(self, item_id):
        # /ip/<id> works without the product-name slug; Walmart server-redirects
        # to the canonical URL on a real product page. Note: when Walmart
        # cannot geolocate the requester it defaults to ZIP 95829 (Sacramento);
        # prices and availability shown reflect that location.
        url = f"https://www.walmart.com/ip/{item_id}"
        proxy = self._get_next_proxy()

        kwargs = {"timeout": REQUEST_TIMEOUT, "impersonate": "chrome"}
        if proxy is not None:
            kwargs["proxy"] = proxy
        response = curl_requests.get(url, **kwargs)

        # 404 means the product page is gone. Skip it without retrying.
        if response.status_code == 404:
            logger.warning(f"Product {item_id} not found (404)")
            return None

        # 429 and 5xx are temporary; retry with the next proxy.
        if response.status_code == 429:
            raise RetryableError(f"Rate limited (429) for {item_id}")
        if 500 <= response.status_code < 600:
            raise RetryableError(f"Server error {response.status_code} for {item_id}")

        # Hard block: 307 -> /blocked?url=<base64>. After redirect follow, the
        # final URL contains /blocked. This is Akamai's pre-challenge gate.
        if BLOCKED_PATH in (response.url or ""):
            raise RetryableError(f"Akamai hard block (/blocked) for {item_id}")

        if response.status_code != 200:
            raise Exception(f"Permanent HTTP error {response.status_code} for {item_id}")

        # Soft challenge: HTTP 200 with a challenge body. Status alone is
        # not enough; the body must be inspected on every successful response.
        body_lower = response.text.lower()
        for marker in CHALLENGE_MARKERS:
            if marker in body_lower:
                raise RetryableError(f"Bot challenge ({marker!r}) for {item_id}")

        return response.text

    def parse_from_next_data(self, soup):
        """Read the full priceInfo + seller + availability + event blocks
        from the embedded Next.js JSON.

        Returns a dict ready for unpacking into PriceResult, or None if
        the JSON blob is missing or the product object cannot be located.
        """
        wrapper = soup.select_one(NEXT_DATA_SELECTOR)
        if wrapper is None:
            return None
        try:
            data = json.loads(wrapper.get_text())
        except (json.JSONDecodeError, ValueError):
            return None

        try:
            product = data["props"]["pageProps"]["initialData"]["data"]["product"]
        except (KeyError, TypeError):
            return None

        # Variants: the page picks one canonical variant based on the URL.
        # Other variants live in product.variantsMap; we monitor whichever
        # item_id the user listed, so no variant traversal is required.
        price_info = product.get("priceInfo") or {}
        current_price = _coerce_price(price_info.get("currentPrice"))
        was_price = _coerce_price(price_info.get("wasPrice"))
        list_price = _coerce_price(price_info.get("listPrice"))
        unit_price_obj = price_info.get("unitPrice") or {}
        unit_price_display = unit_price_obj.get("priceString") if isinstance(unit_price_obj, dict) else None

        is_reduced = bool(price_info.get("isPriceReduced"))
        offer_type = _extract_offer_type(price_info.get("priceDisplayCodes"))

        # Marketplace items have multiple sellers; topBoostedOffer is the
        # buybox winner. For Walmart-direct (INTERNAL) items this is all-null.
        # When populated it shows whose price the customer actually sees.
        tbo = product.get("topBoostedOffer") or {}
        buybox_price_display = tbo.get("priceString") if isinstance(tbo, dict) else None

        # availabilityStatusV2 provides a human-readable display string;
        # availabilityStatus is the raw enum. Keep both — display for alerts,
        # code for downstream filtering.
        avail_v2 = product.get("availabilityStatusV2") or {}
        availability = avail_v2.get("display") if isinstance(avail_v2, dict) else None
        availability = availability or product.get("availabilityStatus") or "Unknown"
        availability_code = product.get("availabilityStatus")

        # Event pricing flags. These distinguish a permanent markdown
        # ("rollback") from an event-driven temporary price ("priceFlip"
        # or "specialBuy" — Walmart's terms for limited-time events).
        event_attrs = product.get("eventAttributes") or {}
        is_price_event = bool(event_attrs.get("priceFlip") or event_attrs.get("specialBuy"))

        rating_value = product.get("averageRating")
        rating = f"{rating_value:.1f}" if isinstance(rating_value, (int, float)) else None

        review_count_value = product.get("numberOfReviews")
        review_count = int(review_count_value) if isinstance(review_count_value, (int, float)) else None

        return {
            "title": product.get("name") or "Unknown",
            "brand": product.get("brand"),
            "price": current_price,
            "was_price": was_price,
            "list_price": list_price,
            "unit_price_display": unit_price_display,
            "buybox_price_display": buybox_price_display,
            "is_reduced": is_reduced,
            "offer_type": offer_type,
            "seller_name": product.get("sellerName"),
            "seller_type": product.get("sellerType"),
            "availability": availability,
            "availability_code": availability_code,
            "rating": rating,
            "review_count": review_count,
            "is_price_event": is_price_event,
        }

    def parse_price_from_css(self, soup):
        """Fallback parser used only if __NEXT_DATA__ is missing."""
        for selector in PRICE_SELECTORS:
            price = extract_price_text(soup.select_one(selector))
            if price is not None:
                return price
        return None

    def parse_product_info(self, html, item_id):
        soup = BeautifulSoup(html, "lxml")

        # JSON-first: matches Walmart's server-rendered architecture.
        parsed = self.parse_from_next_data(soup)
        if parsed is not None:
            return PriceResult(item_id=item_id, **parsed)

        # Fallback path. Reachable only if Walmart removes or restructures
        # __NEXT_DATA__, which has not happened in years of practice.
        logger.warning(f"__NEXT_DATA__ missing for {item_id}; falling back to CSS")
        title_tag = soup.select_one("h1[itemprop='name']") or soup.select_one("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown"

        price = self.parse_price_from_css(soup)

        return PriceResult(
            item_id=item_id, title=title, price=price,
            availability="Unknown", rating=None,
        )

    def get_price(self, item_id):
        # Random delay between requests breaks the uniform timing pattern
        # that anti-bot systems use as one of their detection signals.
        time.sleep(random.uniform(3, 7))

        # Catch RetryableError only. fetch_product_page raises a plain Exception
        # for permanent 4xx responses that should NOT retry across products;
        # those need to surface to the caller, not get swallowed here.
        try:
            html = self.fetch_product_page(item_id)
        except RetryableError as e:
            logger.error(f"Retries exhausted for {item_id}: {e}")
            return None

        if html is None:
            return None
        return self.parse_product_info(html, item_id)
