# alerts.py: multi-channel alerts via apprise, configured from environment variable.
#
# Slack-friendly formatting:
#   - Plain text body so Slack auto-renders the product URL as a clickable link.
#   - Tagged seller type ("WALMART" vs "3RD-PARTY") because third-party seller
#     prices are noisier than Walmart-direct prices and the reader should know.
#   - Offer-type tag (ROLLBACK / CLEARANCE / REDUCED) and event-pricing tag
#     surfaced when present, since these affect how the reader should act.

import os
import apprise
from loguru import logger
from scraper import PriceResult
from config import ProductConfig

notifier = apprise.Apprise()

# Each line in APPRISE_URLS is one apprise notification URL. For Slack:
#   slack://TOKEN_A/TOKEN_B/TOKEN_C
# from a Slack incoming webhook https://hooks.slack.com/services/A/B/C.
# Other channels (one per line, multiple supported):
#   discord://webhook_id/webhook_token
#   tgram://bot_token/chat_id
#   mailto://user:app_password@gmail.com?to=you@gmail.com
# Full list: https://github.com/caronc/apprise/wiki
for url in os.environ.get("APPRISE_URLS", "").strip().splitlines():
    url = url.strip()
    if url:
        notifier.add(url)


def _format_body(result: PriceResult, prior_price: float) -> str:
    """Build the Slack-friendly alert body.

    Layout, in this order so the reader sees what matters first:
      1. Product title (from the live page, not the configured nickname)
      2. Previous / Current / Drop (the 24-hour baseline comparison)
      3. vs MSRP / vs Was Price (when Walmart shows one — separate signal)
      4. Per-unit price (when Walmart shows one — useful on groceries)
      5. Tags: offer-type, event-pricing, third-party-seller (when relevant)
      6. Stock status
      7. Product URL (last so it does not push content out of view)
    """
    # Caller has verified result.price is not None.
    assert result.price is not None

    drop = prior_price - result.price
    pct = (drop / prior_price) * 100

    lines = [
        result.title,
        "",
        f"Previous: ${prior_price:.2f}",
        f"Current:  ${result.price:.2f}",
        f"Drop:     ${drop:.2f} (-{pct:.2f}%)",
    ]

    # MSRP comparison. Walmart's `wasPrice` is the most recent prior price
    # and `listPrice` is the manufacturer's suggested price. Surface
    # whichever is present and higher than the current price.
    msrp_anchor = None
    msrp_label = None
    if result.was_price is not None and result.was_price > result.price:
        msrp_anchor = result.was_price
        msrp_label = "vs Was"
    elif result.list_price is not None and result.list_price > result.price:
        msrp_anchor = result.list_price
        msrp_label = "vs MSRP"
    if msrp_anchor is not None and msrp_label is not None:
        msrp_drop = msrp_anchor - result.price
        msrp_pct = (msrp_drop / msrp_anchor) * 100
        lines.append(f"{msrp_label}:   ${msrp_anchor:.2f} (-${msrp_drop:.2f} / -{msrp_pct:.2f}%)")

    if result.unit_price_display:
        lines.append(f"Per unit: {result.unit_price_display}")

    # Marketplace buybox: when the seller is not Walmart-direct, surface
    # the buybox seller's displayed price. This can differ from the
    # listing's currentPrice on multi-seller items.
    if result.buybox_price_display and result.seller_type and result.seller_type != "INTERNAL":
        lines.append(f"Buybox:   {result.buybox_price_display}")

    # Tag row: Walmart's branded offer label and event-pricing flag.
    tags = []
    if result.offer_type:
        tags.append(result.offer_type.replace("Price", "").upper())
    if result.is_price_event:
        # priceFlip / specialBuy are limited-time events; the reader
        # should treat the alert with more urgency than a regular markdown.
        tags.append("EVENT-PRICING")
    # Third-party-seller flag. Walmart-direct prices are stable; marketplace
    # seller prices change more often. INTERNAL = Walmart, anything else
    # is a third-party seller.
    if result.seller_type and result.seller_type != "INTERNAL":
        seller = result.seller_name or "3rd-party"
        tags.append(f"SOLD BY {seller.upper()}")
    if tags:
        lines.append(f"Tags:     {' | '.join(tags)}")

    if result.availability and result.availability not in ("Unknown", "In stock"):
        lines.append(f"Stock:    {result.availability}")

    lines.append("")
    lines.append(f"https://www.walmart.com/ip/{result.item_id}")

    return "\n".join(lines)


def send_alert(result: PriceResult, product: ProductConfig, prior_price: float):
    """Send a price-drop alert. The caller has already verified current < prior."""
    assert result.price is not None, "send_alert requires a non-None result.price"

    title = f"Price Drop: {product.name}"
    body = _format_body(result, prior_price)

    if len(notifier) > 0:
        # Apprise returns False when delivery fails. Without this check, a
        # broken webhook would still log success and the failure would be silent.
        if notifier.notify(title=title, body=body):
            logger.success(
                f"Alert sent for {result.item_id}: ${result.price:.2f} (was ${prior_price:.2f})"
            )
        else:
            logger.error(
                f"Alert delivery failed for {result.item_id}: "
                f"${result.price:.2f} (was ${prior_price:.2f})"
            )
    else:
        logger.warning(f"No notification services configured! {title}")
