"""
DS5220 DP3 — Part 1: Data Ingestion Pipeline
Tracks Bitcoin, Ethereum, and Solana prices from CoinGecko every 15 minutes.
Stores timestamped records in DynamoDB and regenerates a price-history plot in S3.
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from chalice import Chalice, Rate

app = Chalice(app_name="arnavwx-ingestion")

# ---------------------------------------------------------------------------
# Configuration (override via Lambda environment variables)
# ---------------------------------------------------------------------------
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "arnavwx-prices")
S3_BUCKET = os.environ.get("S3_BUCKET", "arnavwx-dp3-plots")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

COINS = ["bitcoin", "ethereum", "solana"]
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana"
    "&vs_currencies=usd"
    "&include_market_cap=true"
    "&include_24hr_vol=true"
    "&include_24hr_change=true"
)

# Reuse boto3 clients across warm Lambda invocations
_dynamodb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)


# ---------------------------------------------------------------------------
# Scheduled ingestion handler — fires every 15 minutes
# ---------------------------------------------------------------------------
@app.schedule(Rate(15, unit=Rate.MINUTES))
def ingest(event):
    """Fetch current crypto prices and write a timestamped record per coin."""

    # 1. Fetch prices from CoinGecko (no API key required)
    try:
        req = urllib.request.Request(
            COINGECKO_URL, headers={"User-Agent": "ds5220-dp3/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        app.log.error(f"CoinGecko fetch failed: {exc}")
        return  # return cleanly so EventBridge retries next cycle

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = _dynamodb.Table(DYNAMODB_TABLE)

    # 2. Persist one record per coin
    for coin in COINS:
        if coin not in data:
            app.log.warning(f"Coin '{coin}' missing from API response.")
            continue

        coin_data = data[coin]
        item = {
            "coin": coin,
            "timestamp": timestamp,
            # Store numerics as Decimal — DynamoDB's native number type
            "price_usd": Decimal(str(round(coin_data.get("usd", 0), 8))),
            "market_cap": Decimal(str(round(coin_data.get("usd_market_cap", 0), 2))),
            "volume_24h": Decimal(str(round(coin_data.get("usd_24h_vol", 0), 2))),
            "price_change_24h_pct": Decimal(
                str(round(coin_data.get("usd_24h_change", 0), 6))
            ),
        }

        try:
            table.put_item(Item=item)
            app.log.info(
                f"Stored {coin} @ {timestamp}: "
                f"${float(item['price_usd']):,.2f} "
                f"({float(item['price_change_24h_pct']):+.2f}% 24h)"
            )
        except Exception as exc:
            app.log.error(f"DynamoDB put failed for {coin}: {exc}")

    # 3. Regenerate the BTC price chart and upload to S3
    try:
        _regenerate_plot(table)
    except Exception as exc:
        app.log.error(f"Plot generation failed: {exc}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _regenerate_plot(table):
    """
    Query the last 48 hours of BTC, ETH, and SOL prices, render a multi-line
    chart of percent change since the start of the window via QuickChart,
    and upload the PNG to S3. Normalizing to % change lets all three coins
    share a single y-axis despite very different absolute price scales.
    """
    start_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    coin_series = {}
    for coin in COINS:
        resp = table.query(
            KeyConditionExpression=(
                Key("coin").eq(coin) & Key("timestamp").gte(start_time)
            ),
            ScanIndexForward=True,
        )
        coin_series[coin] = {
            item["timestamp"]: float(item["price_usd"])
            for item in resp.get("Items", [])
        }

    # Intersect timestamps across all three coins so the series align on x
    common_ts = sorted(
        set.intersection(*(set(s.keys()) for s in coin_series.values()))
    )

    if len(common_ts) < 2:
        app.log.info("Not enough overlapping data yet to generate a plot — skipping.")
        return

    labels = [
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").strftime("%m/%d %H:%M")
        for ts in common_ts
    ]

    coin_meta = {
        "bitcoin":  {"label": "BTC", "color": "#F7931A"},
        "ethereum": {"label": "ETH", "color": "#627EEA"},
        "solana":   {"label": "SOL", "color": "#9945FF"},
    }

    datasets = []
    for coin in COINS:
        prices = [coin_series[coin][ts] for ts in common_ts]
        baseline = prices[0]
        pct_change = [((p / baseline) - 1) * 100 for p in prices]
        meta = coin_meta[coin]
        datasets.append(
            {
                "label": meta["label"],
                "data": pct_change,
                "borderColor": meta["color"],
                "backgroundColor": meta["color"],
                "borderWidth": 2,
                "fill": False,
                "pointRadius": 0,
                "tension": 0.2,
            }
        )

    chart_config = {
        "type": "line",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "title": {
                "display": True,
                "text": "BTC / ETH / SOL — % Change Over Last 48 Hours",
                "fontSize": 16,
            },
            "legend": {"position": "top"},
            "scales": {
                "xAxes": [
                    {
                        "scaleLabel": {"display": True, "labelString": "Time (UTC)"},
                        "ticks": {"maxRotation": 45, "minRotation": 45},
                    }
                ],
                "yAxes": [
                    {
                        "scaleLabel": {
                            "display": True,
                            "labelString": "% change from start of window",
                        },
                    }
                ],
            },
        },
    }

    body = json.dumps(
        {
            "chart": chart_config,
            "width": 1000,
            "height": 500,
            "backgroundColor": "white",
            "format": "png",
        }
    ).encode()

    req = urllib.request.Request(
        "https://quickchart.io/chart",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        png_bytes = resp.read()

    _s3.put_object(
        Bucket=S3_BUCKET,
        Key="latest-btc.png",
        Body=png_bytes,
        ContentType="image/png",
    )
    app.log.info(
        f"Plot uploaded: https://{S3_BUCKET}.s3.us-east-1.amazonaws.com/latest-btc.png"
    )
