"""
Multi-Exchange Data Collector

Aggregates data from multiple exchanges for more accurate CLRI:
- Binance USDM (largest volume)
- Bybit (significant OI)
- OKX (institutional flow)
- Bitget (retail sentiment)

Cross-exchange signals:
- Funding divergence between exchanges
- OI concentration shifts
- Volume migration patterns
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import ccxt.pro as ccxtpro

from core.clri import CLRIInput

logger = logging.getLogger(__name__)


@dataclass
class ExchangeConfig:
    """Configuration for an exchange connection"""
    exchange_id: str
    symbols: list[str]
    weight: float = 1.0  # Weight in aggregation (by volume/importance)
    options: dict = field(default_factory=dict)
    enabled: bool = True


# Symbol mappings per exchange (they use different formats)
SYMBOL_MAPPINGS = {
    'binanceusdm': {
        'BTC': 'BTC/USDT:USDT',
        'ETH': 'ETH/USDT:USDT',
        'SOL': 'SOL/USDT:USDT',
        'XRP': 'XRP/USDT:USDT',
        'DOGE': 'DOGE/USDT:USDT',
        'AVAX': 'AVAX/USDT:USDT',
        'LINK': 'LINK/USDT:USDT',
        'ARB': 'ARB/USDT:USDT',
        'OP': 'OP/USDT:USDT',
        'SUI': 'SUI/USDT:USDT',
        'ADA': 'ADA/USDT:USDT',
    },
    'bybit': {
        'BTC': 'BTC/USDT:USDT',
        'ETH': 'ETH/USDT:USDT',
        'SOL': 'SOL/USDT:USDT',
        'XRP': 'XRP/USDT:USDT',
        'DOGE': 'DOGE/USDT:USDT',
        'AVAX': 'AVAX/USDT:USDT',
        'LINK': 'LINK/USDT:USDT',
        'ARB': 'ARB/USDT:USDT',
        'OP': 'OP/USDT:USDT',
        'SUI': 'SUI/USDT:USDT',
        'ADA': 'ADA/USDT:USDT',
    },
    'okx': {
        'BTC': 'BTC/USDT:USDT',
        'ETH': 'ETH/USDT:USDT',
        'SOL': 'SOL/USDT:USDT',
        'XRP': 'XRP/USDT:USDT',
        'DOGE': 'DOGE/USDT:USDT',
        'AVAX': 'AVAX/USDT:USDT',
        'LINK': 'LINK/USDT:USDT',
        'ARB': 'ARB/USDT:USDT',
        'OP': 'OP/USDT:USDT',
        'SUI': 'SUI/USDT:USDT',
        'ADA': 'ADA/USDT:USDT',
    },
    'bitget': {
        'BTC': 'BTC/USDT:USDT',
        'ETH': 'ETH/USDT:USDT',
        'SOL': 'SOL/USDT:USDT',
        'XRP': 'XRP/USDT:USDT',
        'DOGE': 'DOGE/USDT:USDT',
        'AVAX': 'AVAX/USDT:USDT',
        'LINK': 'LINK/USDT:USDT',
        'ADA': 'ADA/USDT:USDT',
    },
}

# Default tracked base symbols
DEFAULT_BASE_SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'AVAX', 'LINK', 'ARB', 'OP', 'SUI']

# Exchange weights (approximate by volume/importance)
EXCHANGE_WEIGHTS = {
    'binanceusdm': 0.45,  # ~45% of perp volume
    'bybit': 0.25,        # ~25% 
    'okx': 0.20,          # ~20%
    'bitget': 0.10,       # ~10%
}


@dataclass
class ExchangeData:
    """Data from a single exchange for a symbol"""
    exchange_id: str
    symbol: str
    timestamp: datetime
    
    funding_rate: float = 0
    predicted_funding: Optional[float] = None
    open_interest: float = 0
    volume_24h: float = 0
    price: float = 0
    
    bid_depth: float = 0
    ask_depth: float = 0
    
    # Exchange-specific
    long_short_ratio: Optional[float] = None


class MultiExchangeCollector:
    """
    Collects and aggregates data from multiple exchanges
    
    Features:
    - Weighted aggregation by exchange volume
    - Cross-exchange divergence detection
    - Fault tolerance (continues if one exchange fails)
    """
    
    def __init__(
        self,
        base_symbols: list[str] = None,
        enabled_exchanges: list[str] = None,
        on_data_callback: Optional[Callable[[CLRIInput], None]] = None,
        on_cross_exchange_signal: Optional[Callable[[dict], None]] = None,
        polling_interval: int = 60,
    ):
        self.base_symbols = base_symbols or DEFAULT_BASE_SYMBOLS
        self.enabled_exchanges = enabled_exchanges or ['binanceusdm', 'bybit', 'okx']
        self.on_data_callback = on_data_callback
        self.on_cross_exchange_signal = on_cross_exchange_signal
        self.polling_interval = polling_interval
        
        # Exchange instances
        self.exchanges: dict[str, ccxtpro.Exchange] = {}
        
        # Per-exchange data cache
        self._exchange_data: dict[str, dict[str, ExchangeData]] = defaultdict(dict)
        
        # Previous values for change detection
        self._previous_oi: dict[str, dict[str, float]] = defaultdict(dict)
        
        # Running state
        self._running = False
        self._tasks: list[asyncio.Task] = []
    
    def _get_exchange_symbol(self, exchange_id: str, base_symbol: str) -> Optional[str]:
        """Get exchange-specific symbol format"""
        mappings = SYMBOL_MAPPINGS.get(exchange_id, {})
        return mappings.get(base_symbol)
    
    async def _init_exchange(self, exchange_id: str) -> Optional[ccxtpro.Exchange]:
        """Initialize an exchange connection"""
        try:
            exchange_class = getattr(ccxtpro, exchange_id)

            options = {
                'enableRateLimit': True,
            }

            # Exchange-specific options
            if exchange_id == 'binanceusdm':
                options['defaultType'] = 'future'
            elif exchange_id == 'bybit':
                options['defaultType'] = 'linear'
            elif exchange_id == 'okx':
                options['defaultType'] = 'swap'
            elif exchange_id == 'bitget':
                options['defaultType'] = 'swap'

            exchange = exchange_class(options)
            await exchange.load_markets()

            logger.info(f"Initialized {exchange_id} with {len(exchange.symbols)} markets")
            return exchange

        except Exception as e:
            logger.error(f"Failed to initialize {exchange_id}: {e}")
            return None
    
    async def start(self):
        """Start data collection from all exchanges"""
        logger.info(f"Starting multi-exchange collector: {self.enabled_exchanges}")
        self._running = True
        
        # Initialize exchanges
        for exchange_id in self.enabled_exchanges:
            exchange = await self._init_exchange(exchange_id)
            if exchange:
                self.exchanges[exchange_id] = exchange
        
        if not self.exchanges:
            logger.error("No exchanges initialized!")
            return
        
        # Start collection tasks per exchange
        for exchange_id, exchange in self.exchanges.items():
            # Polling task for funding/OI
            self._tasks.append(
                asyncio.create_task(
                    self._poll_exchange(exchange_id, exchange)
                )
            )
            
            # Streaming tasks for tickers/orderbooks
            for base_symbol in self.base_symbols:
                symbol = self._get_exchange_symbol(exchange_id, base_symbol)
                if symbol and symbol in exchange.markets:
                    self._tasks.append(
                        asyncio.create_task(
                            self._stream_ticker(exchange_id, exchange, base_symbol, symbol)
                        )
                    )
        
        # Aggregation task
        self._tasks.append(
            asyncio.create_task(self._aggregate_and_emit())
        )
        
        # Cross-exchange analysis task
        self._tasks.append(
            asyncio.create_task(self._analyze_cross_exchange())
        )
        
        logger.info(f"Started {len(self._tasks)} collection tasks")
    
    async def stop(self):
        """Stop all collection tasks"""
        logger.info("Stopping multi-exchange collector...")
        self._running = False
        
        for task in self._tasks:
            task.cancel()
        
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        for exchange in self.exchanges.values():
            await exchange.close()
        
        self._tasks = []
        self.exchanges = {}
        logger.info("Multi-exchange collector stopped")
    
    async def _poll_exchange(self, exchange_id: str, exchange: ccxtpro.Exchange):
        """Poll funding rates and OI from an exchange"""
        while self._running:
            try:
                for base_symbol in self.base_symbols:
                    symbol = self._get_exchange_symbol(exchange_id, base_symbol)
                    if not symbol or symbol not in exchange.markets:
                        continue
                    
                    try:
                        # Fetch funding rate
                        funding = await exchange.fetch_funding_rate(symbol)
                        
                        funding_rate = funding.get('fundingRate', 0) or 0
                        predicted = funding.get('nextFundingRate')
                        
                        # OI often included in funding response
                        oi = funding.get('openInterestValue', 0) or funding.get('openInterest', 0) or 0
                        
                        # If not, fetch separately
                        if oi == 0:
                            try:
                                oi_data = await exchange.fetch_open_interest(symbol)
                                oi = oi_data.get('openInterestValue', 0) or oi_data.get('openInterest', 0) or 0
                            except:
                                pass
                        
                        # Update cache
                        if base_symbol not in self._exchange_data[exchange_id]:
                            self._exchange_data[exchange_id][base_symbol] = ExchangeData(
                                exchange_id=exchange_id,
                                symbol=base_symbol,
                                timestamp=datetime.now(timezone.utc),
                            )
                        
                        data = self._exchange_data[exchange_id][base_symbol]
                        data.funding_rate = funding_rate
                        data.predicted_funding = predicted
                        data.open_interest = oi
                        data.timestamp = datetime.now(timezone.utc)
                        
                    except Exception as e:
                        logger.debug(f"Funding fetch failed for {exchange_id}:{symbol}: {e}")
                    
                    await asyncio.sleep(0.2)  # Rate limit spacing
                
                await asyncio.sleep(self.polling_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling error for {exchange_id}: {e}")
                await asyncio.sleep(5)
    
    async def _stream_ticker(
        self, 
        exchange_id: str, 
        exchange: ccxtpro.Exchange,
        base_symbol: str,
        symbol: str,
    ):
        """Stream ticker data for a symbol"""
        while self._running:
            try:
                ticker = await exchange.watch_ticker(symbol)
                
                if base_symbol not in self._exchange_data[exchange_id]:
                    self._exchange_data[exchange_id][base_symbol] = ExchangeData(
                        exchange_id=exchange_id,
                        symbol=base_symbol,
                        timestamp=datetime.now(timezone.utc),
                    )
                
                data = self._exchange_data[exchange_id][base_symbol]
                data.price = ticker.get('last', 0) or 0
                data.volume_24h = ticker.get('quoteVolume', 0) or 0
                data.timestamp = datetime.now(timezone.utc)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Ticker stream error for {exchange_id}:{symbol}: {e}")
                await asyncio.sleep(1)
    
    async def _aggregate_and_emit(self):
        """Aggregate data across exchanges and emit CLRIInput"""
        while self._running:
            try:
                await asyncio.sleep(5)  # Emit every 5 seconds
                
                for base_symbol in self.base_symbols:
                    aggregated = self._aggregate_symbol(base_symbol)
                    if aggregated and self.on_data_callback:
                        self.on_data_callback(aggregated)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Aggregation error: {e}")
    
    def _aggregate_symbol(self, base_symbol: str) -> Optional[CLRIInput]:
        """Aggregate data for a symbol across all exchanges"""
        exchange_readings = []
        
        for exchange_id in self.enabled_exchanges:
            if base_symbol in self._exchange_data[exchange_id]:
                data = self._exchange_data[exchange_id][base_symbol]
                weight = EXCHANGE_WEIGHTS.get(exchange_id, 0.1)
                exchange_readings.append((data, weight))
        
        if not exchange_readings:
            return None
        
        # Weighted aggregation
        total_weight = sum(w for _, w in exchange_readings)
        
        # Funding rate: volume-weighted average
        weighted_funding = sum(d.funding_rate * w for d, w in exchange_readings) / total_weight
        
        # OI: sum across exchanges
        total_oi = sum(d.open_interest for d, _ in exchange_readings)
        
        # Calculate OI change
        prev_oi = self._previous_oi.get(base_symbol, {}).get('total', total_oi)
        oi_change_pct = ((total_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0
        self._previous_oi[base_symbol] = {'total': total_oi}
        
        # Volume: sum across exchanges
        total_volume = sum(d.volume_24h for d, _ in exchange_readings)
        
        # Price: use highest-weight exchange
        price = max(exchange_readings, key=lambda x: x[1])[0].price
        
        # Estimate spot price from funding rate direction
        # Positive funding = futures trading at premium (contango)
        # Negative funding = futures trading at discount (backwardation)
        # This is a rough estimate - proper spot price fetching would improve accuracy
        funding_implied_basis = weighted_funding * 100  # Convert to approximate %
        spot_price = price / (1 + funding_implied_basis) if price > 0 else 0

        return CLRIInput(
            symbol=base_symbol,
            timestamp=datetime.now(timezone.utc),
            funding_rate=weighted_funding,
            open_interest=total_oi,
            oi_change_1h_pct=oi_change_pct,
            perp_volume_24h=total_volume,
            spot_volume_24h=total_volume * 0.7,  # Estimate
            price=price,
            spot_price=spot_price,  # Estimated from funding rate
            # Liquidations filled by separate module
            liquidations_1h_usd=0,
            long_liquidations_1h=0,
            short_liquidations_1h=0,
        )
    
    async def _analyze_cross_exchange(self):
        """Detect cross-exchange signals"""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                for base_symbol in self.base_symbols:
                    signals = self._detect_cross_exchange_signals(base_symbol)
                    
                    if signals and self.on_cross_exchange_signal:
                        await self.on_cross_exchange_signal(signals)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cross-exchange analysis error: {e}")
    
    def _detect_cross_exchange_signals(self, base_symbol: str) -> Optional[dict]:
        """Detect interesting cross-exchange patterns"""
        exchange_data = []
        
        for exchange_id in self.enabled_exchanges:
            if base_symbol in self._exchange_data[exchange_id]:
                exchange_data.append(self._exchange_data[exchange_id][base_symbol])
        
        if len(exchange_data) < 2:
            return None
        
        # Funding rate divergence
        funding_rates = [d.funding_rate for d in exchange_data]
        funding_spread = max(funding_rates) - min(funding_rates)
        
        # OI concentration
        oi_values = [d.open_interest for d in exchange_data if d.open_interest > 0]
        if oi_values:
            total_oi = sum(oi_values)
            max_oi = max(oi_values)
            oi_concentration = max_oi / total_oi if total_oi > 0 else 0
        else:
            oi_concentration = 0
        
        signals = {
            'symbol': base_symbol,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'funding_spread': funding_spread,
            'oi_concentration': oi_concentration,
            'exchanges': {d.exchange_id: d.funding_rate for d in exchange_data},
        }
        
        # Alert thresholds
        if funding_spread > 0.0005:  # 0.05% spread
            signals['alert'] = 'FUNDING_DIVERGENCE'
            signals['message'] = f"{base_symbol} funding divergence: {funding_spread:.4%} spread across exchanges"
            return signals
        
        if oi_concentration > 0.7:  # One exchange has >70% of OI
            signals['alert'] = 'OI_CONCENTRATION'
            signals['message'] = f"{base_symbol} OI concentrated: {oi_concentration:.1%} on single exchange"
            return signals
        
        return None
    
    def get_exchange_breakdown(self, base_symbol: str) -> dict:
        """Get per-exchange data for a symbol (for dashboard)"""
        breakdown = {}

        for exchange_id in self.enabled_exchanges:
            if base_symbol in self._exchange_data[exchange_id]:
                data = self._exchange_data[exchange_id][base_symbol]
                breakdown[exchange_id] = {
                    'funding_rate': data.funding_rate,
                    'open_interest': data.open_interest,
                    'volume_24h': data.volume_24h,
                    'price': data.price,
                }

        return breakdown

    def get_current_oi(self) -> dict[str, float]:
        """
        Get total OI per base symbol across all exchanges.
        Used for dynamic OI-weighted Market CLRI calculation.

        Returns:
            Dict mapping base symbol to total OI in USD
        """
        oi = {}
        for base_symbol in self.base_symbols:
            total = 0
            for exchange_id in self.enabled_exchanges:
                if base_symbol in self._exchange_data[exchange_id]:
                    total += self._exchange_data[exchange_id][base_symbol].open_interest
            if total > 0:
                oi[base_symbol] = total
        return oi
