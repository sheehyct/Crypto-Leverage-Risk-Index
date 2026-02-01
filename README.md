# CLRI - Crypto Leverage Risk Index

A real-time monitoring system that tracks crypto market leverage to predict volatile price moves. Aggregates funding rates, open interest, liquidations, and order book data across multiple exchanges to produce a composite risk score (0-100).

## Overview

CLRI monitors leverage buildup across major crypto perpetual futures markets. When traders pile into leveraged positions, the market becomes fragile - a small price move can trigger cascading liquidations, causing violent price swings in either direction.

**Key Features:**

- Multi-exchange aggregation (Binance, Bybit, OKX)
- Real liquidation data via Coinglass API
- Dynamic OI-weighted market index
- Two-layer intelligent alert system
- Regime detection and dynamic component weighting
- Composite stress scoring

## CLRI Score Interpretation

### Risk Levels

| Level | Score | Market Condition |
|-------|-------|------------------|
| LOW | 0-24 | Normal conditions, balanced positioning |
| MODERATE | 25-44 | Slightly elevated leverage |
| ELEVATED | 45-64 | High leverage, potential for volatility |
| HIGH | 65-79 | Very high leverage, expect significant moves |
| EXTREME | 80-100 | Maximum risk, violent move imminent |

### Direction Indicators

| Direction | Meaning |
|-----------|---------|
| LONG CROWDED | Excessive long positions (positive funding, downside liquidation risk) |
| SHORT CROWDED | Excessive short positions (negative funding, upside squeeze risk) |
| NEUTRAL | Balanced positioning |

## Components

CLRI combines five normalized components using z-score calculations:

| Component | Base Weight | Data Source |
|-----------|-------------|-------------|
| Funding Rate | 25% | Exchange APIs (CCXT Pro) |
| Open Interest | 25% | Exchange APIs (CCXT Pro) |
| Volume Ratio | 15% | Perp vs Spot volume |
| Liquidations | 20% | Coinglass API |
| Order Book Imbalance | 15% | Exchange WebSocket |

### Dynamic Weighting

Component weights adjust automatically based on detected market regime:

| Regime | Trigger | Effect |
|--------|---------|--------|
| NORMAL | All metrics stable | Base weights apply |
| LIQUIDATION_CASCADE | Liquidation z-score >= 2.0 | Liquidation weight increases to 35% |
| FUNDING_STRESS | Funding z-score >= 2.0 | Funding weight increases to 35% |
| OI_SPIKE | OI z-score >= 2.5 | OI weight increases to 35% |

## Alert System

CLRI uses a two-layer alert system designed to minimize noise while capturing high-value signals.

### Layer 1: Market CLRI Alerts

A single market-wide index using dynamic OI weighting. Alerts fire when:

- Composite crosses threshold levels (65, 80, 90)
- Rapid 15+ point movement detected

Example alert:
```
MARKET CLRI: 74 - HIGH RISK
Direction: LONG CROWDED
Top contributors:
  BTC (52% weight): CLRI 78
  ETH (28% weight): CLRI 71
  XRP (8% weight): CLRI 69
```

### Layer 2: Divergence Alerts

Individual symbol alerts only fire when a symbol diverges significantly (25+ points) from the market average. This catches unusual single-asset leverage buildup.

Example alert:
```
DIVERGENCE: SOL CLRI 78 vs Market 35 (+43)
SOL showing independent leverage buildup
Direction: LONG CROWDED
```

### Additional Alert Types

- **Liquidation Events**: Large dollar liquidations detected
- **Price Validation**: Post-hoc validation when 3%+ price moves occur

## Architecture

```
+------------------+     +------------------+     +------------------+
|   Binance USD-M  |     |      Bybit       |     |       OKX        |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         +------------------------+------------------------+
                                  |
                    +-------------v--------------+
                    |  Multi-Exchange Collector  |
                    |     (CCXT Pro WebSocket)   |
                    +-------------+--------------+
                                  |
         +------------------------+------------------------+
         |                        |                        |
+--------v---------+   +----------v----------+   +---------v---------+
|  CLRI Calculator |   | Liquidation Collector|   |   OI Aggregator   |
|  (per symbol)    |   |    (Coinglass API)   |   | (dynamic weights) |
+--------+---------+   +----------+-----------+   +---------+---------+
         |                        |                        |
         +------------------------+------------------------+
                                  |
                    +-------------v--------------+
                    |     Aggregate CLRI         |
                    |  (Market Index + Alerts)   |
                    +-------------+--------------+
                                  |
              +-------------------+-------------------+
              |                   |                   |
    +---------v--------+  +-------v-------+  +-------v-------+
    |    PostgreSQL    |  |    Discord    |  |   Dashboard   |
    |   (persistence)  |  |   (alerts)    |  |  (optional)   |
    +------------------+  +---------------+  +---------------+
```

## Deployment

### Prerequisites

- Python 3.11+
- PostgreSQL 15+
- Discord webhook (for alerts)
- Coinglass API key (for liquidation data)

### Local Development

```bash
# Clone repository
git clone https://github.com/yourusername/clri.git
cd clri

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings

# Start PostgreSQL (Docker)
docker run -d -p 5432:5432 \
  -e POSTGRES_DB=clri \
  -e POSTGRES_PASSWORD=dev \
  postgres:15

# Run worker
python -m worker.main

# Run dashboard (optional, separate terminal)
uvicorn app.main:app --reload
```

### Production Deployment (Docker)

```bash
cd deploy

# Configure environment
cp ../.env.example .env
# Edit .env with production values

# Deploy
docker compose -f docker-compose.prod.yml up -d

# View logs
docker compose -f docker-compose.prod.yml logs -f worker
```

### VPS Requirements

- EU-based server recommended (Binance geo-restrictions)
- 1 CPU, 1GB RAM minimum
- PostgreSQL database

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `DISCORD_WEBHOOK_URL` | Yes | Discord webhook for alerts |
| `COINGLASS_API_KEY` | Yes | Coinglass API key for liquidation data |
| `ENABLED_EXCHANGES` | No | Comma-separated list (default: binanceusdm,bybit,okx) |
| `TRACKED_SYMBOLS` | No | Comma-separated list (default: BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA) |
| `POLLING_INTERVAL` | No | Data collection interval in seconds (default: 60) |
| `SUMMARY_INTERVAL` | No | Market summary interval in seconds (default: 900) |
| `DISCORD_BOT_TOKEN` | No | Discord bot token for message cleanup |
| `DISCORD_CHANNEL_ID` | No | Channel ID for automatic cleanup |

### Discord Setup

**Webhook (Required for alerts):**
1. Server Settings > Integrations > Webhooks
2. Create New Webhook
3. Copy URL to `DISCORD_WEBHOOK_URL`

**Bot (Optional, for message cleanup):**
1. Create application at https://discord.com/developers/applications
2. Create bot and copy token to `DISCORD_BOT_TOKEN`
3. Enable "Message Content Intent"
4. Invite bot with "Manage Messages" and "Read Message History" permissions
5. Copy channel ID to `DISCORD_CHANNEL_ID`

## API Reference

### Current Readings

```
GET /api/clri/current
```

Returns current CLRI readings for all tracked symbols.

### Market Summary

```
GET /api/clri/market
```

Returns market-wide CLRI index with OI weights and top contributors.

### Historical Data

```
GET /api/clri/history/{symbol}?hours=24
```

Returns historical readings for a specific symbol.

### Recent Alerts

```
GET /api/alerts?limit=50
```

Returns recent alert history.

## High-Value Signals

Beyond the composite score, CLRI calculates additional signals for advanced analysis:

| Signal | Description |
|--------|-------------|
| `basis_pct` | Futures premium/discount vs spot |
| `liquidation_oi_ratio` | Liquidations relative to open interest |
| `funding_oi_stress` | Funding rate stress adjusted for OI |
| `oi_price_divergence` | OI movement vs price movement |
| `composite_stress` | Combined stress indicator (0-100) |

## Project Structure

```
clri/
├── app/                    # FastAPI dashboard
│   └── main.py
├── core/                   # Core business logic
│   ├── clri.py            # CLRI calculation engine
│   ├── database.py        # SQLAlchemy models
│   ├── discord_alerter.py # Discord webhook integration
│   ├── discord_cleanup.py # Discord bot for cleanup
│   ├── liquidations.py    # Coinglass API client
│   └── multi_exchange.py  # Multi-exchange collector
├── worker/                 # Background worker
│   └── main.py
├── deploy/                 # Deployment configs
│   └── docker-compose.prod.yml
├── scripts/                # Utility scripts
│   └── purge_discord.py   # One-time message cleanup
├── templates/              # Dashboard templates
├── requirements.txt
└── README.md
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with clear commit messages
4. Submit a pull request

## License

MIT License - see LICENSE file for details.
