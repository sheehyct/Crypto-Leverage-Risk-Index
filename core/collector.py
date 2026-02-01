"""
CCXT Pro WebSocket Data Collector

Streams real-time funding rates, open interest, and trades
from multiple exchanges for CLRI calculation.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional
from dataclasses import dataclass, field
import ccxt.pro as ccxtpro
from collections import defaultdict

from core.clri import CLRIInput

logger = logging.getLogger(__name__)


@dataclass
class ExchangeConfig:
    """Configuration for an exchange connection"""
    exchange_id: str
    symbols: list[str]
    options: dict = field(default_factory=dict)
    

# Default symbols to track (perpetuals)
DEFAULT_SYMBOLS = [
    'BTC/USDT:USDT',
    'ETH/USDT:USDT', 
    'SOL/USDT:USDT',
    'XRP/USDT:USDT',
    'DOGE/USDT:USDT',
    'AVAX/USDT:USDT',
    'LINK/USDT:USDT',
    'ARB/USDT:USDT',
    'OP/USDT:USDT',
    'SUI/USDT:USDT',
]


class DataCollector:
    """
    Collects real-time data from crypto exchanges using CCXT Pro WebSockets
    
    Data collected:
    - Funding rates (polled, as most exchanges don't stream this)
    - Open interest (polled or streamed where available)
    - Trades/Tickers (streamed for volume calculation)
    - Order book (streamed for depth analysis)
    """
    
    def __init__(
        self,
        exchanges: list[ExchangeConfig] = None,
        on_data_callback: Optional[Callable[[CLRIInput], None]] = None,
        polling_interval: int = 60,  # seconds for funding/OI polling
    ):
        self.exchange_configs = exchanges or [
            ExchangeConfig('binanceusdm', DEFAULT_SYMBOLS),
        ]
        
        self.on_data_callback = on_data_callback
        self.polling_interval = polling_interval
        
        # Exchange instances
        self.exchanges: dict[str, ccxtpro.Exchange] = {}
        
        # Cached data
        self._funding_cache: dict[str, dict] = defaultdict(dict)
        self._oi_cache: dict[str, dict] = defaultdict(dict)
        self._ticker_cache: dict[str, dict] = defaultdict(dict)
        self._orderbook_cache: dict[str, dict] = defaultdict(dict)
        self._liquidations_cache: dict[str, dict] = defaultdict(lambda: {'long': 0, 'short': 0, 'total': 0})
        
        # Previous OI for change calculation
        self._previous_oi: dict[str, float] = {}
        
        # Running flag
        self._running = False
        self._tasks: list[asyncio.Task] = []
    
    async def _init_exchange(self, config: ExchangeConfig) -> ccxtpro.Exchange:
        """Initialize exchange connection"""
        exchange_class = getattr(ccxtpro, config.exchange_id)
        
        options = {
            'enableRateLimit': True,
            **config.options
        }
        
        exchange = exchange_class(options)
        
        # Load markets
        await exchange.load_markets()
        
        logger.info(f"Initialized {config.exchange_id} with {len(exchange.symbols)} markets")
        
        return exchange
    
    async def start(self):
        """Start all data collection tasks"""
        logger.info("Starting data collector...")
        self._running = True
        
        # Initialize exchanges
        for config in self.exchange_configs:
            try:
                exchange = await self._init_exchange(config)
                self.exchanges[config.exchange_id] = exchange
            except Exception as e:
                logger.error(f"Failed to initialize {config.exchange_id}: {e}")
        
        # Start collection tasks
        for config in self.exchange_configs:
            if config.exchange_id not in self.exchanges:
                continue
                
            exchange = self.exchanges[config.exchange_id]
            
            # Polling tasks (funding, OI)
            self._tasks.append(
                asyncio.create_task(
                    self._poll_funding_oi(exchange, config.symbols)
                )
            )
            
            # Streaming tasks (tickers, orderbooks)
            for symbol in config.symbols:
                if symbol in exchange.markets:
                    self._tasks.append(
                        asyncio.create_task(
                            self._stream_ticker(exchange, symbol)
                        )
                    )
                    self._tasks.append(
                        asyncio.create_task(
                            self._stream_orderbook(exchange, symbol)
                        )
                    )
        
        # Aggregation task - combines all data and calls callback
        self._tasks.append(
            asyncio.create_task(self._aggregate_and_emit())
        )
        
        logger.info(f"Started {len(self._tasks)} collection tasks")
    
    async def stop(self):
        """Stop all collection tasks and close connections"""
        logger.info("Stopping data collector...")
        self._running = False
        
        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        
        # Wait for cancellation
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Close exchanges
        for exchange in self.exchanges.values():
            await exchange.close()
        
        self._tasks = []
        self.exchanges = {}
        logger.info("Data collector stopped")
    
    async def _poll_funding_oi(self, exchange: ccxtpro.Exchange, symbols: list[str]):
        """Poll funding rates and open interest periodically"""
        while self._running:
            try:
                for symbol in symbols:
                    if symbol not in exchange.markets:
                        continue
                    
                    try:
                        # Fetch funding rate
                        funding = await exchange.fetch_funding_rate(symbol)
                        self._funding_cache[exchange.id][symbol] = {
                            'rate': funding.get('fundingRate', 0),
                            'predicted': funding.get('nextFundingRate'),
                            'timestamp': funding.get('timestamp', datetime.now(timezone.utc).timestamp() * 1000),
                        }
                        
                        # Fetch open interest
                        try:
                            oi = await exchange.fetch_open_interest(symbol)
                            current_oi = oi.get('openInterestValue', 0) or oi.get('openInterest', 0)
                            
                            # Calculate change
                            prev_oi = self._previous_oi.get(f"{exchange.id}:{symbol}", current_oi)
                            oi_change_pct = ((current_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0
                            self._previous_oi[f"{exchange.id}:{symbol}"] = current_oi
                            
                            self._oi_cache[exchange.id][symbol] = {
                                'oi': current_oi,
                                'change_pct': oi_change_pct,
                                'timestamp': datetime.now(timezone.utc).timestamp() * 1000,
                            }
                        except Exception as e:
                            logger.debug(f"OI fetch failed for {symbol}: {e}")
                        
                    except Exception as e:
                        logger.warning(f"Funding fetch failed for {symbol}: {e}")
                    
                    # Small delay between symbols to avoid rate limits
                    await asyncio.sleep(0.5)
                
                # Wait for next poll cycle
                await asyncio.sleep(self.polling_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)
    
    async def _stream_ticker(self, exchange: ccxtpro.Exchange, symbol: str):
        """Stream ticker data for a symbol"""
        while self._running:
            try:
                ticker = await exchange.watch_ticker(symbol)
                
                self._ticker_cache[exchange.id][symbol] = {
                    'last': ticker.get('last', 0),
                    'volume': ticker.get('quoteVolume', 0),  # 24h volume in quote currency
                    'change_pct': ticker.get('percentage', 0),
                    'timestamp': ticker.get('timestamp', datetime.now(timezone.utc).timestamp() * 1000),
                }
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Ticker stream error for {symbol}: {e}")
                await asyncio.sleep(1)
    
    async def _stream_orderbook(self, exchange: ccxtpro.Exchange, symbol: str, depth: int = 20):
        """Stream order book for depth analysis"""
        while self._running:
            try:
                orderbook = await exchange.watch_order_book(symbol, depth)
                
                # Calculate depth within 1% of mid price
                if orderbook['bids'] and orderbook['asks']:
                    mid_price = (orderbook['bids'][0][0] + orderbook['asks'][0][0]) / 2
                    threshold = mid_price * 0.01  # 1%
                    
                    bid_depth = sum(
                        bid[0] * bid[1] for bid in orderbook['bids']
                        if bid[0] >= mid_price - threshold
                    )
                    ask_depth = sum(
                        ask[0] * ask[1] for ask in orderbook['asks']
                        if ask[0] <= mid_price + threshold
                    )
                    
                    self._orderbook_cache[exchange.id][symbol] = {
                        'bid_depth': bid_depth,
                        'ask_depth': ask_depth,
                        'mid_price': mid_price,
                        'spread_pct': (orderbook['asks'][0][0] - orderbook['bids'][0][0]) / mid_price * 100,
                        'timestamp': orderbook.get('timestamp', datetime.now(timezone.utc).timestamp() * 1000),
                    }
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Orderbook stream error for {symbol}: {e}")
                await asyncio.sleep(1)
    
    async def _aggregate_and_emit(self):
        """Aggregate all cached data and emit CLRIInput objects"""
        while self._running:
            try:
                await asyncio.sleep(5)  # Emit every 5 seconds
                
                for exchange_id, exchange in self.exchanges.items():
                    for config in self.exchange_configs:
                        if config.exchange_id != exchange_id:
                            continue
                        
                        for symbol in config.symbols:
                            try:
                                clri_input = self._build_clri_input(exchange_id, symbol)
                                if clri_input and self.on_data_callback:
                                    self.on_data_callback(clri_input)
                            except Exception as e:
                                logger.debug(f"Error building CLRIInput for {symbol}: {e}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Aggregation error: {e}")
                await asyncio.sleep(1)
    
    def _build_clri_input(self, exchange_id: str, symbol: str) -> Optional[CLRIInput]:
        """Build a CLRIInput from cached data"""
        funding = self._funding_cache.get(exchange_id, {}).get(symbol, {})
        oi = self._oi_cache.get(exchange_id, {}).get(symbol, {})
        ticker = self._ticker_cache.get(exchange_id, {}).get(symbol, {})
        orderbook = self._orderbook_cache.get(exchange_id, {}).get(symbol, {})
        liqs = self._liquidations_cache.get(f"{exchange_id}:{symbol}", {})
        
        # Need at least funding data
        if not funding:
            return None
        
        # Normalize symbol for display
        display_symbol = symbol.replace('/USDT:USDT', '').replace(':USDT', '')
        
        return CLRIInput(
            symbol=display_symbol,
            timestamp=datetime.now(timezone.utc),
            
            # Funding
            funding_rate=funding.get('rate', 0),
            predicted_funding=funding.get('predicted'),
            
            # OI
            open_interest=oi.get('oi', 0),
            oi_change_1h_pct=oi.get('change_pct', 0),
            
            # Volume (we'd need spot volume too for ratio - placeholder)
            perp_volume_24h=ticker.get('volume', 0),
            spot_volume_24h=ticker.get('volume', 0) * 0.7,  # Rough estimate, ideally fetch from spot
            
            # Order book
            bid_depth_usd=orderbook.get('bid_depth', 0),
            ask_depth_usd=orderbook.get('ask_depth', 0),
            
            # Liquidations (would need separate feed - placeholder)
            liquidations_1h_usd=liqs.get('total', 0),
            long_liquidations_1h=liqs.get('long', 0),
            short_liquidations_1h=liqs.get('short', 0),
            
            # Price
            price=ticker.get('last', 0),
            price_change_1h_pct=ticker.get('change_pct', 0),
        )
    
    def get_current_data(self, symbol: str) -> Optional[CLRIInput]:
        """Get current aggregated data for a symbol"""
        for exchange_id in self.exchanges:
            data = self._build_clri_input(exchange_id, symbol)
            if data:
                return data
        return None
    
    def get_all_symbols(self) -> list[str]:
        """Get all tracked symbols"""
        symbols = set()
        for config in self.exchange_configs:
            for symbol in config.symbols:
                display = symbol.replace('/USDT:USDT', '').replace(':USDT', '')
                symbols.add(display)
        return sorted(symbols)


async def test_collector():
    """Test the data collector"""
    logging.basicConfig(level=logging.INFO)
    
    def on_data(data: CLRIInput):
        print(f"{data.symbol}: funding={data.funding_rate:.6f}, OI=${data.open_interest:,.0f}")
    
    collector = DataCollector(
        exchanges=[
            ExchangeConfig('binanceusdm', ['BTC/USDT:USDT', 'ETH/USDT:USDT']),
        ],
        on_data_callback=on_data,
        polling_interval=30,
    )
    
    try:
        await collector.start()
        await asyncio.sleep(120)  # Run for 2 minutes
    finally:
        await collector.stop()


if __name__ == '__main__':
    asyncio.run(test_collector())
