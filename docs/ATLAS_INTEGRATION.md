# CLRI → ATLAS Integration Guide

This document describes how to integrate the Crypto Leverage Risk Index (CLRI) into your ATLAS algorithmic trading system.

## Overview

CLRI provides real-time crypto leverage risk metrics that can enhance ATLAS in several ways:

1. **Regime Indicator** - High CLRI suggests volatile regime, informing position sizing
2. **Cross-Asset Correlation** - Crypto stress often precedes equity volatility
3. **Entry Filter** - Avoid new positions during extreme readings
4. **Volatility Prediction** - CLRI spikes often precede large moves by 30-60 minutes

## Integration Methods

### Method 1: JSON File Export (Simplest)

CLRI can export its state to a JSON file that ATLAS reads periodically.

**Enable in CLRI:**
```bash
# Set environment variable
ATLAS_EXPORT_PATH=/path/to/clri_state.json
```

**ATLAS reads the file:**
```python
import json
from pathlib import Path
from datetime import datetime, timedelta

class CLRIReader:
    """Reads CLRI state from JSON export"""
    
    def __init__(self, export_path: str, max_age_seconds: int = 120):
        self.export_path = Path(export_path)
        self.max_age_seconds = max_age_seconds
        self._cache = None
        self._cache_time = None
    
    def get_state(self) -> dict:
        """Get current CLRI state"""
        if not self.export_path.exists():
            return {'market_clri': {'market_clri': 0, 'risk_level': 'LOW'}}
        
        # Check cache
        if self._cache and self._cache_time:
            age = (datetime.now() - self._cache_time).total_seconds()
            if age < 5:  # Use cache if < 5 seconds old
                return self._cache
        
        try:
            with open(self.export_path) as f:
                data = json.load(f)
            
            # Check freshness
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(ts.tzinfo) - ts).total_seconds()
            
            if age > self.max_age_seconds:
                # Data is stale
                return {'stale': True, 'age_seconds': age, **data}
            
            self._cache = data
            self._cache_time = datetime.now()
            return data
            
        except Exception as e:
            return {'error': str(e)}
    
    def get_market_clri(self) -> int:
        """Get market-wide CLRI score (0-100)"""
        state = self.get_state()
        return state.get('market_clri', {}).get('market_clri', 0)
    
    def get_symbol_clri(self, symbol: str) -> dict:
        """Get CLRI for a specific symbol"""
        state = self.get_state()
        return state.get('symbols', {}).get(symbol.upper(), {})
    
    def is_elevated(self) -> bool:
        """Quick check if market CLRI is elevated (>= 65)"""
        return self.get_market_clri() >= 65
    
    def is_extreme(self) -> bool:
        """Quick check if market CLRI is extreme (>= 85)"""
        return self.get_market_clri() >= 85
```

### Method 2: Direct API Integration

ATLAS can poll CLRI's REST API directly.

```python
import httpx
from dataclasses import dataclass
from typing import Optional

@dataclass
class CLRIState:
    market_clri: int
    risk_level: str
    market_direction: str
    btc_clri: int
    eth_clri: int
    elevated_count: int

class CLRIClient:
    """Direct API client for CLRI dashboard"""
    
    def __init__(
        self, 
        base_url: str,  # e.g., "https://your-clri.railway.app"
        session_token: str,  # Get from login
    ):
        self.base_url = base_url.rstrip('/')
        self.session_token = session_token
        self._client = httpx.AsyncClient(
            cookies={'session_token': session_token},
            timeout=10.0,
        )
    
    async def get_market_summary(self) -> CLRIState:
        """Get market-wide CLRI summary"""
        response = await self._client.get(f"{self.base_url}/api/clri/market")
        response.raise_for_status()
        data = response.json()
        
        return CLRIState(
            market_clri=data.get('market_clri', 0),
            risk_level=data.get('risk_level', 'LOW'),
            market_direction=data.get('market_direction', 'NEUTRAL'),
            btc_clri=data.get('individual_readings', {}).get('BTC', 0),
            eth_clri=data.get('individual_readings', {}).get('ETH', 0),
            elevated_count=data.get('symbols_elevated', 0),
        )
    
    async def get_current_readings(self) -> dict:
        """Get all current CLRI readings"""
        response = await self._client.get(f"{self.base_url}/api/clri/current")
        response.raise_for_status()
        return response.json()
    
    async def close(self):
        await self._client.aclose()
```

### Method 3: Shared Database (Advanced)

For tightest integration, ATLAS can read directly from CLRI's PostgreSQL database.

```python
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import pandas as pd

class CLRIDatabase:
    """Direct database access for ATLAS"""
    
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url)
        self.Session = sessionmaker(bind=self.engine)
    
    def get_latest_readings(self, limit: int = 100) -> pd.DataFrame:
        """Get latest CLRI readings as DataFrame"""
        query = """
            SELECT 
                timestamp,
                symbol,
                clri_score,
                risk_level,
                direction,
                funding_rate,
                open_interest,
                liquidations_1h_usd,
                price
            FROM clri_readings
            ORDER BY timestamp DESC
            LIMIT :limit
        """
        
        with self.engine.connect() as conn:
            df = pd.read_sql(text(query), conn, params={'limit': limit})
        
        return df
    
    def get_symbol_history(
        self, 
        symbol: str, 
        hours: int = 24,
    ) -> pd.DataFrame:
        """Get historical CLRI for a symbol"""
        query = """
            SELECT 
                timestamp,
                clri_score,
                funding_rate,
                open_interest,
                price
            FROM clri_readings
            WHERE symbol = :symbol
              AND timestamp > NOW() - INTERVAL ':hours hours'
            ORDER BY timestamp
        """
        
        with self.engine.connect() as conn:
            df = pd.read_sql(
                text(query), 
                conn, 
                params={'symbol': symbol, 'hours': hours}
            )
        
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)
        return df
```

## ATLAS Integration Patterns

### Pattern 1: Regime Detection Enhancement

Add CLRI as an input to your existing regime detection:

```python
class ATLASRegimeDetector:
    """Enhanced regime detection with CLRI"""
    
    def __init__(self, clri_reader: CLRIReader):
        self.clri = clri_reader
        self.jump_model = ...  # Your existing jump model
        self.vix_threshold = ...
    
    def detect_regime(self, market_data: pd.DataFrame) -> str:
        """Detect current market regime"""
        
        # Your existing regime detection
        base_regime = self.jump_model.predict(market_data)
        
        # Enhance with CLRI
        clri_state = self.clri.get_state()
        market_clri = clri_state.get('market_clri', {}).get('market_clri', 0)
        
        # CLRI modifies regime assessment
        if market_clri >= 85:
            # Override to VOLATILE regardless of other signals
            return 'VOLATILE_EXTREME'
        
        elif market_clri >= 65:
            # Elevate regime volatility assessment
            if base_regime == 'CALM':
                return 'ELEVATED'  # Upgrade calm to elevated
            elif base_regime == 'TRENDING':
                return 'VOLATILE'  # Upgrade trending to volatile
        
        elif market_clri >= 45:
            # Moderate adjustment
            if base_regime == 'CALM':
                return 'WATCHFUL'
        
        return base_regime
```

### Pattern 2: Position Sizing Adjustment

Scale position sizes based on CLRI:

```python
class CLRIPositionSizer:
    """Adjust position sizes based on crypto leverage risk"""
    
    # CLRI to position scale mapping
    SCALE_MAP = {
        (0, 25): 1.0,     # Full size
        (25, 45): 0.85,   # Slightly reduced
        (45, 65): 0.65,   # Moderately reduced
        (65, 80): 0.40,   # Significantly reduced
        (80, 100): 0.20,  # Minimal size
    }
    
    def __init__(self, clri_reader: CLRIReader):
        self.clri = clri_reader
    
    def get_scale_factor(self) -> float:
        """Get position scale factor based on CLRI"""
        market_clri = self.clri.get_market_clri()
        
        for (low, high), scale in self.SCALE_MAP.items():
            if low <= market_clri < high:
                return scale
        
        return 0.20  # Default to minimal if extreme
    
    def adjust_size(self, base_size: float) -> float:
        """Adjust position size"""
        return base_size * self.get_scale_factor()
```

### Pattern 3: Entry Filter

Filter entries during extreme crypto leverage:

```python
class CLRIEntryFilter:
    """Filter trade entries based on CLRI"""
    
    def __init__(
        self, 
        clri_reader: CLRIReader,
        block_threshold: int = 80,
        warn_threshold: int = 65,
    ):
        self.clri = clri_reader
        self.block_threshold = block_threshold
        self.warn_threshold = warn_threshold
    
    def should_enter(self, signal: dict) -> tuple[bool, str]:
        """
        Check if entry should proceed
        
        Returns: (should_enter, reason)
        """
        market_clri = self.clri.get_market_clri()
        
        if market_clri >= self.block_threshold:
            return False, f"BLOCKED: CLRI={market_clri} >= {self.block_threshold}"
        
        if market_clri >= self.warn_threshold:
            return True, f"WARNING: CLRI={market_clri} elevated, proceed with caution"
        
        return True, "OK"
    
    def should_exit_early(self, position: dict) -> tuple[bool, str]:
        """Check if position should exit early due to CLRI spike"""
        market_clri = self.clri.get_market_clri()
        
        # Exit if CLRI spikes above extreme
        if market_clri >= 90:
            return True, f"CLRI EXTREME: {market_clri}, early exit recommended"
        
        return False, "OK"
```

### Pattern 4: BTC→Equity Correlation Signal

Use BTC CLRI as leading indicator for equity volatility:

```python
class CryptoEquityCorrelation:
    """
    Monitor crypto leverage as equity vol predictor
    
    Theory: Crypto liquidation cascades often precede 
    equity volatility by 30-60 minutes due to:
    - Institutional deleveraging across asset classes
    - Risk-off sentiment propagation
    - Margin call contagion
    """
    
    def __init__(self, clri_reader: CLRIReader):
        self.clri = clri_reader
        self.btc_clri_history = []
        self.max_history = 60  # 60 minutes
    
    def update(self):
        """Call every minute"""
        state = self.clri.get_state()
        btc_clri = state.get('symbols', {}).get('BTC', {}).get('clri_score', 0)
        
        self.btc_clri_history.append({
            'timestamp': datetime.now(),
            'clri': btc_clri,
        })
        
        # Trim history
        if len(self.btc_clri_history) > self.max_history:
            self.btc_clri_history = self.btc_clri_history[-self.max_history:]
    
    def get_equity_vol_warning(self) -> dict:
        """
        Check if BTC CLRI suggests upcoming equity volatility
        """
        if len(self.btc_clri_history) < 5:
            return {'warning': False}
        
        recent = self.btc_clri_history[-5:]
        current = recent[-1]['clri']
        avg = sum(r['clri'] for r in recent) / len(recent)
        
        # Rapid CLRI increase in last 5 minutes
        rate_of_change = current - recent[0]['clri']
        
        if current >= 75 and rate_of_change > 15:
            return {
                'warning': True,
                'level': 'HIGH',
                'message': f'BTC CLRI spiked {rate_of_change} pts in 5min to {current}',
                'equity_vol_eta_minutes': 30,  # Expected equity vol in ~30min
            }
        
        if current >= 65:
            return {
                'warning': True,
                'level': 'ELEVATED',
                'message': f'BTC CLRI elevated at {current}',
                'equity_vol_eta_minutes': 60,
            }
        
        return {'warning': False}
```

## VectorBT Pro Integration

For backtesting CLRI signals with your VBT setup:

```python
import vectorbtpro as vbt
import pandas as pd

# Load CLRI historical data
clri_df = pd.read_sql(
    """
    SELECT timestamp, symbol, clri_score, direction
    FROM clri_readings
    WHERE symbol = 'BTC'
    ORDER BY timestamp
    """,
    engine
)
clri_df.set_index('timestamp', inplace=True)

# Create CLRI-based signals
def clri_signal_func(clri_score, direction):
    """
    Generate signals based on CLRI
    
    Strategy: Fade extreme leverage
    - SHORT when CLRI > 80 and LONG_CROWDED
    - LONG when CLRI > 80 and SHORT_CROWDED
    """
    entries = np.zeros(len(clri_score), dtype=bool)
    exits = np.zeros(len(clri_score), dtype=bool)
    
    for i in range(1, len(clri_score)):
        if clri_score[i] >= 80:
            if direction[i] == 'LONG_CROWDED':
                entries[i] = True  # Go short
            elif direction[i] == 'SHORT_CROWDED':
                entries[i] = True  # Go long
        
        # Exit when CLRI normalizes
        if clri_score[i] < 50 and clri_score[i-1] >= 50:
            exits[i] = True
    
    return entries, exits

# Backtest
entries, exits = clri_signal_func(
    clri_df['clri_score'].values,
    clri_df['direction'].values
)

pf = vbt.Portfolio.from_signals(
    price_df['close'],
    entries=entries,
    exits=exits,
    freq='1h',
)

print(pf.stats())
```

## Environment Variables for ATLAS Integration

```bash
# In CLRI deployment
ATLAS_EXPORT_PATH=/shared/clri_state.json

# In ATLAS deployment
CLRI_STATE_PATH=/shared/clri_state.json
# OR
CLRI_API_URL=https://your-clri.railway.app
CLRI_SESSION_TOKEN=your-session-token
# OR
CLRI_DATABASE_URL=postgresql://...
```

## Monitoring & Debugging

### Check CLRI State

```python
# Quick state check
reader = CLRIReader('/path/to/clri_state.json')
print(f"Market CLRI: {reader.get_market_clri()}")
print(f"Elevated: {reader.is_elevated()}")
print(f"BTC: {reader.get_symbol_clri('BTC')}")
```

### Log CLRI Decisions

```python
import logging

logger = logging.getLogger('atlas.clri')

def log_clri_decision(action: str, clri_state: dict, decision: str):
    """Log CLRI-influenced decisions for analysis"""
    logger.info(
        f"CLRI_DECISION | action={action} | "
        f"market_clri={clri_state.get('market_clri', 0)} | "
        f"btc_clri={clri_state.get('symbols', {}).get('BTC', {}).get('clri_score', 0)} | "
        f"decision={decision}"
    )
```

## Recommended ATLAS Workflow

1. **Every minute**: Update CLRI state from export/API
2. **Before entry signals**: Check CLRI filter
3. **Position sizing**: Apply CLRI scale factor
4. **Regime detection**: Include CLRI in regime model
5. **Active positions**: Monitor for CLRI spikes (early exit trigger)
6. **EOD analysis**: Review CLRI correlation with PnL

## Common Issues

### Stale Data
- Set `max_age_seconds` appropriately (default 120s)
- Alert if data is stale for >5 minutes

### Missing Symbols
- Verify symbol naming matches (BTC vs BTC/USDT)
- Check CLRI is tracking the symbols you need

### False Positives
- CLRI elevation doesn't guarantee a move
- Use as one input among many, not sole signal
- Backtest your specific strategy integration

---

Questions? The CLRI system is designed to be a supplementary signal for ATLAS, not a replacement for your core strategy logic. Use it to add another dimension of risk awareness.
