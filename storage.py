# storage.py: TinyDB-backed price history + decision layer.
#
# Alert policy: fire only when all three are true:
#   1. Current price is a new low across the 24-hour window.
#   2. Drop is at least 2% AND at least $1.
#   3. No alert has fired on this product in the last 6 hours.

from datetime import datetime, timedelta
from loguru import logger
from tinydb import Query


MIN_DROP_PCT = 2.0          # alert on drops of at least 2%
MIN_DROP_DOLLARS = 1.00     # AND at least $1 in absolute terms (stricter limit applies)
COOLDOWN_HOURS = 6          # do not re-alert on the same product within 6 hours
BASELINE_WINDOW_HOURS = 24  # baseline = lowest price seen in the last 24 hours
HISTORY_RETENTION_DAYS = 30 # readings older than this are removed automatically


def prune_old_entries(db, retention_days=HISTORY_RETENTION_DAYS):
    """Delete readings older than retention_days to bound the file size."""
    P = Query()
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    removed = db.remove(P.timestamp < cutoff)
    if removed:
        logger.info(f"Pruned {len(removed)} entries older than {retention_days}d")


def get_baseline_price(db, item_id, window_hours=BASELINE_WINDOW_HOURS):
    """Return the lowest price seen for this item_id in the last window_hours, or None."""
    P = Query()
    cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    recent = db.search((P.item_id == item_id) & (P.timestamp >= cutoff))
    prices = [r["price"] for r in recent if r.get("price") is not None]
    return min(prices) if prices else None


def get_last_alert_time(db, item_id):
    """Return the timestamp of the most recent alerted reading, or None."""
    P = Query()
    alerted = db.search((P.item_id == item_id) & (P.alerted == True))  # noqa: E712
    if not alerted:
        return None
    most_recent = max(alerted, key=lambda r: r.get("timestamp", ""))
    return datetime.fromisoformat(most_recent["timestamp"])


def decide(current, baseline, last_alert_at, now):
    """Pure function. Returns (should_alert, reason_string)."""
    if baseline is None:
        return False, "no recent baseline (re-establishing)"

    if current >= baseline:
        if current == baseline:
            return False, f"matches 24h low (${baseline:.2f})"
        return False, f"above 24h low ${baseline:.2f}"

    drop = baseline - current
    pct = (drop / baseline) * 100

    if drop < MIN_DROP_DOLLARS or pct < MIN_DROP_PCT:
        return False, (
            f"drop -${drop:.2f}/-{pct:.2f}% below threshold "
            f"(need >=${MIN_DROP_DOLLARS:.2f} AND >={MIN_DROP_PCT}%)"
        )

    if last_alert_at is not None:
        hours_since = (now - last_alert_at).total_seconds() / 3600
        if hours_since < COOLDOWN_HOURS:
            return False, (
                f"cooldown active ({hours_since:.1f}h since last alert, "
                f"need {COOLDOWN_HOURS}h)"
            )

    return True, f"new 24h low (was ${baseline:.2f}, drop -${drop:.2f}/-{pct:.2f}%)"
