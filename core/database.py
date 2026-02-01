"""
Database Models for CLRI

Uses SQLAlchemy with async support for PostgreSQL.
"""

from datetime import datetime, timezone
from typing import Optional
import enum

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, 
    Enum, Text, Index, UniqueConstraint, ForeignKey
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.dialects.postgresql import JSONB


Base = declarative_base()


class RiskLevel(enum.Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class Direction(enum.Enum):
    OVERLEVERAGED_LONG = "OVERLEVERAGED_LONG"
    OVERLEVERAGED_SHORT = "OVERLEVERAGED_SHORT"
    BALANCED = "BALANCED"
    # Legacy aliases for database compatibility
    LONG_CROWDED = "OVERLEVERAGED_LONG"
    SHORT_CROWDED = "OVERLEVERAGED_SHORT"
    NEUTRAL = "BALANCED"


class CLRIReading(Base):
    """
    Individual CLRI readings per symbol
    """
    __tablename__ = 'clri_readings'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    
    # Raw inputs
    funding_rate = Column(Float)
    predicted_funding = Column(Float)
    open_interest = Column(Float)
    oi_change_1h_pct = Column(Float)
    oi_change_24h_pct = Column(Float)
    perp_volume_24h = Column(Float)
    spot_volume_24h = Column(Float)
    long_short_ratio = Column(Float)
    liquidations_1h_usd = Column(Float)
    bid_depth_usd = Column(Float)
    ask_depth_usd = Column(Float)
    price = Column(Float)
    
    # Component scores
    funding_score = Column(Float)
    oi_score = Column(Float)
    volume_score = Column(Float)
    liquidation_score = Column(Float)
    imbalance_score = Column(Float)

    # High-value signals
    basis_pct = Column(Float)  # (Futures - Spot) / Spot * 100
    liquidation_oi_ratio = Column(Float)  # Liquidations / OI * 100
    funding_oi_stress = Column(Float)  # |Funding| * OI (annualized $)
    oi_price_divergence = Column(Float)  # OI change % - Price change %

    # Composite
    clri_score = Column(Integer, nullable=False, index=True)
    direction = Column(Enum(Direction), nullable=False)
    direction_confidence = Column(Float)
    risk_level = Column(Enum(RiskLevel), nullable=False, index=True)

    # Alert tracking
    alert_triggered = Column(Boolean, default=False)

    # Composite stress score (algorithm enhancement 4D)
    composite_stress = Column(Float)
    stress_level = Column(String(20))  # LOW, MODERATE, ELEVATED, HIGH, EXTREME

    # Dynamic weighting info (algorithm enhancement 4C)
    detected_regime = Column(String(30))  # NORMAL, LIQUIDATION_CASCADE, FUNDING_STRESS, OI_SPIKE
    regime_intensity = Column(Float)

    # Constraints
    __table_args__ = (
        UniqueConstraint('timestamp', 'symbol', name='uq_reading_time_symbol'),
        Index('ix_readings_symbol_time', 'symbol', 'timestamp'),
    )


class MarketSummary(Base):
    """
    Aggregate market CLRI readings
    """
    __tablename__ = 'market_summaries'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, unique=True, index=True)
    
    market_clri = Column(Integer, nullable=False)
    risk_level = Column(Enum(RiskLevel), nullable=False)
    market_direction = Column(String(20))
    
    symbols_tracked = Column(Integer)
    symbols_elevated = Column(Integer)
    symbols_high = Column(Integer)
    
    # JSON blob with individual readings
    individual_readings = Column(JSONB)


class Alert(Base):
    """
    Alert history for tracking and analysis
    """
    __tablename__ = 'alerts'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    triggered_at = Column(DateTime(timezone=True), nullable=False, index=True)
    
    symbol = Column(String(20), nullable=False, index=True)
    alert_type = Column(String(50), nullable=False)  # THRESHOLD_CROSSED, SPIKE_DETECTED, etc.
    
    clri_score = Column(Integer)
    risk_level = Column(Enum(RiskLevel))
    direction = Column(Enum(Direction))
    
    message = Column(Text)
    
    # Delivery tracking
    discord_sent = Column(Boolean, default=False)
    telegram_sent = Column(Boolean, default=False)
    
    # For spike validation
    price_before = Column(Float)
    price_after = Column(Float)
    price_change_pct = Column(Float)
    validated = Column(Boolean)  # Did the predicted spike happen?


class PriceSpike(Base):
    """
    Record of actual price spikes for model validation
    """
    __tablename__ = 'price_spikes'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    
    # Spike details
    price_change_pct = Column(Float, nullable=False)
    timeframe_minutes = Column(Integer)  # How long the move took
    direction = Column(String(10))  # UP or DOWN
    
    # CLRI at time of spike
    clri_score_before = Column(Integer)
    clri_score_peak = Column(Integer)
    clri_direction = Column(Enum(Direction))
    
    # For learning
    was_predicted = Column(Boolean)  # Did CLRI flag this beforehand?
    alert_id = Column(Integer, ForeignKey('alerts.id'))


class User(Base):
    """
    Simple user model for dashboard authentication
    """
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime(timezone=True))
    is_active = Column(Boolean, default=True)
    
    # Preferences
    alert_preferences = Column(JSONB, default=dict)  # Which alerts they want


class Session(Base):
    """
    User sessions for authentication
    """
    __tablename__ = 'sessions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    user = relationship("User")


# Database connection management

class Database:
    """
    Async database connection manager
    """
    
    def __init__(self, database_url: str):
        # Convert postgres:// to postgresql+asyncpg://
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql+asyncpg://', 1)
        elif database_url.startswith('postgresql://'):
            database_url = database_url.replace('postgresql://', 'postgresql+asyncpg://', 1)
        
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        
        self.async_session = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    
    async def create_tables(self):
        """Create all tables"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    async def drop_tables(self):
        """Drop all tables (use with caution!)"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    
    def session(self) -> AsyncSession:
        """Get a new session"""
        return self.async_session()
    
    async def close(self):
        """Close the engine"""
        await self.engine.dispose()


# Repository functions for common operations

async def save_clri_reading(session: AsyncSession, reading: CLRIReading):
    """Save a CLRI reading"""
    session.add(reading)
    await session.commit()


async def save_clri_readings_batch(session: AsyncSession, readings: list[CLRIReading]):
    """Save multiple CLRI readings"""
    session.add_all(readings)
    await session.commit()


async def get_latest_readings(
    session: AsyncSession,
    limit: int = 100,
    symbol: Optional[str] = None,
) -> list[CLRIReading]:
    """Get the latest CLRI readings"""
    from sqlalchemy import select
    
    query = select(CLRIReading).order_by(CLRIReading.timestamp.desc()).limit(limit)
    
    if symbol:
        query = query.where(CLRIReading.symbol == symbol)
    
    result = await session.execute(query)
    return result.scalars().all()


async def get_readings_for_timerange(
    session: AsyncSession,
    symbol: str,
    start_time: datetime,
    end_time: datetime,
) -> list[CLRIReading]:
    """Get CLRI readings for a symbol within a time range"""
    from sqlalchemy import select, and_
    
    query = (
        select(CLRIReading)
        .where(
            and_(
                CLRIReading.symbol == symbol,
                CLRIReading.timestamp >= start_time,
                CLRIReading.timestamp <= end_time,
            )
        )
        .order_by(CLRIReading.timestamp)
    )
    
    result = await session.execute(query)
    return result.scalars().all()


async def save_alert(session: AsyncSession, alert: Alert):
    """Save an alert"""
    session.add(alert)
    await session.commit()


async def get_recent_alerts(
    session: AsyncSession,
    limit: int = 50,
    symbol: Optional[str] = None,
) -> list[Alert]:
    """Get recent alerts"""
    from sqlalchemy import select
    
    query = select(Alert).order_by(Alert.triggered_at.desc()).limit(limit)
    
    if symbol:
        query = query.where(Alert.symbol == symbol)
    
    result = await session.execute(query)
    return result.scalars().all()
