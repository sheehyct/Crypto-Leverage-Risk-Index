"""
Discord Webhook Alerter

Sends CLRI alerts to Discord channels via webhooks.
Simple, no bot required - just POST to the webhook URL.
"""

import asyncio
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
from enum import Enum

from core.clri import CLRIOutput, MarketCLRIOutput

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    INFO = 0x3498db      # Blue
    WARNING = 0xf39c12   # Orange
    ELEVATED = 0xe67e22  # Dark Orange
    HIGH = 0xe74c3c      # Red
    EXTREME = 0x9b59b6   # Purple (for the crazy stuff)


@dataclass
class DiscordEmbed:
    """Discord embed structure"""
    title: str
    description: str
    color: int
    fields: list[dict] = None
    footer: dict = None
    timestamp: str = None
    
    def to_dict(self) -> dict:
        embed = {
            'title': self.title,
            'description': self.description,
            'color': self.color,
        }
        if self.fields:
            embed['fields'] = self.fields
        if self.footer:
            embed['footer'] = self.footer
        if self.timestamp:
            embed['timestamp'] = self.timestamp
        return embed


class DiscordAlerter:
    """
    Sends alerts to Discord via webhook
    
    Features:
    - Rate limiting to avoid spam
    - Cooldown per symbol
    - Different embed colors by severity
    - Market summary alerts
    """
    
    def __init__(
        self,
        webhook_url: str,
        cooldown_seconds: int = 300,  # 5 minute cooldown per symbol
        rate_limit_per_minute: int = 10,
    ):
        self.webhook_url = webhook_url
        self.cooldown_seconds = cooldown_seconds
        self.rate_limit_per_minute = rate_limit_per_minute

        # Tracking
        self._last_alert_time: dict[str, datetime] = {}
        self._alerts_this_minute: int = 0
        self._minute_start: datetime = datetime.now(timezone.utc)

        # Client (httpx for better Windows DNS compatibility)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create httpx client"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        """Close the client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits"""
        now = datetime.now(timezone.utc)
        
        # Reset counter if minute has passed
        if (now - self._minute_start).total_seconds() >= 60:
            self._alerts_this_minute = 0
            self._minute_start = now
        
        return self._alerts_this_minute < self.rate_limit_per_minute
    
    def _check_cooldown(self, symbol: str) -> bool:
        """Check if symbol is still in cooldown"""
        if symbol not in self._last_alert_time:
            return True
        
        elapsed = (datetime.now(timezone.utc) - self._last_alert_time[symbol]).total_seconds()
        return elapsed >= self.cooldown_seconds
    
    def _get_alert_color(self, clri_score: int, risk_level: str) -> int:
        """Get embed color based on severity"""
        if clri_score >= 90 or risk_level == "EXTREME":
            return AlertLevel.EXTREME.value
        elif clri_score >= 80 or risk_level == "HIGH":
            return AlertLevel.HIGH.value
        elif clri_score >= 65 or risk_level == "ELEVATED":
            return AlertLevel.ELEVATED.value
        elif clri_score >= 45:
            return AlertLevel.WARNING.value
        else:
            return AlertLevel.INFO.value
    
    def _get_direction_indicator(self, direction: str) -> str:
        """Get text indicator for direction"""
        if direction in ("LONG_CROWDED", "OVERLEVERAGED_LONG"):
            return "[OL-LONG]"
        elif direction in ("SHORT_CROWDED", "OVERLEVERAGED_SHORT"):
            return "[OL-SHORT]"
        else:
            return "[BALANCED]"

    def _get_risk_indicator(self, risk_level: str) -> str:
        """Get text indicator for risk level"""
        indicators = {
            "LOW": "[LOW]",
            "MODERATE": "[MOD]",
            "ELEVATED": "[ELEV]",
            "HIGH": "[HIGH]",
            "EXTREME": "[EXTREME]",
        }
        return indicators.get(risk_level, "[?]")
    
    async def send_clri_alert(self, output: CLRIOutput) -> bool:
        """
        Send a CLRI alert to Discord
        
        Returns True if sent, False if rate limited or cooldown
        """
        # Check limits
        if not self._check_rate_limit():
            logger.warning("Rate limit reached, skipping alert")
            return False
        
        if not self._check_cooldown(output.symbol):
            logger.debug(f"{output.symbol} still in cooldown")
            return False
        
        # Build embed
        direction_ind = self._get_direction_indicator(output.direction)
        risk_ind = self._get_risk_indicator(output.risk_level)

        title = f"{risk_ind} CLRI Alert: {output.symbol}"

        description = output.alert_message or f"CLRI Score: {output.clri_score}/100"

        fields = [
            {
                'name': 'CLRI Score',
                'value': f"**{output.clri_score}**/100",
                'inline': True,
            },
            {
                'name': f'{direction_ind} Direction',
                'value': output.direction.replace('_', ' '),
                'inline': True,
            },
            {
                'name': 'Risk Level',
                'value': output.risk_level,
                'inline': True,
            },
            {
                'name': 'Component Scores',
                'value': (
                    f"Funding: {output.funding_score:.0f}\n"
                    f"OI: {output.oi_score:.0f}\n"
                    f"Volume: {output.volume_score:.0f}\n"
                    f"Liquidations: {output.liquidation_score:.0f}\n"
                    f"Imbalance: {output.imbalance_score:.0f}"
                ),
                'inline': False,
            },
        ]
        
        embed = DiscordEmbed(
            title=title,
            description=description,
            color=self._get_alert_color(output.clri_score, output.risk_level),
            fields=fields,
            footer={'text': 'Crypto Leverage Risk Index'},
            timestamp=output.timestamp.isoformat(),
        )
        
        # Send
        success = await self._send_webhook({'embeds': [embed.to_dict()]})
        
        if success:
            self._last_alert_time[output.symbol] = datetime.now(timezone.utc)
            self._alerts_this_minute += 1
        
        return success
    
    async def send_market_summary(self, market_data: dict, readings: list[CLRIOutput]) -> bool:
        """
        Send a market-wide summary alert
        """
        if not self._check_rate_limit():
            return False
        
        market_clri = market_data.get('market_clri', 0)
        risk_level = market_data.get('risk_level', 'LOW')
        direction = market_data.get('market_direction', 'MIXED')
        
        risk_ind = self._get_risk_indicator(risk_level)
        direction_ind = self._get_direction_indicator(direction)

        title = f"{risk_ind} Market CLRI Summary"

        # Build individual readings summary
        readings_text = "\n".join([
            f"{self._get_risk_indicator(r.risk_level)} **{r.symbol}**: {r.clri_score} ({r.direction.replace('_', ' ')})"
            for r in sorted(readings, key=lambda x: x.clri_score, reverse=True)[:10]
        ])

        fields = [
            {
                'name': 'Market CLRI',
                'value': f"**{market_clri}**/100",
                'inline': True,
            },
            {
                'name': f'{direction_ind} Market Bias',
                'value': direction.replace('_', ' '),
                'inline': True,
            },
            {
                'name': 'Elevated Count',
                'value': f"{market_data.get('symbols_elevated', 0)} symbols",
                'inline': True,
            },
            {
                'name': 'Top Readings',
                'value': readings_text or "No data",
                'inline': False,
            },
        ]
        
        embed = DiscordEmbed(
            title=title,
            description=f"Overall market leverage risk: **{risk_level}**",
            color=self._get_alert_color(market_clri, risk_level),
            fields=fields,
            footer={'text': 'Market Summary - Updated every 15 minutes'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        return await self._send_webhook({'embeds': [embed.to_dict()]})

    async def send_market_clri_alert(
        self,
        market_output: MarketCLRIOutput,
        alert_type: str = "THRESHOLD",  # THRESHOLD or SPIKE
        previous_clri: int = 0,
    ) -> bool:
        """
        Send Market CLRI alert (Layer 1 - primary alert type).

        This is the main alert channel - one alert covers the whole market.
        Replaces individual symbol threshold alerts.

        Args:
            market_output: MarketCLRIOutput from AggregateCLRI
            alert_type: "THRESHOLD" for crossing 65/80/90, "SPIKE" for rapid change
            previous_clri: Previous market CLRI (for spike alerts)
        """
        if not self._check_rate_limit():
            return False

        market_clri = market_output.market_clri
        risk_level = market_output.risk_level
        direction = market_output.market_direction

        risk_ind = self._get_risk_indicator(risk_level)
        direction_ind = self._get_direction_indicator(direction)

        # Build title based on alert type
        if alert_type == "SPIKE":
            change = market_clri - previous_clri
            title = f"[MARKET SPIKE] {previous_clri} -> {market_clri} ({change:+d} pts)"
        else:
            title = f"{risk_ind} MARKET CLRI: {market_clri} - {risk_level} RISK"

        # Format top contributors
        contributors_text = ""
        for symbol, weight, clri_score in market_output.top_contributors:
            contributors_text += f"**{symbol}** ({weight*100:.0f}% wt): CLRI {clri_score}\n"

        # Format current weights for context
        weights_text = ", ".join([
            f"{s}: {w*100:.0f}%"
            for s, w in sorted(market_output.weights.items(), key=lambda x: -x[1])[:5]
        ])

        fields = [
            {
                'name': 'Market CLRI',
                'value': f"**{market_clri}**/100",
                'inline': True,
            },
            {
                'name': f'{direction_ind} Direction',
                'value': direction.replace('_', ' '),
                'inline': True,
            },
            {
                'name': 'Risk Level',
                'value': risk_level,
                'inline': True,
            },
            {
                'name': 'Top Contributors',
                'value': contributors_text or "No data",
                'inline': False,
            },
            {
                'name': 'OI Weights',
                'value': weights_text or "N/A",
                'inline': False,
            },
        ]

        # Add elevated/high counts
        if market_output.symbols_elevated > 0 or market_output.symbols_high > 0:
            fields.append({
                'name': 'Symbol Counts',
                'value': f"Elevated (65+): {market_output.symbols_elevated} | High (80+): {market_output.symbols_high}",
                'inline': False,
            })

        embed = DiscordEmbed(
            title=title,
            description=f"Market-wide leverage risk is **{risk_level}**",
            color=self._get_alert_color(market_clri, risk_level),
            fields=fields,
            footer={'text': 'Market CLRI Alert - OI Weighted'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        success = await self._send_webhook({'embeds': [embed.to_dict()]})

        if success:
            self._last_alert_time['_MARKET_'] = datetime.now(timezone.utc)
            self._alerts_this_minute += 1

        return success

    async def send_divergence_alert(
        self,
        symbol: str,
        symbol_clri: int,
        market_clri: int,
        divergence: int,
        direction: str,
        risk_level: str,
    ) -> bool:
        """
        Send Divergence Alert (Layer 2 - rare, high-value).

        Only fires when a single symbol's CLRI is 25+ points above market.
        This catches cases where one coin has unusual leverage building independently.

        Args:
            symbol: The divergent symbol
            symbol_clri: Symbol's individual CLRI score
            market_clri: Current market CLRI
            divergence: symbol_clri - market_clri (should be >= 25)
            direction: Symbol's direction bias
            risk_level: Symbol's risk level
        """
        if not self._check_rate_limit():
            return False

        # Use symbol cooldown for divergence alerts
        cooldown_key = f"DIV_{symbol}"
        if not self._check_cooldown(cooldown_key):
            logger.debug(f"{symbol} divergence still in cooldown")
            return False

        direction_ind = self._get_direction_indicator(direction)

        embed = DiscordEmbed(
            title=f"[DIVERGENCE] {symbol} CLRI {symbol_clri} vs Market {market_clri}",
            description=f"**{symbol}** is **+{divergence} pts** above market - independent leverage buildup detected",
            color=self._get_alert_color(symbol_clri, risk_level),
            fields=[
                {
                    'name': 'Symbol CLRI',
                    'value': f"**{symbol_clri}**/100",
                    'inline': True,
                },
                {
                    'name': 'Market CLRI',
                    'value': str(market_clri),
                    'inline': True,
                },
                {
                    'name': 'Divergence',
                    'value': f"+{divergence} pts",
                    'inline': True,
                },
                {
                    'name': f'{direction_ind} Direction',
                    'value': direction.replace('_', ' '),
                    'inline': True,
                },
                {
                    'name': 'Risk Level',
                    'value': risk_level,
                    'inline': True,
                },
            ],
            footer={'text': 'Divergence Alert - Independent Leverage Buildup'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        success = await self._send_webhook({'embeds': [embed.to_dict()]})

        if success:
            self._last_alert_time[cooldown_key] = datetime.now(timezone.utc)
            self._alerts_this_minute += 1

        return success

    async def send_liquidation_event_alert(
        self,
        symbol: str,
        total_1h_usd: float,
        long_1h_usd: float,
        short_1h_usd: float,
        current_clri: int,
        risk_level: str,
        direction: str,
    ) -> bool:
        """
        Alert Type 1: Liquidation Event Alert
        Triggered when large $ liquidations occur (provides context for CLRI changes)
        """
        # Determine which side is getting liquidated more
        if long_1h_usd > short_1h_usd * 1.5:
            bias = "LONGS LIQUIDATED"
            indicator = "[!] LONG LIQUIDATIONS"
            color = AlertLevel.HIGH.value
        elif short_1h_usd > long_1h_usd * 1.5:
            bias = "SHORTS LIQUIDATED"
            indicator = "[!] SHORT LIQUIDATIONS"
            color = AlertLevel.ELEVATED.value
        else:
            bias = "MIXED LIQUIDATIONS"
            indicator = "[!] LIQUIDATION EVENT"
            color = AlertLevel.WARNING.value

        embed = DiscordEmbed(
            title=f"{indicator}: {symbol}",
            description=f"**${total_1h_usd:,.0f}** liquidated in the last hour",
            color=color,
            fields=[
                {
                    'name': 'Long Liquidations',
                    'value': f"${long_1h_usd:,.0f}",
                    'inline': True,
                },
                {
                    'name': 'Short Liquidations',
                    'value': f"${short_1h_usd:,.0f}",
                    'inline': True,
                },
                {
                    'name': 'Bias',
                    'value': bias,
                    'inline': True,
                },
                {
                    'name': 'Current CLRI',
                    'value': f"{current_clri}/100",
                    'inline': True,
                },
                {
                    'name': 'Risk Level',
                    'value': risk_level,
                    'inline': True,
                },
                {
                    'name': 'Direction',
                    'value': direction.replace('_', ' '),
                    'inline': True,
                },
            ],
            footer={'text': 'Liquidation Event Alert'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return await self._send_webhook({'embeds': [embed.to_dict()]})

    async def send_clri_spike_alert(
        self,
        symbol: str,
        clri_before: int,
        clri_after: int,
        change_points: int,
        time_window_seconds: int,
        direction: str,
        risk_level: str,
    ) -> bool:
        """
        Alert Type 2: CLRI Spike Alert
        Triggered when CLRI increases rapidly (e.g., +15 points in 5 minutes)
        """
        indicator = "[CLRI SPIKE]" if change_points > 0 else "[CLRI DROP]"

        # Color based on the new CLRI level
        color = self._get_alert_color(clri_after, risk_level)

        embed = DiscordEmbed(
            title=f"{indicator} {symbol}: {clri_before} -> {clri_after}",
            description=f"CLRI changed by **{change_points:+d} points** in {time_window_seconds}s",
            color=color,
            fields=[
                {
                    'name': 'CLRI Before',
                    'value': str(clri_before),
                    'inline': True,
                },
                {
                    'name': 'CLRI After',
                    'value': str(clri_after),
                    'inline': True,
                },
                {
                    'name': 'Change',
                    'value': f"{change_points:+d} pts",
                    'inline': True,
                },
                {
                    'name': 'Direction',
                    'value': direction.replace('_', ' '),
                    'inline': True,
                },
                {
                    'name': 'Risk Level',
                    'value': risk_level,
                    'inline': True,
                },
            ],
            footer={'text': 'CLRI Spike Alert'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return await self._send_webhook({'embeds': [embed.to_dict()]})

    async def send_price_validation_alert(
        self,
        symbol: str,
        price_change_pct: float,
        clri_before: int,
        clri_after: int,
        direction_before: str,
        was_predicted: bool,
    ) -> bool:
        """
        Alert Type 3: Price Validation Alert (Post-hoc)
        Sent after a significant price move to validate CLRI predictions
        """
        if was_predicted:
            indicator = "[VALIDATED]"
            description = f"CLRI correctly predicted the **{price_change_pct:+.2f}%** move!"
            color = AlertLevel.INFO.value
        else:
            indicator = "[MISSED]"
            description = f"**{price_change_pct:+.2f}%** move occurred without elevated CLRI"
            color = AlertLevel.WARNING.value

        embed = DiscordEmbed(
            title=f"{indicator} Price Move: {symbol}",
            description=description,
            color=color,
            fields=[
                {
                    'name': 'Price Change',
                    'value': f"{price_change_pct:+.2f}%",
                    'inline': True,
                },
                {
                    'name': 'CLRI Before Move',
                    'value': str(clri_before),
                    'inline': True,
                },
                {
                    'name': 'CLRI After Move',
                    'value': str(clri_after),
                    'inline': True,
                },
                {
                    'name': 'Direction Signal',
                    'value': direction_before.replace('_', ' '),
                    'inline': True,
                },
                {
                    'name': 'Prediction',
                    'value': "CORRECT" if was_predicted else "MISSED",
                    'inline': True,
                },
            ],
            footer={'text': 'Price Validation - Model Learning'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return await self._send_webhook({'embeds': [embed.to_dict()]})
    
    async def send_test_message(self) -> bool:
        """Send a test message to verify webhook is working"""
        embed = DiscordEmbed(
            title="CLRI Bot Connected",
            description="Crypto Leverage Risk Index alerts are now active.",
            color=AlertLevel.INFO.value,
            fields=[
                {
                    'name': 'What is CLRI?',
                    'value': (
                        "CLRI monitors funding rates, open interest, and leverage "
                        "to detect elevated risk of volatile price moves."
                    ),
                    'inline': False,
                },
                {
                    'name': 'Alert Thresholds',
                    'value': (
                        "LOW: 0-25\n"
                        "MODERATE: 26-45\n"
                        "ELEVATED: 46-65\n"
                        "HIGH: 66-80\n"
                        "EXTREME: 81-100"
                    ),
                    'inline': False,
                },
            ],
            footer={'text': 'CLRI Bot v1.0'},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        return await self._send_webhook({'embeds': [embed.to_dict()]})
    
    async def _send_webhook(self, payload: dict) -> bool:
        """Send payload to Discord webhook"""
        try:
            client = await self._get_client()

            response = await client.post(
                self.webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
            )
            if response.status_code == 204:
                logger.info("Discord alert sent successfully")
                return True
            elif response.status_code == 429:
                # Rate limited by Discord
                data = response.json()
                retry_after = data.get('retry_after', 5)
                logger.warning(f"Discord rate limited, retry after {retry_after}s")
                await asyncio.sleep(retry_after)
                return False
            else:
                text = response.text
                logger.error(f"Discord webhook failed: {response.status_code} - {text}")
                return False

        except Exception as e:
            logger.error(f"Discord webhook error: {e}")
            return False


async def test_alerter():
    """Test Discord alerter"""
    import os
    
    webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        print("Set DISCORD_WEBHOOK_URL environment variable")
        return
    
    alerter = DiscordAlerter(webhook_url)
    
    try:
        # Send test message
        await alerter.send_test_message()
        
        # Send fake alert
        fake_output = CLRIOutput(
            symbol='BTC',
            timestamp=datetime.now(timezone.utc),
            funding_score=75,
            oi_score=80,
            volume_score=60,
            liquidation_score=70,
            imbalance_score=55,
            clri_score=72,
            direction='OVERLEVERAGED_LONG',
            direction_confidence=0.7,
            risk_level='HIGH',
            should_alert=True,
            alert_message='[ALERT] BTC CLRI crossed HIGH threshold: 72/100 (OVERLEVERAGED_LONG)',
        )
        
        await alerter.send_clri_alert(fake_output)
        
    finally:
        await alerter.close()


if __name__ == '__main__':
    asyncio.run(test_alerter())
