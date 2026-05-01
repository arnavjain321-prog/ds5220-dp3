"""
DS5220 DP3 — Part 2: Integration API
Chalice app deployed to API Gateway + Lambda.
Reads from the DynamoDB table populated by the ingestion pipeline
and exposes three resources consumable by the course Discord bot.

API Contract (must match exactly):
  GET /          → { "about": "...", "resources": ["current", "trend", "plot"] }
  GET /current   → { "response": "<string>" }
  GET /trend     → { "response": "<string>" }
  GET /plot      → { "response": "<s3-url>" }
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from chalice import Chalice

app = Chalice(app_name="arnavwx-api")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "arnavwx-prices")
S3_BUCKET = os.environ.get("S3_BUCKET", "arnavwx-dp3-plots")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Stable S3 URL — the ingestion Lambda overwrites this key after every run
PLOT_URL = f"https://{S3_BUCKET}.s3.{REGION}.amazonaws.com/latest-btc.png"

_dynamodb = boto3.resource("dynamodb", region_name=REGION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_float(val):
    """Convert DynamoDB Decimal (or anything) to float for JSON serialization."""
    if isinstance(val, Decimal):
        return float(val)
    return val


def _get_latest(coin: str = "bitcoin") -> Optional[Dict]:
    """Return the single most-recent record for a coin, or None."""
    table = _dynamodb.Table(DYNAMODB_TABLE)
    resp = table.query(
        KeyConditionExpression=Key("coin").eq(coin),
        ScanIndexForward=False,  # descending timestamp → first item is newest
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    return {k: _to_float(v) for k, v in items[0].items()}


def _get_history(coin: str = "bitcoin", hours: int = 24) -> List[Dict]:
    """Return all records for a coin within the last N hours, oldest-first."""
    table = _dynamodb.Table(DYNAMODB_TABLE)
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    resp = table.query(
        KeyConditionExpression=(
            Key("coin").eq(coin) & Key("timestamp").gte(start)
        ),
        ScanIndexForward=True,
    )
    return [{k: _to_float(v) for k, v in item.items()} for item in resp.get("Items", [])]


# ---------------------------------------------------------------------------
# Route: zone apex — MUST return exactly this shape for Discord bot
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return {
        "about": (
            "Tracks Bitcoin, Ethereum, and Solana prices every 15 minutes "
            "using the CoinGecko API. Stores a full time series in DynamoDB "
            "and publishes a live price-history chart to S3."
        ),
        "resources": ["current", "trend", "plot"],
    }


# ---------------------------------------------------------------------------
# Route: /current — most recent prices for BTC, ETH, SOL
# ---------------------------------------------------------------------------
@app.route("/current")
def current():
    btc = _get_latest("bitcoin")
    eth = _get_latest("ethereum")
    sol = _get_latest("solana")

    if not btc:
        return {
            "response": (
                "No data collected yet — the ingestion Lambda may still be "
                "warming up. Check back in a few minutes."
            )
        }

    ts = btc.get("timestamp", "unknown")
    parts = [f"BTC ${btc['price_usd']:,.2f}"]
    if eth:
        parts.append(f"ETH ${eth['price_usd']:,.2f}")
    if sol:
        parts.append(f"SOL ${sol['price_usd']:,.2f}")

    return {"response": f"{' | '.join(parts)}  (as of {ts} UTC)"}


# ---------------------------------------------------------------------------
# Route: /trend — 24-hour trend analysis for Bitcoin
# ---------------------------------------------------------------------------
@app.route("/trend")
def trend():
    items = _get_history("bitcoin", hours=24)

    # Fallback: use the stored 24h-change field from CoinGecko if we have < 2 samples
    if len(items) < 2:
        latest = _get_latest("bitcoin")
        if latest and "price_change_24h_pct" in latest:
            pct = latest["price_change_24h_pct"]
            arrow = "↑" if pct >= 0 else "↓"
            return {
                "response": (
                    f"BTC 24h change: {arrow} {pct:+.2f}% "
                    f"(CoinGecko-reported; still collecting local history)"
                )
            }
        return {"response": "Not enough data yet — check back after a few collection cycles."}

    oldest = items[0]["price_usd"]
    latest = items[-1]["price_usd"]
    pct_change = ((latest - oldest) / oldest) * 100
    dollar_change = latest - oldest
    arrow = "↑" if pct_change >= 0 else "↓"

    low = min(i["price_usd"] for i in items)
    high = max(i["price_usd"] for i in items)
    samples = len(items)
    window_h = round(samples * 15 / 60, 1)

    return {
        "response": (
            f"BTC over ~{window_h}h ({samples} samples): "
            f"{arrow} {pct_change:+.2f}% (${dollar_change:+,.2f}). "
            f"Range: ${low:,.2f} – ${high:,.2f}."
        )
    }


# ---------------------------------------------------------------------------
# Route: /plot — returns the S3 URL of the latest BTC price chart
# ---------------------------------------------------------------------------
@app.route("/plot")
def plot():
    return {"response": PLOT_URL}
