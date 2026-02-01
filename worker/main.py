"""
CLRI Worker Service

Main background worker that:
1. Collects data from multiple exchanges via CCXT Pro
2. Integrates real liquidation data from Coinglass
3. Calculates CLRI scores
4. Stores readings in database
5. Sends Discord alerts when thresholds are crossed
6. Optionally exports data for ATLAS integration
"""

import asyncio
import logging
import os
import signal
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.clri import CLRICalculator, CLRIInput, CLRIOutput, AggregateCLRI, MarketCLRIOutput
from core.multi_exchange import MultiExchangeCollector, DEFAULT_BASE_SYMBOLS
from core.liquidations import LiquidationCollector, LiquidationData, LiquidationAggregator
from core.discord_alerter import DiscordAlerter
from core.discord_cleanup import DiscordCleanupBot
from core.database import (
    Database, CLRIReading, Alert, MarketSummary,
    save_clri_reading, save_alert, RiskLevel, Direction,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


class CLRIWorker:
    """
    Main worker service for CLRI calculation and alerting
    
    Enhanced with:
    - Multi-exchange data aggregation
    - Real liquidation data from Coinglass
    - ATLAS export capability
    """
    
    def __init__(
        self,
        database_url: str,
        discord_webhook_url: Optional[str] = None,
        coinglass_api_key: Optional[str] = None,
        enabled_exchanges: list[str] = None,
        symbols: list[str] = None,
        polling_interval: int = 60,
        summary_interval: int = 900,  # 15 minutes
        atlas_export_path: Optional[str] = None,  # For ATLAS integration
        discord_bot_token: Optional[str] = None,  # For message cleanup
        discord_channel_id: Optional[int] = None,  # Channel to clean
    ):
        self.database_url = database_url
        self.discord_webhook_url = discord_webhook_url
        self.coinglass_api_key = coinglass_api_key
        self.polling_interval = polling_interval
        self.summary_interval = summary_interval
        self.atlas_export_path = atlas_export_path
        self.discord_bot_token = discord_bot_token
        self.discord_channel_id = discord_channel_id
        
        # Exchange and symbol configs
        self.enabled_exchanges = enabled_exchanges or ['binanceusdm', 'bybit', 'okx']
        self.symbols = symbols or DEFAULT_BASE_SYMBOLS
        
        # Components (initialized in start())
        self.db: Optional[Database] = None
        self.exchange_collector: Optional[MultiExchangeCollector] = None
        self.liquidation_collector: Optional[LiquidationCollector] = None
        self.liquidation_aggregator = LiquidationAggregator()
        self.alerter: Optional[DiscordAlerter] = None
        self.cleanup_bot: Optional[DiscordCleanupBot] = None
        self.calculator = CLRICalculator()
        self.aggregate = AggregateCLRI()
        
        # State
        self._running = False
        self._latest_readings: dict[str, CLRIOutput] = {}
        self._latest_liquidations: dict[str, dict] = {}
        self._data_queue: asyncio.Queue = asyncio.Queue()

        # CLRI spike detection: track (timestamp, clri_score) for last 5 minutes
        self._clri_history: dict[str, deque] = {}
        self.CLRI_SPIKE_THRESHOLD = 15  # Points change to trigger spike alert
        self.CLRI_SPIKE_WINDOW = 300  # 5 minutes in seconds

        # Hybrid spike detection: return-to-baseline + cooldown
        self._spike_last_alert_time: dict[str, datetime] = {}  # Last alert timestamp
        self._spike_last_alert_score: dict[str, int] = {}  # CLRI score when alert was sent
        self._spike_baseline_reset: dict[str, bool] = {}  # True if CLRI returned to baseline
        self.SPIKE_COOLDOWN_SECONDS = 300  # 5 minute minimum cooldown
        self.SPIKE_BASELINE_THRESHOLD = 45  # Must drop below MODERATE to "reset"
        self.SPIKE_CONTINUATION_THRESHOLD = 15  # Additional pts for continuation alert

        # Only send spike alerts for major trading symbols
        self.SPIKE_ALERT_SYMBOLS = {'BTC', 'ETH', 'XRP', 'ADA'}

        # Price tracking for validation alerts: (timestamp, price)
        self._price_history: dict[str, deque] = {}
        self.PRICE_MOVE_THRESHOLD = 3.0  # 3% move triggers validation
        self.PRICE_VALIDATION_WINDOW = 3600  # 1 hour in seconds

        # Market CLRI tracking for two-layer alert system
        self._previous_market_clri: int = 0
        self._market_alert_last_time: Optional[datetime] = None
        self.MARKET_ALERT_COOLDOWN = 300  # 5 minute cooldown for market alerts
        self.MARKET_ALERT_INTERVAL = 30  # Check market alerts every 30 seconds
        self.MARKET_THRESHOLD_ELEVATED = 65
        self.MARKET_THRESHOLD_HIGH = 80
        self.MARKET_THRESHOLD_EXTREME = 90

        # Graceful shutdown
        self._shutdown_event = asyncio.Event()

    async def _load_history_from_db(self):
        """
        Load historical readings from database to initialize z-score calculations.
        This prevents CLRI from resetting to low values after worker restart.
        """
        from sqlalchemy import select, desc

        logger.info("Loading historical data from database...")

        try:
            async with self.db.session() as session:
                for symbol in self.symbols:
                    # Query last 100 readings for this symbol
                    query = (
                        select(CLRIReading)
                        .where(CLRIReading.symbol == symbol)
                        .order_by(desc(CLRIReading.timestamp))
                        .limit(100)
                    )
                    result = await session.execute(query)
                    readings = result.scalars().all()

                    if not readings:
                        logger.debug(f"No historical data for {symbol}")
                        continue

                    # Reverse to get oldest first
                    readings = list(reversed(readings))

                    # Extract history arrays
                    funding_rates = [r.funding_rate for r in readings if r.funding_rate is not None]
                    open_interests = [r.open_interest for r in readings if r.open_interest is not None]

                    # Calculate volume ratios
                    volume_ratios = []
                    for r in readings:
                        if r.spot_volume_24h and r.spot_volume_24h > 0 and r.perp_volume_24h:
                            volume_ratios.append(r.perp_volume_24h / r.spot_volume_24h)

                    liquidations = [r.liquidations_1h_usd for r in readings if r.liquidations_1h_usd is not None]

                    # Get last known CLRI score
                    last_score = readings[-1].clri_score if readings else 0

                    # Load into calculator
                    self.calculator.load_history(
                        symbol=symbol,
                        funding_rates=funding_rates,
                        open_interests=open_interests,
                        volume_ratios=volume_ratios,
                        liquidations=liquidations,
                        previous_score=last_score,
                    )

                    logger.info(
                        f"Loaded {len(readings)} historical readings for {symbol} "
                        f"(last CLRI: {last_score})"
                    )

        except Exception as e:
            logger.error(f"Failed to load history from database: {e}")
            logger.info("Continuing with empty history (scores will normalize over time)")

    async def start(self):
        """Start the worker service"""
        logger.info("Starting CLRI Worker...")

        # Initialize database
        self.db = Database(self.database_url)
        await self.db.create_tables()
        logger.info("Database initialized")

        # Load historical data to initialize z-score calculations
        await self._load_history_from_db()
        
        # Initialize Discord alerter
        if self.discord_webhook_url:
            self.alerter = DiscordAlerter(self.discord_webhook_url)
            await self.alerter.send_test_message()
            logger.info("Discord alerter initialized")

        # Initialize Discord cleanup bot (for automatic message cleanup)
        if self.discord_bot_token and self.discord_channel_id:
            self.cleanup_bot = DiscordCleanupBot(
                bot_token=self.discord_bot_token,
                channel_id=self.discord_channel_id,
                max_age_hours=24,  # Delete messages older than 24 hours
                check_interval_seconds=3600,  # Check every hour
                keep_recent_count=10,  # Always keep most recent 10 messages
            )
            await self.cleanup_bot.start()
            logger.info("Discord cleanup bot initialized")

        # Initialize multi-exchange collector
        self.exchange_collector = MultiExchangeCollector(
            base_symbols=self.symbols,
            enabled_exchanges=self.enabled_exchanges,
            on_data_callback=self._on_exchange_data,
            on_cross_exchange_signal=self._on_cross_exchange_signal,
            polling_interval=self.polling_interval,
        )
        
        # Initialize liquidation collector (Coinglass)
        if self.coinglass_api_key:
            self.liquidation_collector = LiquidationCollector(
                api_key=self.coinglass_api_key,
                symbols=self.symbols,
                on_data_callback=self._on_liquidation_data,
                on_spike_callback=self._on_liquidation_spike,
                polling_interval=self.polling_interval,
            )
            logger.info("Coinglass liquidation collector initialized")
        else:
            logger.warning("COINGLASS_API_KEY not set, liquidation data will be estimated")
        
        self._running = True
        
        # Start collectors
        await self.exchange_collector.start()
        
        if self.liquidation_collector:
            await self.liquidation_collector.start()
        
        # Start processing tasks
        tasks = [
            asyncio.create_task(self._process_data_queue()),
            asyncio.create_task(self._market_alert_loop()),  # Two-layer alert system
            asyncio.create_task(self._periodic_summary()),
            asyncio.create_task(self._price_validation_loop()),
            asyncio.create_task(self._wait_for_shutdown()),
        ]

        # ATLAS export task if configured
        if self.atlas_export_path:
            tasks.append(asyncio.create_task(self._atlas_export_loop()))
        
        logger.info("CLRI Worker started successfully")
        logger.info(f"Tracking {len(self.symbols)} symbols across {len(self.enabled_exchanges)} exchanges")
        
        # Wait for shutdown
        await self._shutdown_event.wait()
        
        # Cleanup
        logger.info("Shutting down...")
        self._running = False
        
        for task in tasks:
            task.cancel()
        
        await asyncio.gather(*tasks, return_exceptions=True)
        
        if self.exchange_collector:
            await self.exchange_collector.stop()

        if self.liquidation_collector:
            await self.liquidation_collector.stop()

        if self.alerter:
            await self.alerter.close()

        if self.cleanup_bot:
            await self.cleanup_bot.stop()

        if self.db:
            await self.db.close()
        
        logger.info("CLRI Worker stopped")
    
    def _on_exchange_data(self, data: CLRIInput):
        """Callback when new exchange data is received"""
        # Merge with liquidation data if available
        if data.symbol in self._latest_liquidations:
            liq = self._latest_liquidations[data.symbol]
            data.liquidations_1h_usd = liq.get('liquidations_1h_usd', 0)
            data.long_liquidations_1h = liq.get('long_liquidations_1h', 0)
            data.short_liquidations_1h = liq.get('short_liquidations_1h', 0)
        
        try:
            self._data_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("Data queue full, dropping reading")
    
    def _on_liquidation_data(self, liq_data: LiquidationData):
        """Callback when liquidation data is received"""
        processed = self.liquidation_aggregator.process(liq_data)
        self._latest_liquidations[liq_data.symbol] = processed
    
    async def _on_liquidation_spike(self, liq_data: LiquidationData):
        """
        Alert Type 1: Liquidation Event Alert
        Triggered when large $ liquidations occur
        """
        if self.alerter:
            # Get current CLRI reading for this symbol
            current_reading = self._latest_readings.get(liq_data.symbol)
            current_clri = current_reading.clri_score if current_reading else 0
            risk_level = current_reading.risk_level if current_reading else "LOW"
            direction = current_reading.direction if current_reading else "BALANCED"

            await self.alerter.send_liquidation_event_alert(
                symbol=liq_data.symbol,
                total_1h_usd=liq_data.total_1h_usd,
                long_1h_usd=liq_data.long_1h_usd,
                short_1h_usd=liq_data.short_1h_usd,
                current_clri=current_clri,
                risk_level=risk_level,
                direction=direction,
            )
            logger.info(
                f"Liquidation event alert: {liq_data.symbol} "
                f"${liq_data.total_1h_usd:,.0f} liquidated"
            )
    
    async def _on_cross_exchange_signal(self, signal: dict):
        """Callback when cross-exchange signal is detected"""
        if self.alerter and signal.get('alert'):
            # Could send a special alert for cross-exchange divergences
            logger.info(f"Cross-exchange signal: {signal.get('message')}")
    
    async def _atlas_export_loop(self):
        """Export CLRI data for ATLAS integration"""
        import json
        
        while self._running:
            try:
                await asyncio.sleep(60)  # Export every minute
                
                if not self._latest_readings:
                    continue
                
                # Build export data
                export_data = {
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'market_clri': self.get_market_summary(),
                    'symbols': {
                        symbol: {
                            'clri_score': output.clri_score,
                            'risk_level': output.risk_level,
                            'direction': output.direction,
                            'funding_score': output.funding_score,
                            'oi_score': output.oi_score,
                            'liquidation_score': output.liquidation_score,
                        }
                        for symbol, output in self._latest_readings.items()
                    },
                    'liquidations': self._latest_liquidations,
                }
                
                # Write to file (ATLAS can read this)
                with open(self.atlas_export_path, 'w') as f:
                    json.dump(export_data, f)
                
                logger.debug(f"ATLAS export written to {self.atlas_export_path}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ATLAS export error: {e}")
    
    async def _process_data_queue(self):
        """Process incoming data from the queue"""
        while self._running:
            try:
                # Get data with timeout
                try:
                    data = await asyncio.wait_for(
                        self._data_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                # Calculate CLRI
                output = self.calculator.calculate(data)

                # NOTE: Individual spike alerts disabled in favor of Market CLRI two-layer system
                # Divergence detection (25+ pts above market) handled by _market_alert_loop()
                # await self._check_clri_spike(output)

                # Update latest reading
                self._latest_readings[data.symbol] = output

                # Track CLRI history for spike detection
                self._update_clri_history(output)

                # Track price history for validation
                self._update_price_history(data.symbol, data.price)

                # Log
                logger.debug(
                    f"{output.symbol}: CLRI={output.clri_score} "
                    f"({output.risk_level}, {output.direction})"
                )

                # Save to database
                await self._save_reading(data, output)

                # NOTE: Individual threshold alerts disabled in favor of Market CLRI alerts
                # Market-wide alerts and divergence detection handled by _market_alert_loop()
                # if output.should_alert and self.alerter:
                #     await self._send_alert(output)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing data: {e}", exc_info=True)

    async def _market_alert_loop(self):
        """
        Two-layer Market CLRI alert system.

        Layer 1 (Primary): Market CLRI threshold alerts
        - Single alert when market-wide CLRI crosses 65/80/90
        - Uses dynamic OI weighting
        - One alert covers the whole market

        Layer 2 (Divergence): Individual symbol divergence alerts
        - Only fires when a symbol is 25+ points above market CLRI
        - Catches unusual single-coin leverage buildup
        """
        while self._running:
            try:
                await asyncio.sleep(self.MARKET_ALERT_INTERVAL)

                if not self._latest_readings or not self.alerter:
                    continue

                readings = list(self._latest_readings.values())
                if not readings:
                    continue

                # Get current OI from exchange collector for dynamic weighting
                oi_data = {}
                if self.exchange_collector:
                    oi_data = self.exchange_collector.get_current_oi()

                # Calculate market CLRI with dynamic OI weighting
                market_output = self.aggregate.calculate_market_clri(readings, oi_data)
                market_clri = market_output.market_clri

                # --- Layer 1: Market CLRI Threshold Alerts ---
                now = datetime.now(timezone.utc)
                can_alert = (
                    self._market_alert_last_time is None or
                    (now - self._market_alert_last_time).total_seconds() >= self.MARKET_ALERT_COOLDOWN
                )

                if can_alert:
                    # Check for threshold crossings
                    prev = self._previous_market_clri
                    alert_sent = False

                    if market_clri >= self.MARKET_THRESHOLD_EXTREME and prev < self.MARKET_THRESHOLD_EXTREME:
                        await self.alerter.send_market_clri_alert(market_output, "THRESHOLD", prev)
                        alert_sent = True
                        logger.info(f"Market CLRI EXTREME alert: {prev} -> {market_clri}")
                    elif market_clri >= self.MARKET_THRESHOLD_HIGH and prev < self.MARKET_THRESHOLD_HIGH:
                        await self.alerter.send_market_clri_alert(market_output, "THRESHOLD", prev)
                        alert_sent = True
                        logger.info(f"Market CLRI HIGH alert: {prev} -> {market_clri}")
                    elif market_clri >= self.MARKET_THRESHOLD_ELEVATED and prev < self.MARKET_THRESHOLD_ELEVATED:
                        await self.alerter.send_market_clri_alert(market_output, "THRESHOLD", prev)
                        alert_sent = True
                        logger.info(f"Market CLRI ELEVATED alert: {prev} -> {market_clri}")

                    # Also check for rapid market spikes (15+ pts in short window)
                    if not alert_sent and abs(market_clri - prev) >= 15:
                        await self.alerter.send_market_clri_alert(market_output, "SPIKE", prev)
                        alert_sent = True
                        logger.info(f"Market CLRI SPIKE alert: {prev} -> {market_clri}")

                    if alert_sent:
                        self._market_alert_last_time = now

                # Update previous for next iteration
                self._previous_market_clri = market_clri

                # --- Layer 2: Divergence Alerts ---
                for symbol, symbol_clri, divergence in market_output.divergent_symbols:
                    # Find the reading to get direction and risk level
                    reading = next((r for r in readings if r.symbol == symbol), None)
                    if reading:
                        await self.alerter.send_divergence_alert(
                            symbol=symbol,
                            symbol_clri=symbol_clri,
                            market_clri=market_clri,
                            divergence=divergence,
                            direction=reading.direction,
                            risk_level=reading.risk_level,
                        )
                        logger.info(f"Divergence alert: {symbol} CLRI {symbol_clri} vs Market {market_clri} (+{divergence})")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market alert loop: {e}", exc_info=True)

    async def _save_reading(self, data: CLRIInput, output: CLRIOutput):
        """Save a CLRI reading to the database"""
        try:
            async with self.db.session() as session:
                reading = CLRIReading(
                    timestamp=output.timestamp,
                    symbol=output.symbol,
                    
                    # Raw inputs
                    funding_rate=data.funding_rate,
                    predicted_funding=data.predicted_funding,
                    open_interest=data.open_interest,
                    oi_change_1h_pct=data.oi_change_1h_pct,
                    perp_volume_24h=data.perp_volume_24h,
                    spot_volume_24h=data.spot_volume_24h,
                    liquidations_1h_usd=data.liquidations_1h_usd,
                    bid_depth_usd=data.bid_depth_usd,
                    ask_depth_usd=data.ask_depth_usd,
                    price=data.price,
                    
                    # Component scores
                    funding_score=output.funding_score,
                    oi_score=output.oi_score,
                    volume_score=output.volume_score,
                    liquidation_score=output.liquidation_score,
                    imbalance_score=output.imbalance_score,

                    # High-value signals
                    basis_pct=output.basis_pct,
                    liquidation_oi_ratio=output.liquidation_oi_ratio,
                    funding_oi_stress=output.funding_oi_stress,
                    oi_price_divergence=output.oi_price_divergence,

                    # Composite
                    clri_score=output.clri_score,
                    direction=Direction[output.direction],
                    direction_confidence=output.direction_confidence,
                    risk_level=RiskLevel[output.risk_level],

                    alert_triggered=output.should_alert,

                    # Composite stress (algorithm enhancement 4D)
                    composite_stress=output.composite_stress,
                    stress_level=output.stress_level,

                    # Dynamic weighting (algorithm enhancement 4C)
                    detected_regime=output.detected_regime,
                    regime_intensity=output.regime_intensity,
                )
                
                await save_clri_reading(session, reading)
                
        except Exception as e:
            logger.error(f"Failed to save reading: {e}")
    
    async def _send_alert(self, output: CLRIOutput):
        """Send a Discord alert (Threshold crossed - Alert Type 3)"""
        try:
            success = await self.alerter.send_clri_alert(output)

            if success:
                # Save alert record
                async with self.db.session() as session:
                    alert = Alert(
                        triggered_at=output.timestamp,
                        symbol=output.symbol,
                        alert_type='THRESHOLD_CROSSED',
                        clri_score=output.clri_score,
                        risk_level=RiskLevel[output.risk_level],
                        direction=Direction[output.direction],
                        message=output.alert_message,
                        discord_sent=True,
                    )
                    await save_alert(session, alert)

                logger.info(f"Alert sent for {output.symbol}: {output.alert_message}")

        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    def _update_clri_history(self, output: CLRIOutput):
        """Track CLRI scores over time for spike detection"""
        symbol = output.symbol
        now = datetime.now(timezone.utc)

        if symbol not in self._clri_history:
            self._clri_history[symbol] = deque(maxlen=100)

        self._clri_history[symbol].append((now, output.clri_score))

        # Prune old entries beyond the spike window
        cutoff = now - timedelta(seconds=self.CLRI_SPIKE_WINDOW)
        while self._clri_history[symbol] and self._clri_history[symbol][0][0] < cutoff:
            self._clri_history[symbol].popleft()

    async def _check_clri_spike(self, output: CLRIOutput):
        """
        Alert Type 2: CLRI Spike Alert (Hybrid Detection)

        Only alerts for major symbols (BTC, ETH, XRP, ADA) with:
        1. Return-to-baseline logic: After alerting, only alert again if:
           - CLRI drops back below 30, THEN spikes up again, OR
           - CLRI continues UP by another 15 pts from alert level
        2. Minimum 5-minute cooldown as safety net
        """
        symbol = output.symbol
        now = datetime.now(timezone.utc)

        # Only alert for major trading symbols
        if symbol not in self.SPIKE_ALERT_SYMBOLS:
            return

        if not self.alerter:
            return

        if symbol not in self._clri_history or len(self._clri_history[symbol]) < 2:
            return

        new_score = output.clri_score

        # Check if baseline has been reset (CLRI dropped below threshold)
        if new_score < self.SPIKE_BASELINE_THRESHOLD:
            self._spike_baseline_reset[symbol] = True

        # Check cooldown
        if symbol in self._spike_last_alert_time:
            time_since_alert = (now - self._spike_last_alert_time[symbol]).total_seconds()
            if time_since_alert < self.SPIKE_COOLDOWN_SECONDS:
                # Still in cooldown - check for continuation spike only
                last_alert_score = self._spike_last_alert_score.get(symbol, 0)
                if new_score >= last_alert_score + self.SPIKE_CONTINUATION_THRESHOLD:
                    # Continuation spike - CLRI kept climbing
                    pass  # Allow alert
                else:
                    return  # Skip - still in cooldown and no continuation

        # Find the oldest reading within the spike window for baseline comparison
        history = self._clri_history[symbol]
        cutoff = now - timedelta(seconds=self.CLRI_SPIKE_WINDOW)

        oldest_in_window = None
        for ts, score in history:
            if ts >= cutoff:
                oldest_in_window = (ts, score)
                break

        if oldest_in_window is None:
            return

        old_score = oldest_in_window[1]
        change = new_score - old_score
        time_diff = (now - oldest_in_window[0]).total_seconds()

        # Check if this qualifies as a spike
        if abs(change) < self.CLRI_SPIKE_THRESHOLD:
            return

        # Only alert if score is in meaningful territory (MODERATE+)
        # Either spike INTO 45+ territory, OUT OF 45+ territory, or WITHIN 45+ territory
        if new_score < self.SPIKE_BASELINE_THRESHOLD and old_score < self.SPIKE_BASELINE_THRESHOLD:
            return  # Both scores below MODERATE threshold - not actionable

        # Check return-to-baseline requirement (unless this is first alert for symbol)
        if symbol in self._spike_last_alert_time:
            baseline_reset = self._spike_baseline_reset.get(symbol, False)
            last_alert_score = self._spike_last_alert_score.get(symbol, 0)

            # Allow alert if: baseline was reset OR this is a continuation spike
            is_continuation = new_score >= last_alert_score + self.SPIKE_CONTINUATION_THRESHOLD
            if not baseline_reset and not is_continuation:
                return  # Skip - baseline not reset and not a continuation

        # Send the alert
        await self.alerter.send_clri_spike_alert(
            symbol=symbol,
            clri_before=old_score,
            clri_after=new_score,
            change_points=change,
            time_window_seconds=int(time_diff),
            direction=output.direction,
            risk_level=output.risk_level,
        )

        # Update tracking state
        self._spike_last_alert_time[symbol] = now
        self._spike_last_alert_score[symbol] = new_score
        self._spike_baseline_reset[symbol] = False  # Reset the baseline flag

        logger.info(
            f"CLRI spike alert: {symbol} {old_score} -> {new_score} "
            f"({change:+d} pts in {time_diff:.0f}s)"
        )

        # Save spike alert to database
        try:
            async with self.db.session() as session:
                alert = Alert(
                    triggered_at=now,
                    symbol=symbol,
                    alert_type='CLRI_SPIKE',
                    clri_score=new_score,
                    risk_level=RiskLevel[output.risk_level],
                    direction=Direction[output.direction],
                    message=f"CLRI spike: {old_score} -> {new_score} ({change:+d} pts)",
                    discord_sent=True,
                )
                await save_alert(session, alert)
        except Exception as e:
            logger.error(f"Failed to save spike alert: {e}")

    def _update_price_history(self, symbol: str, price: float):
        """Track prices over time for validation alerts"""
        if price <= 0:
            return

        now = datetime.now(timezone.utc)

        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=200)

        self._price_history[symbol].append((now, price))

        # Prune old entries beyond the validation window
        cutoff = now - timedelta(seconds=self.PRICE_VALIDATION_WINDOW)
        while self._price_history[symbol] and self._price_history[symbol][0][0] < cutoff:
            self._price_history[symbol].popleft()
    
    async def _periodic_summary(self):
        """Send periodic market summary (saves to DB, logging only)"""
        while self._running:
            try:
                await asyncio.sleep(self.summary_interval)

                if not self._latest_readings:
                    continue

                # Calculate market aggregate with OI weighting
                readings = list(self._latest_readings.values())
                oi_data = {}
                if self.exchange_collector:
                    oi_data = self.exchange_collector.get_current_oi()

                market_output = self.aggregate.calculate_market_clri(readings, oi_data)

                # Save summary to database (alerts now handled by _market_alert_loop)
                async with self.db.session() as session:
                    summary = MarketSummary(
                        timestamp=datetime.now(timezone.utc),
                        market_clri=market_output.market_clri,
                        risk_level=RiskLevel[market_output.risk_level],
                        market_direction=market_output.market_direction,
                        symbols_tracked=len(readings),
                        symbols_elevated=market_output.symbols_elevated,
                        symbols_high=market_output.symbols_high,
                        individual_readings=market_output.individual_readings,
                    )
                    session.add(summary)
                    await session.commit()

                # Log weights for debugging
                top_weights = ", ".join([
                    f"{s}: {w*100:.0f}%"
                    for s, w in sorted(market_output.weights.items(), key=lambda x: -x[1])[:3]
                ])
                logger.info(
                    f"Market CLRI: {market_output.market_clri} "
                    f"({market_output.symbols_elevated} elevated) "
                    f"[Weights: {top_weights}]"
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic summary: {e}")

    async def _price_validation_loop(self):
        """
        Alert Type 3: Price Validation Alert (Post-hoc)
        Check for significant price moves and validate CLRI predictions
        """
        # Track last validated prices to avoid duplicate alerts
        last_validated: dict[str, tuple[datetime, float]] = {}

        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute

                if not self._price_history or not self.alerter:
                    continue

                now = datetime.now(timezone.utc)

                for symbol, history in self._price_history.items():
                    if len(history) < 10:
                        continue

                    # Get price from 1 hour ago (or oldest available)
                    oldest = history[0]
                    newest = history[-1]

                    # Skip if not enough time has passed
                    time_diff = (newest[0] - oldest[0]).total_seconds()
                    if time_diff < 300:  # Need at least 5 minutes of data
                        continue

                    # Calculate price change
                    price_change_pct = ((newest[1] - oldest[1]) / oldest[1]) * 100

                    # Check if this is a significant move
                    if abs(price_change_pct) < self.PRICE_MOVE_THRESHOLD:
                        continue

                    # Check if we already validated this move recently
                    if symbol in last_validated:
                        last_time, last_price = last_validated[symbol]
                        # Skip if validated within last 30 minutes
                        if (now - last_time).total_seconds() < 1800:
                            continue

                    # Get CLRI readings from before and after the move
                    clri_before = 0
                    clri_after = 0
                    direction_before = "BALANCED"

                    if symbol in self._clri_history:
                        clri_hist = list(self._clri_history[symbol])
                        if clri_hist:
                            # Before: oldest reading
                            clri_before = clri_hist[0][1] if clri_hist else 0
                            # After: newest reading
                            clri_after = clri_hist[-1][1] if clri_hist else 0

                    current_reading = self._latest_readings.get(symbol)
                    if current_reading:
                        direction_before = current_reading.direction

                    # Determine if CLRI predicted this move
                    # Elevated (>=65) before the move = predicted
                    was_predicted = clri_before >= 65

                    # Check direction match
                    # Price up + OVERLEVERAGED_SHORT = correct prediction (shorts squeezed)
                    # Price down + OVERLEVERAGED_LONG = correct prediction (longs liquidated)
                    direction_correct = (
                        (price_change_pct > 0 and direction_before == "OVERLEVERAGED_SHORT") or
                        (price_change_pct < 0 and direction_before == "OVERLEVERAGED_LONG")
                    )

                    # Only count as predicted if both CLRI was elevated AND direction was right
                    was_predicted = was_predicted and direction_correct

                    # Send validation alert
                    await self.alerter.send_price_validation_alert(
                        symbol=symbol,
                        price_change_pct=price_change_pct,
                        clri_before=clri_before,
                        clri_after=clri_after,
                        direction_before=direction_before,
                        was_predicted=was_predicted,
                    )

                    logger.info(
                        f"Price validation: {symbol} {price_change_pct:+.2f}% - "
                        f"{'PREDICTED' if was_predicted else 'MISSED'}"
                    )

                    # Record validation
                    last_validated[symbol] = (now, newest[1])

                    # Save to database
                    try:
                        async with self.db.session() as session:
                            alert = Alert(
                                triggered_at=now,
                                symbol=symbol,
                                alert_type='PRICE_VALIDATION',
                                clri_score=clri_before,
                                risk_level=RiskLevel[current_reading.risk_level] if current_reading else RiskLevel.LOW,
                                direction=Direction[direction_before] if direction_before != "BALANCED" else Direction.BALANCED,
                                message=f"Price {price_change_pct:+.2f}% - {'PREDICTED' if was_predicted else 'MISSED'}",
                                discord_sent=True,
                            )
                            await save_alert(session, alert)
                    except Exception as e:
                        logger.error(f"Failed to save validation alert: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in price validation: {e}")

    async def _wait_for_shutdown(self):
        """Wait for shutdown signal"""
        loop = asyncio.get_event_loop()
        
        def handle_signal():
            logger.info("Received shutdown signal")
            self._shutdown_event.set()
        
        # Register signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, handle_signal)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass
        
        # Wait forever (until signal)
        await asyncio.Event().wait()
    
    def get_current_readings(self) -> dict[str, CLRIOutput]:
        """Get current CLRI readings (for API access)"""
        return self._latest_readings.copy()
    
    def get_market_summary(self) -> dict:
        """Get current market summary (for API access)"""
        if not self._latest_readings:
            return {'market_clri': 0, 'risk_level': 'LOW', 'weights': {}}

        readings = list(self._latest_readings.values())
        oi_data = {}
        if self.exchange_collector:
            oi_data = self.exchange_collector.get_current_oi()

        market_output = self.aggregate.calculate_market_clri(readings, oi_data)

        # Convert dataclass to dict for API compatibility
        return {
            'market_clri': market_output.market_clri,
            'risk_level': market_output.risk_level,
            'market_direction': market_output.market_direction,
            'symbols_elevated': market_output.symbols_elevated,
            'symbols_high': market_output.symbols_high,
            'weights': market_output.weights,
            'total_oi': market_output.total_oi,
            'top_contributors': market_output.top_contributors,
            'divergent_symbols': market_output.divergent_symbols,
            'individual_readings': market_output.individual_readings,
        }


async def main():
    """Main entry point"""
    # Load .env file
    from dotenv import load_dotenv
    load_dotenv()

    # Load configuration from environment
    database_url = os.getenv('DATABASE_URL', 'postgresql://localhost/clri')
    discord_webhook = os.getenv('DISCORD_WEBHOOK_URL')
    coinglass_key = os.getenv('COINGLASS_API_KEY')
    atlas_export = os.getenv('ATLAS_EXPORT_PATH')

    # Discord bot for message cleanup
    discord_bot_token = os.getenv('DISCORD_BOT_TOKEN')
    discord_channel_id_str = os.getenv('DISCORD_CHANNEL_ID')
    discord_channel_id = int(discord_channel_id_str) if discord_channel_id_str else None

    if not discord_webhook:
        logger.warning("DISCORD_WEBHOOK_URL not set, alerts will be disabled")

    if not coinglass_key:
        logger.warning("COINGLASS_API_KEY not set, using estimated liquidation data")

    if discord_bot_token and discord_channel_id:
        logger.info("Discord cleanup bot configured")
    else:
        logger.info("Discord cleanup bot not configured (DISCORD_BOT_TOKEN/DISCORD_CHANNEL_ID missing)")

    # Configure exchanges (can be overridden via env)
    exchanges_str = os.getenv('ENABLED_EXCHANGES', 'binanceusdm,bybit,okx')
    enabled_exchanges = [e.strip() for e in exchanges_str.split(',')]

    # Configure symbols (can be overridden via env)
    symbols_str = os.getenv('TRACKED_SYMBOLS', 'BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ARB')
    tracked_symbols = [s.strip() for s in symbols_str.split(',')]

    # Create and run worker
    worker = CLRIWorker(
        database_url=database_url,
        discord_webhook_url=discord_webhook,
        coinglass_api_key=coinglass_key,
        enabled_exchanges=enabled_exchanges,
        symbols=tracked_symbols,
        polling_interval=60,
        summary_interval=900,
        atlas_export_path=atlas_export,
        discord_bot_token=discord_bot_token,
        discord_channel_id=discord_channel_id,
    )

    await worker.start()


if __name__ == '__main__':
    asyncio.run(main())
