# DS5220 DP3 — Crypto Price Tracker (`arnavwx`)

A two-part cloud pipeline that tracks Bitcoin, Ethereum, and Solana prices every 15 minutes using the [CoinGecko](https://www.coingecko.com/en/api) public API, stores a full time series in DynamoDB, and exposes the data through an API Gateway + Lambda service registered with the course Discord bot.

---

## Data Source

**CoinGecko `/simple/price` endpoint** — free, no API key required, globally reliable.

- Coins tracked: **Bitcoin (BTC)**, **Ethereum (ETH)**, **Solana (SOL)**
- Fields collected per sample: `price_usd`, `market_cap`, `volume_24h`, `price_change_24h_pct`
- Cadence: every **15 minutes** via EventBridge scheduled rule
- Why crypto? Prices change continuously, each sample adds signal, and the data supports a range of interesting derived metrics (trends, highs/lows, volatility windows) without requiring authentication or paid API tiers.

---

## Architecture

### Part 1 — Ingestion Pipeline

```
EventBridge Rule (rate(15 min))
        │
        ▼
Lambda (arnavwx-ingestion)
        │
        ├──► GET https://api.coingecko.com/api/v3/simple/price
        │          ?ids=bitcoin,ethereum,solana&vs_currencies=usd&...
        │
        ├──► DynamoDB PutItem  ──► arnavwx-prices
        │         coin (PK)  │  timestamp (SK)  │  price_usd  │  ...
        │
        └──► QuickChart API ──► S3 PutObject ──► arnavwx-dp3-plots/latest-btc.png
```

| Component | AWS Service | Details |
|-----------|-------------|---------|
| Schedule  | EventBridge | `rate(15 minutes)` |
| Ingest function | Lambda (Python 3.11) | Chalice `@app.schedule` |
| Persistent store | DynamoDB | PK=`coin`, SK=`timestamp` (ISO 8601), PAY_PER_REQUEST |
| Plot storage | S3 | Public-read bucket, fixed key `latest-btc.png` |

**Plot strategy:** "render on write" — the ingestion Lambda regenerates the chart by POSTing a Chart.js config to the public [QuickChart API](https://quickchart.io) and uploads the returned PNG to S3 after every collection cycle. The API `/plot` resource returns the stable S3 URL without any compute at read time.

The chart is a **3-panel figure** with one subplot per coin stacked vertically — **BTC, ETH, and SOL raw USD prices over the last 48 hours**, each on its own y-axis so the very different absolute price scales (≈$60k vs ≈$2k vs ≈$80) are all readable. Each line uses the coin's brand color with dot markers on every data point: BTC orange (`#F7931A`), ETH blue (`#627EEA`), SOL purple (`#9945FF`). Each subplot's title shows the coin and its latest price. Only the bottom panel renders the time axis labels — the panels share the x-axis. Timestamps are intersected across the three coins so all panels line up on the same x positions.

QuickChart only renders one Chart.js chart per request, so the Lambda fires three requests (one per coin) and stitches the resulting PNGs vertically with [pypng](https://pypi.org/project/pypng/), a pure-Python PNG library — no compiled deps to bundle.

### Part 2 — Integration API

```
Discord bot  /  curl
      │
      ▼
API Gateway (arnavwx-api)
      │
      ├── GET /          → { about, resources }
      ├── GET /current   → latest BTC / ETH / SOL prices
      ├── GET /trend     → 24h price movement for BTC
      └── GET /plot      → S3 URL of latest BTC/ETH/SOL chart
            │
            ▼
      Lambda (arnavwx-api) ──► DynamoDB Query ──► arnavwx-prices
```

---

## Repository Layout

```
ds5220-dp3/
├── README.md
├── setup.sh                  # one-shot infra + deploy script
│
├── ingestion/                # Part 1: scheduled data collection
│   ├── app.py
│   ├── requirements.txt
│   └── .chalice/
│       ├── config.json
│       └── policy-dev.json
│
└── api/                      # Part 2: API Gateway + Lambda
    ├── app.py
    ├── requirements.txt
    └── .chalice/
        ├── config.json
        └── policy-dev.json
```

---

## Deployment

### Prerequisites

- AWS CLI configured (`aws configure`) with a user that has Lambda, DynamoDB, S3, IAM, and API Gateway permissions
- Python 3.11+
- `pip install chalice`

### Deploy everything

```bash
chmod +x setup.sh
./setup.sh
```

The script will:
1. Create the `arnavwx-prices` DynamoDB table
2. Create the `arnavwx-dp3-plots` S3 bucket with public-read access
3. Deploy the ingestion Chalice app (EventBridge + Lambda)
4. Deploy the API Chalice app (API Gateway + Lambda)
5. Print the API URL and the Discord registration command

### Manual deploy (if preferred)

```bash
# Infrastructure
aws dynamodb create-table \
  --table-name arnavwx-prices \
  --attribute-definitions AttributeName=coin,AttributeType=S AttributeName=timestamp,AttributeType=S \
  --key-schema AttributeName=coin,KeyType=HASH AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST --region us-east-1

aws s3api create-bucket --bucket arnavwx-dp3-plots --region us-east-1
# (then disable Block Public Access and apply bucket policy — see setup.sh)

# Ingestion
cd ingestion && pip install -r requirements.txt && chalice deploy --stage dev && cd ..

# API
cd api && chalice deploy --stage dev && cd ..
```

---

## API Resources

| Resource | Method | Response |
|----------|--------|----------|
| `/` | GET | `{ "about": "...", "resources": ["current", "trend", "plot"] }` |
| `/current` | GET | Latest BTC / ETH / SOL prices as a formatted string |
| `/trend` | GET | BTC 24h % change, dollar delta, range (high/low) |
| `/plot` | GET | S3 URL of the BTC/ETH/SOL 3-panel price chart (last 48 hours) |

### Example responses

```json
// GET /
{
  "about": "Tracks Bitcoin, Ethereum, and Solana prices every 15 minutes using the CoinGecko API. Stores a full time series in DynamoDB and publishes a live price-history chart to S3.",
  "resources": ["current", "trend", "plot"]
}

// GET /current
{ "response": "BTC $62,450.00 | ETH $3,218.75 | SOL $142.30  (as of 2025-04-15T14:30:00Z UTC)" }

// GET /trend
{ "response": "BTC over ~6.0h (24 samples): ↑ +1.83% (+$1,124.50). Range: $61,200.00 – $62,600.00." }

// GET /plot
{ "response": "https://arnavwx-dp3-plots.s3.us-east-1.amazonaws.com/latest-btc.png" }
```

---

## Discord Registration

Once deployed, register in the `#dp3` channel:

```
/register arnavwx <your-discord-username> <api-gateway-url>
```

Then test:

```
/project arnavwx
/project arnavwx current
/project arnavwx trend
/project arnavwx plot
```

---

## DynamoDB Schema

**Table:** `arnavwx-prices`

| Attribute | Type | Role | Example |
|-----------|------|------|---------|
| `coin` | String | Partition key | `"bitcoin"` |
| `timestamp` | String | Sort key (ISO 8601) | `"2025-04-15T14:30:00Z"` |
| `price_usd` | Number | BTC/USD price | `62450.12` |
| `market_cap` | Number | Market cap in USD | `1230000000000` |
| `volume_24h` | Number | 24h trading volume | `28500000000` |
| `price_change_24h_pct` | Number | % change from 24h ago | `1.83` |

ISO 8601 timestamps sort lexicographically, so DynamoDB `Query` with `KeyConditionExpression … .gte(start)` correctly returns time-range slices without a GSI.

---

## Stretch Goals Considered

- **Additional coins** — ETH and SOL are tracked alongside BTC; adding resources for them is a one-liner.
- **Parameterized plot window** — `/plot/{window}` (24h, 7d, 30d) could be added with `@app.route('/plot/{window}')`.
- **Anomaly detection** — a `/alerts` resource flagging prices beyond N standard deviations is a natural extension of the collected history.
- **Static frontend** — an `index.html` in S3 that polls `/current` and renders the `/plot` image in-browser.
