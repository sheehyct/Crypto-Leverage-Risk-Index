"""
Crypto Leverage Risk Index (CLRI) - Core Calculation Engine

Computes a composite risk score (0-100) based on:
- Funding rates (deviation from normal)
- Open interest (absolute + rate of change)
- Volume ratios (perp vs spot)
- Liquidation activity
- Order book imbalance

Higher scores = higher probability of violent price moves (either direction)
"""

from dataclasses import dataclass
from typing import Optional, Literal
from datetime import datetime, timedelta
import numpy as np
from collections import deque
import statistics


@dataclass
class CLRIInput:
    """Raw data inputs for CLRI calculation"""
    symbol: str
    timestamp: datetime

    # Funding
    funding_rate: float  # Current funding rate (e.g., 0.0001 = 0.01%)
    predicted_funding: Optional[float] = None

    # Open Interest
    open_interest: float = 0  # Total OI in USD
    oi_change_1h_pct: float = 0  # Percent change in OI over 1 hour
    oi_change_24h_pct: float = 0

    # Volume
    perp_volume_24h: float = 0
    spot_volume_24h: float = 0

    # Positioning (if available)
    long_short_ratio: Optional[float] = None  # >1 = more longs

    # Liquidations
    liquidations_1h_usd: float = 0
    long_liquidations_1h: float = 0
    short_liquidations_1h: float = 0

    # Order book
    bid_depth_usd: float = 0  # Total bid liquidity within X%
    ask_depth_usd: float = 0  # Total ask liquidity within X%

    # Price
    price: float = 0  # Current perpetual/futures price
    spot_price: float = 0  # Spot price for basis calculation
    price_change_1h_pct: float = 0


@dataclass
class MarketRegime:
    """Detected market regime for dynamic weighting"""
    regime_type: Literal["NORMAL", "LIQUIDATION_CASCADE", "FUNDING_STRESS", "OI_SPIKE"]
    intensity: float  # 0.0 to 1.0 (how strongly in this regime)
    trigger_components: list  # Which components triggered this regime


class DynamicWeightCalculator:
    """
    Calculates dynamic weights based on market regime detection.

    Adjusts component weights when specific market conditions are detected:
    - LIQUIDATION_CASCADE: Increase liquidation weight when mass liquidations occur
    - FUNDING_STRESS: Increase funding weight during extreme funding rate periods
    - OI_SPIKE: Increase OI weight when open interest is spiking rapidly
    """

    BASE_WEIGHTS = {
        'funding': 0.25,
        'oi': 0.25,
        'volume': 0.15,
        'liquidation': 0.20,
        'imbalance': 0.15,
    }

    REGIME_WEIGHTS = {
        'LIQUIDATION_CASCADE': {
            'funding': 0.20, 'oi': 0.20, 'volume': 0.10,
            'liquidation': 0.35, 'imbalance': 0.15,
        },
        'FUNDING_STRESS': {
            'funding': 0.35, 'oi': 0.20, 'volume': 0.10,
            'liquidation': 0.20, 'imbalance': 0.15,
        },
        'OI_SPIKE': {
            'funding': 0.20, 'oi': 0.35, 'volume': 0.15,
            'liquidation': 0.20, 'imbalance': 0.10,
        },
    }

    # Thresholds for regime detection (z-scores)
    LIQUIDATION_CASCADE_THRESHOLD = 2.0
    FUNDING_STRESS_THRESHOLD = 2.0
    OI_SPIKE_THRESHOLD = 2.5

    def detect_regime(
        self,
        funding_zscore: float,
        oi_zscore: float,
        liquidation_zscore: float,
    ) -> MarketRegime:
        """
        Detect current market regime from component z-scores.

        Priority order: Liquidation > Funding > OI > Normal
        """
        # Check liquidation cascade first (highest priority)
        if liquidation_zscore >= self.LIQUIDATION_CASCADE_THRESHOLD:
            intensity = min(1.0, (liquidation_zscore - 1.0) / 2.0)
            return MarketRegime("LIQUIDATION_CASCADE", intensity, ["liquidation"])

        # Check funding stress
        if abs(funding_zscore) >= self.FUNDING_STRESS_THRESHOLD:
            intensity = min(1.0, (abs(funding_zscore) - 1.0) / 2.0)
            return MarketRegime("FUNDING_STRESS", intensity, ["funding"])

        # Check OI spike
        if oi_zscore >= self.OI_SPIKE_THRESHOLD:
            intensity = min(1.0, (oi_zscore - 1.5) / 2.0)
            return MarketRegime("OI_SPIKE", intensity, ["oi"])

        return MarketRegime("NORMAL", 0.0, [])

    def get_weights(self, regime: MarketRegime) -> dict:
        """Get blended weights for current regime"""
        if regime.regime_type == "NORMAL":
            return self.BASE_WEIGHTS.copy()

        regime_weights = self.REGIME_WEIGHTS[regime.regime_type]
        return self._blend(self.BASE_WEIGHTS, regime_weights, regime.intensity)

    def _blend(self, base: dict, target: dict, intensity: float) -> dict:
        """Smoothly blend between base and target weights"""
        return {k: base[k] * (1 - intensity) + target[k] * intensity for k in base}


@dataclass
class CLRIOutput:
    """Computed CLRI metrics"""
    symbol: str
    timestamp: datetime

    # Component scores (0-100 each)
    funding_score: float
    oi_score: float
    volume_score: float
    liquidation_score: float
    imbalance_score: float

    # Composite
    clri_score: int  # 0-100

    # Direction bias
    direction: Literal["OVERLEVERAGED_LONG", "OVERLEVERAGED_SHORT", "BALANCED"]
    direction_confidence: float  # 0-1

    # Risk level
    risk_level: Literal["LOW", "MODERATE", "ELEVATED", "HIGH", "EXTREME"]

    # Alert
    should_alert: bool
    alert_message: Optional[str] = None

    # High-value signals (for dashboard/analysis)
    basis_pct: float = 0.0  # (Futures - Spot) / Spot * 100, contango > 0
    liquidation_oi_ratio: float = 0.0  # Liquidations / OI, % of OI liquidated
    funding_oi_stress: float = 0.0  # |Funding Rate| * OI, $ at risk from funding
    oi_price_divergence: float = 0.0  # OI change - price change, new leverage indicator

    # Composite stress score (0-100, combines high-value signals)
    composite_stress: float = 0.0
    stress_level: Literal["LOW", "MODERATE", "ELEVATED", "HIGH", "EXTREME"] = "LOW"

    # Dynamic weighting info
    detected_regime: str = "NORMAL"
    regime_intensity: float = 0.0
    effective_weights: Optional[dict] = None


class CLRICalculator:
    """
    Calculates the Crypto Leverage Risk Index
    
    Maintains rolling history for z-score calculations
    """
    
    # Weights for composite score
    WEIGHTS = {
        'funding': 0.25,
        'oi': 0.25,
        'volume': 0.15,
        'liquidation': 0.20,
        'imbalance': 0.15,
    }
    
    # Thresholds
    FUNDING_EXTREME_THRESHOLD = 0.001  # 0.1% per 8h = ~10% annualized
    OI_SPIKE_THRESHOLD = 0.10  # 10% change in 1 hour
    LIQUIDATION_SPIKE_THRESHOLD = 10_000_000  # $10M in 1 hour
    
    # Risk levels
    RISK_THRESHOLDS = {
        'LOW': 25,
        'MODERATE': 45,
        'ELEVATED': 65,
        'HIGH': 80,
        'EXTREME': 100,
    }
    
    # Alert threshold
    ALERT_THRESHOLD = 65
    
    def __init__(self, history_window: int = 1000):
        """
        Args:
            history_window: Number of readings to keep for z-score calculations
        """
        self.history_window = history_window

        # Rolling history per symbol (for z-score normalization)
        self._funding_history: dict[str, deque] = {}
        self._oi_history: dict[str, deque] = {}
        self._volume_ratio_history: dict[str, deque] = {}
        self._liquidation_history: dict[str, deque] = {}

        # Stress signal history per symbol (for composite stress percentile)
        self._basis_history: dict[str, deque] = {}
        self._liq_oi_history: dict[str, deque] = {}
        self._funding_stress_history: dict[str, deque] = {}
        self._divergence_history: dict[str, deque] = {}

        # Track z-scores for regime detection
        self._latest_zscores: dict[str, dict] = {}

        # Previous scores for delta tracking
        self._previous_scores: dict[str, int] = {}

        # Dynamic weight calculator
        self._weight_calculator = DynamicWeightCalculator()
    
    def _get_or_create_history(self, symbol: str) -> None:
        """Initialize history deques for a symbol if needed"""
        if symbol not in self._funding_history:
            self._funding_history[symbol] = deque(maxlen=self.history_window)
            self._oi_history[symbol] = deque(maxlen=self.history_window)
            self._volume_ratio_history[symbol] = deque(maxlen=self.history_window)
            self._liquidation_history[symbol] = deque(maxlen=self.history_window)
            # Stress signal histories
            self._basis_history[symbol] = deque(maxlen=self.history_window)
            self._liq_oi_history[symbol] = deque(maxlen=self.history_window)
            self._funding_stress_history[symbol] = deque(maxlen=self.history_window)
            self._divergence_history[symbol] = deque(maxlen=self.history_window)
            # Z-score tracking
            self._latest_zscores[symbol] = {'funding': 0.0, 'oi': 0.0, 'liquidation': 0.0}

    def load_history(
        self,
        symbol: str,
        funding_rates: list[float],
        open_interests: list[float],
        volume_ratios: list[float],
        liquidations: list[float],
        previous_score: int = 0,
    ) -> None:
        """
        Load historical data to initialize z-score calculations.
        Call this on startup to restore state from database.

        Args:
            symbol: Symbol to load history for
            funding_rates: List of historical funding rates (oldest first)
            open_interests: List of historical OI values
            volume_ratios: List of historical perp/spot volume ratios
            liquidations: List of historical liquidation amounts
            previous_score: Last known CLRI score
        """
        self._get_or_create_history(symbol)

        # Load each history type
        for rate in funding_rates:
            self._funding_history[symbol].append(rate)

        for oi in open_interests:
            self._oi_history[symbol].append(oi)

        for ratio in volume_ratios:
            self._volume_ratio_history[symbol].append(ratio)

        for liq in liquidations:
            self._liquidation_history[symbol].append(liq)

        # Set previous score to avoid false alerts on first calculation
        if previous_score > 0:
            self._previous_scores[symbol] = previous_score
    
    def _zscore(self, value: float, history: deque) -> float:
        """Calculate z-score of value against history"""
        if len(history) < 10:
            return 0.0
        
        mean = statistics.mean(history)
        stdev = statistics.stdev(history) if len(history) > 1 else 1
        
        if stdev == 0:
            return 0.0
        
        return (value - mean) / stdev
    
    def _percentile(self, value: float, history: deque) -> float:
        """Calculate percentile rank of value in history (0-100)"""
        if len(history) < 10:
            return 50.0
        
        sorted_history = sorted(history)
        count_below = sum(1 for x in sorted_history if x < value)
        return (count_below / len(sorted_history)) * 100
    
    def _score_to_range(self, zscore: float, max_zscore: float = 3.0) -> float:
        """Convert z-score to 0-100 range"""
        # Clip to reasonable range
        clipped = max(-max_zscore, min(max_zscore, abs(zscore)))
        return (clipped / max_zscore) * 100

    def _calculate_composite_stress(
        self,
        symbol: str,
        basis_pct: float,
        liq_oi_ratio: float,
        funding_stress: float,
        oi_divergence: float,
    ) -> tuple[float, str]:
        """
        Calculate composite stress score from high-value signals.

        Uses percentile normalization against history, then weighted combination.

        Returns:
            (composite_stress 0-100, stress_level)
        """
        self._get_or_create_history(symbol)

        # Add to histories (use absolute values for some metrics)
        self._basis_history[symbol].append(abs(basis_pct))
        self._liq_oi_history[symbol].append(liq_oi_ratio)
        self._funding_stress_history[symbol].append(funding_stress)
        self._divergence_history[symbol].append(abs(oi_divergence))

        # Calculate component scores via percentile ranking
        basis_score = self._percentile(abs(basis_pct), self._basis_history[symbol])
        liq_score = self._percentile(liq_oi_ratio, self._liq_oi_history[symbol])
        funding_score = self._percentile(funding_stress, self._funding_stress_history[symbol])
        divergence_score = self._percentile(abs(oi_divergence), self._divergence_history[symbol])

        # Weighted combination
        # Weights based on hypothesized predictive power
        composite = (
            basis_score * 0.20 +       # Basis extremes often precede moves
            liq_score * 0.30 +         # Liquidation % of OI is highly predictive
            funding_score * 0.25 +     # $ at risk from funding
            divergence_score * 0.25    # OI/price divergence = hidden leverage
        )

        composite = min(100, max(0, composite))

        # Determine stress level
        if composite >= 80:
            level = "EXTREME"
        elif composite >= 65:
            level = "HIGH"
        elif composite >= 45:
            level = "ELEVATED"
        elif composite >= 25:
            level = "MODERATE"
        else:
            level = "LOW"

        return composite, level

    def calculate_funding_score(self, data: CLRIInput) -> tuple[float, float]:
        """
        Calculate funding rate score

        Returns:
            (score 0-100, direction_signal -1 to 1)
        """
        self._get_or_create_history(data.symbol)
        history = self._funding_history[data.symbol]

        # Add to history
        history.append(data.funding_rate)

        # Absolute funding extremity
        abs_funding = abs(data.funding_rate)

        # Z-score against history
        zscore = self._zscore(abs_funding, [abs(x) for x in history])

        # Store z-score for regime detection
        self._latest_zscores[data.symbol]['funding'] = zscore

        # Also check against absolute threshold
        threshold_score = min(100, (abs_funding / self.FUNDING_EXTREME_THRESHOLD) * 100)

        # Combined score (weight both)
        score = (self._score_to_range(zscore) * 0.6) + (threshold_score * 0.4)

        # Direction signal: positive funding = longs paying shorts = long crowded
        direction_signal = 1.0 if data.funding_rate > 0 else -1.0
        direction_signal *= min(1.0, abs_funding / self.FUNDING_EXTREME_THRESHOLD)

        return min(100, score), direction_signal
    
    def calculate_oi_score(self, data: CLRIInput) -> float:
        """
        Calculate open interest score based on:
        - Rate of change (spikes indicate new leverage entering)
        - Percentile vs history
        """
        self._get_or_create_history(data.symbol)
        history = self._oi_history[data.symbol]

        # Add to history
        history.append(data.open_interest)

        # Z-score of OI change rate for regime detection
        oi_change_zscore = self._zscore(abs(data.oi_change_1h_pct), history)
        self._latest_zscores[data.symbol]['oi'] = oi_change_zscore

        # Score based on rate of change
        oi_change_score = min(100, (abs(data.oi_change_1h_pct) / self.OI_SPIKE_THRESHOLD) * 100)

        # Score based on absolute level (percentile)
        percentile_score = self._percentile(data.open_interest, history)

        # Combined: weight rapid changes higher
        score = (oi_change_score * 0.6) + (percentile_score * 0.4)

        return min(100, score)
    
    def calculate_volume_score(self, data: CLRIInput) -> float:
        """
        Calculate volume ratio score
        
        High perp/spot ratio = more speculative activity = higher risk
        """
        self._get_or_create_history(data.symbol)
        history = self._volume_ratio_history[data.symbol]
        
        # Calculate ratio
        if data.spot_volume_24h > 0:
            ratio = data.perp_volume_24h / data.spot_volume_24h
        else:
            ratio = 1.0
        
        # Add to history
        history.append(ratio)
        
        # Z-score
        zscore = self._zscore(ratio, history)
        
        return self._score_to_range(zscore)
    
    def calculate_liquidation_score(self, data: CLRIInput) -> tuple[float, float]:
        """
        Calculate liquidation activity score

        Returns:
            (score 0-100, direction_signal -1 to 1)
        """
        self._get_or_create_history(data.symbol)
        history = self._liquidation_history[data.symbol]

        total_liqs = data.liquidations_1h_usd

        # Add to history
        history.append(total_liqs)

        # Score based on absolute threshold
        threshold_score = min(100, (total_liqs / self.LIQUIDATION_SPIKE_THRESHOLD) * 100)

        # Z-score
        zscore = self._zscore(total_liqs, history)
        zscore_score = self._score_to_range(zscore)

        # Store z-score for regime detection
        self._latest_zscores[data.symbol]['liquidation'] = zscore

        # Combined
        score = (threshold_score * 0.5) + (zscore_score * 0.5)

        # Direction: which side is getting liquidated?
        if data.long_liquidations_1h + data.short_liquidations_1h > 0:
            long_pct = data.long_liquidations_1h / (data.long_liquidations_1h + data.short_liquidations_1h)
            # More long liquidations = shorts winning = short-biased
            direction_signal = -1.0 * (long_pct - 0.5) * 2  # Scale to -1 to 1
        else:
            direction_signal = 0.0

        return min(100, score), direction_signal
    
    def calculate_imbalance_score(self, data: CLRIInput) -> tuple[float, float]:
        """
        Calculate order book imbalance score
        
        Returns:
            (score 0-100, direction_signal -1 to 1)
        """
        total_depth = data.bid_depth_usd + data.ask_depth_usd
        
        if total_depth == 0:
            return 0.0, 0.0
        
        # Imbalance ratio
        imbalance = (data.bid_depth_usd - data.ask_depth_usd) / total_depth
        
        # Score based on how extreme the imbalance is
        score = abs(imbalance) * 100
        
        # Direction: more bids = bullish support, more asks = bearish pressure
        direction_signal = imbalance
        
        return min(100, score), direction_signal
    
    def calculate(self, data: CLRIInput) -> CLRIOutput:
        """
        Calculate complete CLRI output with dynamic weighting and composite stress.
        """
        # Calculate component scores (these also track z-scores for regime detection)
        funding_score, funding_direction = self.calculate_funding_score(data)
        oi_score = self.calculate_oi_score(data)
        volume_score = self.calculate_volume_score(data)
        liquidation_score, liq_direction = self.calculate_liquidation_score(data)
        imbalance_score, imbalance_direction = self.calculate_imbalance_score(data)

        # Detect market regime from tracked z-scores
        zscores = self._latest_zscores.get(data.symbol, {'funding': 0, 'oi': 0, 'liquidation': 0})
        regime = self._weight_calculator.detect_regime(
            funding_zscore=zscores['funding'],
            oi_zscore=zscores['oi'],
            liquidation_zscore=zscores['liquidation'],
        )

        # Get dynamic weights based on detected regime
        weights = self._weight_calculator.get_weights(regime)

        # Composite score using DYNAMIC weights
        composite = (
            funding_score * weights['funding'] +
            oi_score * weights['oi'] +
            volume_score * weights['volume'] +
            liquidation_score * weights['liquidation'] +
            imbalance_score * weights['imbalance']
        )

        clri_score = int(min(100, max(0, composite)))

        # Direction calculation
        direction_signals = [
            (funding_direction, 0.4),
            (liq_direction, 0.3),
            (imbalance_direction, 0.3),
        ]

        weighted_direction = sum(sig * weight for sig, weight in direction_signals)

        if weighted_direction > 0.2:
            direction = "OVERLEVERAGED_LONG"
        elif weighted_direction < -0.2:
            direction = "OVERLEVERAGED_SHORT"
        else:
            direction = "BALANCED"

        direction_confidence = min(1.0, abs(weighted_direction))

        # Risk level
        risk_level = "LOW"
        for level, threshold in self.RISK_THRESHOLDS.items():
            if clri_score <= threshold:
                risk_level = level
                break

        # Alert logic
        prev_score = self._previous_scores.get(data.symbol, 0)
        self._previous_scores[data.symbol] = clri_score

        should_alert = False
        alert_message = None

        # Alert if crossing threshold upward, or extreme reading
        if clri_score >= self.ALERT_THRESHOLD and prev_score < self.ALERT_THRESHOLD:
            should_alert = True
            alert_message = f"[ELEVATED] {data.symbol} CLRI crossed ELEVATED threshold: {clri_score}/100 ({direction})"
        elif clri_score >= 80 and prev_score < 80:
            should_alert = True
            alert_message = f"[HIGH] {data.symbol} CLRI crossed HIGH threshold: {clri_score}/100 ({direction})"
        elif clri_score >= 90:
            should_alert = True
            alert_message = f"[EXTREME] {data.symbol} CLRI EXTREME: {clri_score}/100 ({direction}) - High probability of violent move!"

        # Calculate high-value signals
        # 1. Basis: (Futures - Spot) / Spot * 100
        # Positive = contango (bullish leverage), Negative = backwardation (bearish)
        basis_pct = 0.0
        if data.spot_price > 0 and data.price > 0:
            basis_pct = ((data.price - data.spot_price) / data.spot_price) * 100

        # 2. Liquidation/OI Ratio: What % of OI got liquidated
        # Higher = more significant liquidation event
        liquidation_oi_ratio = 0.0
        if data.open_interest > 0:
            liquidation_oi_ratio = (data.liquidations_1h_usd / data.open_interest) * 100

        # 3. Funding OI Stress: |Funding Rate| * OI
        # $ amount at risk from funding payments (annualized)
        # High value = large positions paying significant funding
        funding_oi_stress = abs(data.funding_rate) * data.open_interest * 3 * 365

        # 4. OI/Price Divergence: OI change - price change
        # OI up + price flat = new leverage entering (risky)
        # OI down + price up = shorts covering (less risky)
        oi_price_divergence = data.oi_change_1h_pct - data.price_change_1h_pct

        # Calculate composite stress score from high-value signals
        composite_stress, stress_level = self._calculate_composite_stress(
            data.symbol,
            basis_pct,
            liquidation_oi_ratio,
            funding_oi_stress,
            oi_price_divergence,
        )

        return CLRIOutput(
            symbol=data.symbol,
            timestamp=data.timestamp,
            funding_score=funding_score,
            oi_score=oi_score,
            volume_score=volume_score,
            liquidation_score=liquidation_score,
            imbalance_score=imbalance_score,
            clri_score=clri_score,
            direction=direction,
            direction_confidence=direction_confidence,
            risk_level=risk_level,
            should_alert=should_alert,
            alert_message=alert_message,
            # High-value signals
            basis_pct=basis_pct,
            liquidation_oi_ratio=liquidation_oi_ratio,
            funding_oi_stress=funding_oi_stress,
            oi_price_divergence=oi_price_divergence,
            # Composite stress
            composite_stress=composite_stress,
            stress_level=stress_level,
            # Dynamic weighting info
            detected_regime=regime.regime_type,
            regime_intensity=regime.intensity,
            effective_weights=weights,
        )


@dataclass
class MarketCLRIOutput:
    """Output from Market CLRI aggregation"""
    market_clri: int
    risk_level: str
    market_direction: str
    symbols_elevated: int
    symbols_high: int

    # Dynamic OI weights used
    weights: dict  # {symbol: weight}
    total_oi: float

    # Top contributors for alert display
    top_contributors: list  # [(symbol, weight, clri_score), ...]

    # Divergence detection
    divergent_symbols: list  # [(symbol, clri_score, divergence), ...]

    # Individual readings for reference
    individual_readings: dict  # {symbol: clri_score}


class AggregateCLRI:
    """
    Aggregates CLRI across multiple symbols for market-wide view.

    Uses dynamic OI-based weighting instead of hardcoded weights.
    """

    # Minimum weight floor per symbol (3%)
    MIN_WEIGHT_FLOOR = 0.03

    # Divergence threshold (symbol CLRI must be 25+ pts above market)
    DIVERGENCE_THRESHOLD = 25

    def __init__(self):
        self.calculators: dict[str, CLRICalculator] = {}
        self._previous_market_clri: int = 0

    def get_calculator(self, symbol: str) -> CLRICalculator:
        """Get or create calculator for symbol"""
        if symbol not in self.calculators:
            self.calculators[symbol] = CLRICalculator()
        return self.calculators[symbol]

    def _calculate_oi_weights(
        self,
        readings: list[CLRIOutput],
        oi_data: dict[str, float],
    ) -> dict[str, float]:
        """
        Calculate dynamic weights from open interest.

        Args:
            readings: Individual CLRI readings
            oi_data: Dict of {symbol: total_oi_usd}

        Returns:
            Dict of {symbol: weight} where weights sum to 1.0
        """
        # Get symbols that have both readings and OI data
        symbols_with_oi = []
        for reading in readings:
            symbol = reading.symbol
            if symbol in oi_data and oi_data[symbol] > 0:
                symbols_with_oi.append((symbol, oi_data[symbol]))

        if not symbols_with_oi:
            # Fallback to equal weights if no OI data
            n = len(readings)
            return {r.symbol: 1.0 / n for r in readings} if n > 0 else {}

        total_oi = sum(oi for _, oi in symbols_with_oi)

        # Calculate raw weights from OI
        raw_weights = {symbol: oi / total_oi for symbol, oi in symbols_with_oi}

        # Apply minimum floor
        weights = {}
        for symbol, raw_weight in raw_weights.items():
            weights[symbol] = max(self.MIN_WEIGHT_FLOOR, raw_weight)

        # Renormalize to sum to 1.0
        total_weight = sum(weights.values())
        weights = {s: w / total_weight for s, w in weights.items()}

        return weights

    def calculate_market_clri(
        self,
        readings: list[CLRIOutput],
        oi_data: Optional[dict[str, float]] = None,
    ) -> MarketCLRIOutput:
        """
        Calculate market-wide CLRI from individual symbol readings.

        Args:
            readings: List of individual CLRIOutput per symbol
            oi_data: Optional dict of {symbol: total_oi_usd} for dynamic weighting.
                     If None, falls back to equal weighting.

        Returns:
            MarketCLRIOutput with composite score and metadata
        """
        if not readings:
            return MarketCLRIOutput(
                market_clri=0,
                risk_level='LOW',
                market_direction='BALANCED',
                symbols_elevated=0,
                symbols_high=0,
                weights={},
                total_oi=0,
                top_contributors=[],
                divergent_symbols=[],
                individual_readings={},
            )

        # Calculate dynamic weights from OI
        if oi_data:
            weights = self._calculate_oi_weights(readings, oi_data)
            total_oi = sum(oi_data.values())
        else:
            # Equal weighting fallback
            n = len(readings)
            weights = {r.symbol: 1.0 / n for r in readings}
            total_oi = 0

        # Calculate weighted market CLRI
        weighted_sum = 0
        total_weight = 0
        contributions = []

        for reading in readings:
            symbol = reading.symbol
            weight = weights.get(symbol, 0)
            if weight > 0:
                weighted_sum += reading.clri_score * weight
                total_weight += weight
                contributions.append((symbol, weight, reading.clri_score))

        market_clri = int(weighted_sum / total_weight) if total_weight > 0 else 0

        # Get top 3 contributors by weighted contribution
        contributions.sort(key=lambda x: x[1] * x[2], reverse=True)
        top_contributors = contributions[:3]

        # Detect divergent symbols (25+ pts above market)
        divergent_symbols = []
        for reading in readings:
            divergence = reading.clri_score - market_clri
            if divergence >= self.DIVERGENCE_THRESHOLD:
                divergent_symbols.append((reading.symbol, reading.clri_score, divergence))
        divergent_symbols.sort(key=lambda x: x[2], reverse=True)

        # Determine market direction
        long_crowded = sum(1 for r in readings if r.direction == "OVERLEVERAGED_LONG")
        short_crowded = sum(1 for r in readings if r.direction == "OVERLEVERAGED_SHORT")

        if long_crowded > short_crowded + 2:
            market_direction = "OVERLEVERAGED_LONG"
        elif short_crowded > long_crowded + 2:
            market_direction = "OVERLEVERAGED_SHORT"
        else:
            market_direction = "BALANCED"

        # Risk level
        risk_level = "LOW"
        for level, threshold in CLRICalculator.RISK_THRESHOLDS.items():
            if market_clri <= threshold:
                risk_level = level
                break

        # Store for tracking
        self._previous_market_clri = market_clri

        return MarketCLRIOutput(
            market_clri=market_clri,
            risk_level=risk_level,
            market_direction=market_direction,
            symbols_elevated=sum(1 for r in readings if r.clri_score >= 65),
            symbols_high=sum(1 for r in readings if r.clri_score >= 80),
            weights=weights,
            total_oi=total_oi,
            top_contributors=top_contributors,
            divergent_symbols=divergent_symbols,
            individual_readings={r.symbol: r.clri_score for r in readings},
        )
