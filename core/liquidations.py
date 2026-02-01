"""
Coinglass Liquidation Data Integration

Fetches real liquidation data from Coinglass API:
- Real-time liquidations across exchanges
- Aggregated long/short liquidations
- Historical liquidation data for backtesting

Coinglass is the industry standard for liquidation data.

API Documentation: https://coinglass.com/api
Free tier: 10 requests/minute, limited endpoints
Pro tier: Higher limits, more endpoints
"""

import asyncio
import httpx
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable
from dataclasses import dataclass
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class LiquidationData:
    """Liquidation data for a symbol"""
    symbol: str
    timestamp: datetime
    
    # Totals (1 hour)
    total_1h_usd: float = 0
    long_1h_usd: float = 0
    short_1h_usd: float = 0
    
    # Totals (24 hour)
    total_24h_usd: float = 0
    long_24h_usd: float = 0
    short_24h_usd: float = 0
    
    # Recent spike detection
    liquidation_rate_per_min: float = 0
    is_spike: bool = False
    
    # Exchange breakdown
    by_exchange: dict = None


class CoinglassClient:
    """
    Client for Coinglass API v4

    Endpoints used:
    - /api/futures/liquidation/coin-list - Current liquidation data by coin
    - /api/futures/liquidation/history - Historical liquidations
    """

    BASE_URL = "https://open-api-v4.coinglass.com"

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Coinglass API key (required for v4 API)
        """
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

        # Rate limiting (v4 has higher limits)
        self._last_request_time: float = 0
        self._min_interval = 1.0  # Adjust based on your plan

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create httpx client"""
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.api_key:
                headers['CG-API-KEY'] = self.api_key  # v4 uses CG-API-KEY header
            self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    async def close(self):
        """Close the client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _rate_limit(self):
        """Enforce rate limiting"""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time

        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)

        self._last_request_time = asyncio.get_event_loop().time()

    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make an API request"""
        await self._rate_limit()

        try:
            client = await self._get_client()
            url = f"{self.BASE_URL}{endpoint}"

            response = await client.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                # v4 API uses code '0' for success
                if str(data.get('code')) == '0':
                    return data.get('data')
                else:
                    logger.warning(f"Coinglass API error: {data.get('msg', 'Unknown error')}")
                    return None
            elif response.status_code == 429:
                logger.warning("Coinglass rate limited")
                await asyncio.sleep(60)
                return None
            else:
                logger.warning(f"Coinglass request failed: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Coinglass request error: {e}")
            return None
    
    async def get_liquidation_aggregated(self, symbol: str = "BTC") -> Optional[dict]:
        """
        Get aggregated liquidation data for a symbol

        Returns 24h liquidation totals
        """
        # v4: Get from coin-list and filter by symbol
        data = await self._request(
            "/api/futures/liquidation/coin-list",
            params={"ex": "Binance"}
        )
        if data:
            for item in data:
                if item.get('symbol') == symbol:
                    return item
        return None

    async def get_liquidation_history(
        self,
        symbol: str = "BTC",
        time_type: str = "h1",  # h1, h4, h12, h24
    ) -> Optional[list]:
        """
        Get historical liquidation data

        Args:
            symbol: Trading symbol (BTC, ETH, etc.)
            time_type: Time aggregation (h1=1hour, h4=4hour, etc.)
        """
        data = await self._request(
            "/api/futures/liquidation/history",
            params={
                "symbol": symbol,
                "interval": time_type,
            }
        )
        return data

    async def get_liquidation_by_exchange(self, symbol: str = "BTC") -> Optional[dict]:
        """
        Get liquidation data broken down by exchange
        """
        data = await self._request(
            "/api/futures/liquidation/exchange-list",
            params={"symbol": symbol}
        )
        return data

    async def get_all_liquidations(self) -> Optional[list]:
        """
        Get liquidation data for all major symbols from Binance
        """
        data = await self._request(
            "/api/futures/liquidation/coin-list",
            params={"ex": "Binance"}
        )
        return data


class LiquidationCollector:
    """
    Collects and processes liquidation data from Coinglass
    
    Features:
    - Periodic polling of liquidation data
    - Spike detection (unusual liquidation activity)
    - Historical tracking for CLRI calculation
    """
    
    # Tracked symbols
    DEFAULT_SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'AVAX', 'LINK', 'ARB']
    
    # Spike detection thresholds
    SPIKE_THRESHOLD_1H = 50_000_000  # $50M in 1 hour = spike
    SPIKE_RATE_THRESHOLD = 1_000_000  # $1M per minute = spike
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        symbols: list[str] = None,
        on_data_callback: Optional[Callable[[LiquidationData], None]] = None,
        on_spike_callback: Optional[Callable[[LiquidationData], None]] = None,
        polling_interval: int = 60,
    ):
        self.client = CoinglassClient(api_key)
        self.symbols = symbols or self.DEFAULT_SYMBOLS
        self.on_data_callback = on_data_callback
        self.on_spike_callback = on_spike_callback
        self.polling_interval = polling_interval
        
        # Data cache
        self._data_cache: dict[str, LiquidationData] = {}
        
        # Historical for rate calculation
        self._history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        
        # Running state
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    async def start(self):
        """Start liquidation data collection"""
        logger.info("Starting liquidation collector...")
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
    
    async def stop(self):
        """Stop collection"""
        logger.info("Stopping liquidation collector...")
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        await self.client.close()
    
    async def _poll_loop(self):
        """Main polling loop"""
        while self._running:
            try:
                # Fetch all liquidations
                all_data = await self.client.get_all_liquidations()
                
                if all_data:
                    await self._process_liquidation_data(all_data)
                
                await asyncio.sleep(self.polling_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Liquidation poll error: {e}")
                await asyncio.sleep(5)
    
    async def _process_liquidation_data(self, data: list):
        """Process liquidation data from API"""
        now = datetime.now(timezone.utc)

        for item in data:
            symbol = item.get('symbol', '').upper()

            if symbol not in self.symbols:
                continue

            # Extract values (v4 API field names)
            total_1h = float(item.get('liquidation_usd_1h', 0) or 0)
            long_1h = float(item.get('long_liquidation_usd_1h', 0) or 0)
            short_1h = float(item.get('short_liquidation_usd_1h', 0) or 0)

            total_24h = float(item.get('liquidation_usd_24h', 0) or 0)
            long_24h = float(item.get('long_liquidation_usd_24h', 0) or 0)
            short_24h = float(item.get('short_liquidation_usd_24h', 0) or 0)
            
            # Calculate rate
            self._history[symbol].append((now, total_1h))
            
            # Keep only last 10 readings for rate calculation
            if len(self._history[symbol]) > 10:
                self._history[symbol] = self._history[symbol][-10:]
            
            # Calculate rate (change per minute)
            rate = self._calculate_rate(symbol)
            
            # Detect spike
            is_spike = total_1h >= self.SPIKE_THRESHOLD_1H or rate >= self.SPIKE_RATE_THRESHOLD
            
            liq_data = LiquidationData(
                symbol=symbol,
                timestamp=now,
                total_1h_usd=total_1h,
                long_1h_usd=long_1h,
                short_1h_usd=short_1h,
                total_24h_usd=total_24h,
                long_24h_usd=long_24h,
                short_24h_usd=short_24h,
                liquidation_rate_per_min=rate,
                is_spike=is_spike,
            )
            
            self._data_cache[symbol] = liq_data
            
            # Callbacks
            if self.on_data_callback:
                self.on_data_callback(liq_data)
            
            if is_spike and self.on_spike_callback:
                await self.on_spike_callback(liq_data)
    
    def _calculate_rate(self, symbol: str) -> float:
        """Calculate liquidation rate ($ per minute)"""
        history = self._history.get(symbol, [])
        
        if len(history) < 2:
            return 0
        
        oldest = history[0]
        newest = history[-1]
        
        time_diff_minutes = (newest[0] - oldest[0]).total_seconds() / 60
        value_diff = newest[1] - oldest[1]
        
        if time_diff_minutes <= 0:
            return 0
        
        # Rate of change per minute
        return abs(value_diff) / time_diff_minutes
    
    def get_liquidation_data(self, symbol: str) -> Optional[LiquidationData]:
        """Get current liquidation data for a symbol"""
        return self._data_cache.get(symbol.upper())
    
    def get_all_liquidation_data(self) -> dict[str, LiquidationData]:
        """Get all cached liquidation data"""
        return self._data_cache.copy()


class LiquidationAggregator:
    """
    Aggregates liquidation data for CLRI calculation
    
    Provides:
    - Normalized liquidation scores
    - Long/short imbalance detection
    - Cascade risk indicators
    """
    
    def __init__(self):
        # Historical values for percentile calculation
        self._history_1h: dict[str, list[float]] = defaultdict(list)
        self._history_window = 1000
    
    def process(self, liq_data: LiquidationData) -> dict:
        """
        Process liquidation data for CLRI
        
        Returns dict with normalized values for CLRI calculation
        """
        symbol = liq_data.symbol
        total_1h = liq_data.total_1h_usd
        
        # Add to history
        self._history_1h[symbol].append(total_1h)
        if len(self._history_1h[symbol]) > self._history_window:
            self._history_1h[symbol] = self._history_1h[symbol][-self._history_window:]
        
        # Calculate percentile
        history = self._history_1h[symbol]
        percentile = sum(1 for x in history if x < total_1h) / len(history) * 100 if history else 50
        
        # Long/short imbalance
        if liq_data.long_1h_usd + liq_data.short_1h_usd > 0:
            long_pct = liq_data.long_1h_usd / (liq_data.long_1h_usd + liq_data.short_1h_usd)
        else:
            long_pct = 0.5
        
        # Direction signal (-1 to 1)
        # More long liquidations = price dropping = short direction signal
        direction_signal = -1.0 * (long_pct - 0.5) * 2
        
        return {
            'symbol': symbol,
            'liquidations_1h_usd': total_1h,
            'long_liquidations_1h': liq_data.long_1h_usd,
            'short_liquidations_1h': liq_data.short_1h_usd,
            'liquidation_percentile': percentile,
            'direction_signal': direction_signal,
            'is_spike': liq_data.is_spike,
            'rate_per_min': liq_data.liquidation_rate_per_min,
        }


async def test_coinglass():
    """Test Coinglass integration"""
    import os
    from dotenv import load_dotenv

    # Load .env file from parent directory
    load_dotenv()
    api_key = os.getenv('COINGLASS_API_KEY')
    print(f"API Key loaded: {bool(api_key)}")

    client = CoinglassClient(api_key)

    try:
        # Test all liquidations
        print("Fetching all liquidations...")
        data = await client.get_all_liquidations()

        if data:
            print(f"Got {len(data)} symbols")
            for item in data[:5]:
                symbol = item.get('symbol', 'N/A')
                h1 = item.get('liquidation_usd_1h', 0) or 0
                h24 = item.get('liquidation_usd_24h', 0) or 0
                print(f"  {symbol}: 1h=${h1:,.0f}, 24h=${h24:,.0f}")
        else:
            print("No data returned")

        # Test BTC specific
        print("\nFetching BTC aggregated...")
        btc_data = await client.get_liquidation_aggregated('BTC')

        if btc_data:
            h1 = btc_data.get('liquidation_usd_1h', 0) or 0
            print(f"BTC 1h liquidations: ${h1:,.0f}")

    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(test_coinglass())
