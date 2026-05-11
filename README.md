# Walmart Price Monitor

A scheduled price monitor for Walmart product pages that runs `check_once.py`
every 30 minutes on free GitHub Actions. Each run appends new readings to
`price_history.json`, and the workflow commits this file back to the
repository so the history is kept between runs. Alerts land in Slack (or any
other Apprise-supported channel) on real price drops.

## Why this stack

Walmart product pages embed the full pricing payload in a single Next.js JSON
blob inside `<script id="__NEXT_DATA__">`. The scraper parses this blob and
reads the full product object in one extraction: current price, prior price,
list price, per-unit price, markdown flags (rollback / clearance / reducedPrice),
seller name and type (Walmart-direct vs marketplace), event-pricing flags
(priceFlip / specialBuy), brand, rating, review count, and availability.

Walmart has no JSON-LD or Open Graph price meta tags on product pages, and no
public API for unauthenticated reads; the JSON blob is the only canonical
source. CSS selectors are kept as a defensive fallback only.

The anti-bot layer is Akamai Bot Manager combined with HUMAN Security. Both
serve their soft challenge page with **HTTP 200 OK**, which means status code
alone cannot identify a block; the scraper has to inspect the response body
on every successful response. The two markers checked are the literal
challenge-UI text `"activate and hold the button"` and the title-tag
`"<title>robot or human"`. Generic terms like `"perimeterx"` or `"px-captcha"`
are deliberately not used. Walmart's Content Security Policy lists those
domains on every product page, which would produce false-positive challenges
on every real fetch.
A separate check covers Akamai's hard-block path (HTTP 307 redirect to
`/blocked?url=<base64>`). On any match, the request is retried through the
next proxy in the pool.

## Alert thresholds

The workflow sends an alert when **all** of the following are true:

1. The current price is **lower than the lowest price recorded in the last
   24 hours**. This is a new 24-hour low, not a small change from the previous
   reading.
2. The drop is **at least 2% and at least $1**. This filters out small changes
   of a few cents on low-cost items.
3. **No alert has been sent for the same product in the last 6 hours.** This
   stops repeated notifications when prices change often.

If any condition fails, the script still saves the reading to history but
does not send a notification.

The four limits, plus the history retention setting, are defined as constants
near the top of `check_once.py`:

```python
MIN_DROP_PCT = 2.0
MIN_DROP_DOLLARS = 1.00
COOLDOWN_HOURS = 6
BASELINE_WINDOW_HOURS = 24
HISTORY_RETENTION_DAYS = 30  # readings older than this are deleted automatically
```

Adjust these values to change the alert behavior. For example:

- To alert on any drop of 0.1% or larger, set `MIN_DROP_PCT = 0.1`.
- To allow at most one alert per product per day, set `COOLDOWN_HOURS = 24`.

## Slack alert format

When an alert fires, Slack receives a message like:

```
Price Drop: Great Value Paper Towels

Great Value Ultra Strong Paper Towels (12 Double Rolls)

Previous: $14.97
Current:  $11.98
Drop:     $2.99 (-19.97%)
vs Was:   $16.97 (-$4.99 / -29.41%)
Per unit: $1.14/100 ct
Tags:     ROLLBACK | EVENT-PRICING
Stock:    Limited stock

https://www.walmart.com/ip/186015888
```

The optional lines only appear when relevant:

- `vs Was` / `vs MSRP` appears only when Walmart shows a higher prior price
  or list price.
- `Per unit` appears for groceries and other unit-priced items.
- `Tags` combines the offer label (ROLLBACK / CLEARANCE / REDUCED), the
  event-pricing flag (`priceFlip` or `specialBuy` set on the page), and a
  `SOLD BY <name>` tag for marketplace items where the seller is not
  Walmart-direct.
- `Stock` appears only when the item is not currently in stock.

Walmart-direct prices are stable; marketplace seller prices change more
often, which is why the `SOLD BY` tag is surfaced separately so the reader
knows what kind of price they are looking at.

## File structure

```
.
├── .github/workflows/monitor.yml   # cron schedule and run logic
├── config.py                       # loads proxies from environment, validates products
├── scraper.py                      # curl_cffi scraper + __NEXT_DATA__ parser
├── alerts.py                       # Apprise multi-channel alerts (Slack-friendly format)
├── check_once.py                   # entry point: runs one price check per execution
├── products.json                   # list of Walmart item IDs to monitor
├── requirements.txt
├── .gitignore
└── price_history.json              # created automatically on the first run
```

## Setup (one-time, about 10 minutes)

Fork this repository (or clone it locally) so you have the starter files,
then follow these steps to configure your own copy.

### 1. Create a private GitHub repository

Use a private repository, not a public one. The workflow commits
`price_history.json` automatically, which records the products you monitor
and their price changes over time. A private repository keeps this data out
of search engine indexes.

### 2. Push these files to the repository

```bash
cd path/to/this/folder
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin git@github.com:YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 3. Create a Slack incoming webhook

1. Open https://api.slack.com/apps and click **Create New App** → **From scratch**.
2. Give it a name (e.g. "Walmart Price Monitor") and pick the workspace.
3. In the left sidebar choose **Incoming Webhooks** and switch it on.
4. Click **Add New Webhook to Workspace**, pick the channel that should
   receive alerts, and click **Allow**.
5. Copy the webhook URL. It looks like
   `https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX`.

### 4. Add two GitHub Secrets

In your repository, go to **Settings → Secrets and variables → Actions → New repository secret**, then add the following two secrets.

**Secret 1: `PROXIES`**

One proxy per line, in the format `host:port:user:password`:

```
proxy1.example.com:8000:your_user:your_pass
proxy2.example.com:8001:your_user:your_pass
proxy3.example.com:8002:your_user:your_pass
...
```

ISP proxies are strongly recommended for Walmart specifically. Datacenter
ASNs (AWS, OVH, Hetzner, DigitalOcean) hit the Akamai challenge on most
requests; consumer-ISP IPs pass through far more often.

**Secret 2: `APPRISE_URLS`**

For Slack via the webhook from step 3, convert the URL to Apprise's `slack://`
format. The simplest pattern is:

```
slack://TOKEN_A/TOKEN_B/TOKEN_C
```

where `TOKEN_A`, `TOKEN_B`, and `TOKEN_C` are the three path segments of your
webhook URL. For example, `https://hooks.slack.com/services/T0000/B0000/XYZ`
becomes:

```
slack://T0000/B0000/XYZ
```

You can also use multiple channels by putting one URL per line in the secret:

```
slack://T0000/B0000/XYZ
discord://webhook_id/webhook_token
mailto://you:app_password@gmail.com?to=you@gmail.com
```

For the full list of supported notification channels and their URL formats,
see the Apprise wiki: https://github.com/caronc/apprise/wiki

You can verify the Slack URL works before deploying:

```bash
pip install apprise
apprise -vv -t "test" -b "Price monitor test" "slack://T0000/B0000/XYZ"
```

A message should appear in the chosen Slack channel within a few seconds.

### 5. Edit `products.json`

Replace the example item IDs with the products you want to monitor. Each
entry needs two fields: `item_id` and a recognizable `name`. The `item_id` is
the numeric ID at the end of a Walmart product URL. For example, the URL
`https://www.walmart.com/ip/Great-Value-Ultra-Strong-Paper-Towels-Split-Sheets-12-Double-Rolls/186015888`
has the item ID `186015888`.

```json
{"item_id": "186015888", "name": "Great Value Paper Towels"}
```

There is no target price field. An alert is sent when a price drop crosses
the thresholds defined in `check_once.py`. Commit and push your changes when
you are done.

### 6. Trigger the first run manually

Open the **Actions** tab in your repository, select **Walmart Price Monitor**,
and click **Run workflow**. This first run confirms that your secrets are
configured correctly. After this, runs happen automatically every 30 minutes.

## Important limitations

- **Scheduled runs are not exact.** GitHub may delay cron triggers by 10 to
  30 minutes during periods of high load. For routine price monitoring this
  is acceptable. For time-sensitive cases such as flash sales, use a
  dedicated server instead.
- **GitHub pauses workflows after 60 days of repository inactivity.** This
  workflow's automatic commits count as activity, so the schedule does not
  pause in normal use.
- **The GitHub free tier gives 2,000 Actions minutes per month for private
  repositories.** Each run takes about 1 minute. With 48 runs per day across
  30 days, monthly usage is about 1,440 minutes, which is below the free tier
  limit. Public repositories have unlimited minutes.
- **The workflow includes a concurrency lock.** This stops two runs from
  writing to `price_history.json` at the same time.

## Changing the monitored products

Edit `products.json` and push the change to the repository. The next
scheduled run will use the updated list. No additional deployment step is
needed.

## Changing the run frequency

Edit the `cron` value in `.github/workflows/monitor.yml`:

```yaml
- cron: "*/15 * * * *"   # every 15 minutes
- cron: "0 * * * *"      # every hour
- cron: "0 */6 * * *"    # every 6 hours
```

## Troubleshooting

- **The workflow run failed.** Open the failed run in the Actions tab, expand
  the failed step, and read the log output. Most failures are caused by a
  missing or incorrectly formatted `PROXIES` or `APPRISE_URLS` secret.
- **All requests return the "Robot or human?" challenge.** Walmart's Akamai
  and HUMAN layers are more aggressive than Amazon's anti-bot system, so
  proxy quality matters more here. Make sure the proxies are consumer-ISP
  IPs, not datacenter IPs. Datacenter ASNs (AWS, OVH, Hetzner, DigitalOcean)
  hit the challenge on most Walmart requests.
- **No alerts arrive in Slack even when prices drop.** First verify the
  Slack URL with the `apprise -vv` command from step 4. Then check that the
  price drop crosses the thresholds in `check_once.py`. A $0.10 drop on a
  $30 item is below the default 2% threshold and will not trigger an alert.
- **The workflow is stuck in the "queued" state.** GitHub's free runners can
  be delayed during periods of high demand. The queue usually clears within
  5 to 15 minutes.
