"""SQLAlchemy database models."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class SignalModel(Base):
    """Signal database model."""
    
    __tablename__ = "signals"
    
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    current_price = Column(Float, nullable=False)
    
    entry_zone_low = Column(Float, nullable=False)
    entry_zone_high = Column(Float, nullable=False)
    
    tp1_price = Column(Float, nullable=False)
    tp1_percent = Column(Float, nullable=False)
    
    tp2_price = Column(Float, nullable=False)
    tp2_percent = Column(Float, nullable=False)
    
    tp3_price = Column(Float, nullable=False)
    tp3_percent = Column(Float, nullable=False)
    
    tp4_price = Column(Float, nullable=False)
    tp4_percent = Column(Float, nullable=False)
    
    stop_loss_price = Column(Float, nullable=False)
    stop_loss_percent = Column(Float, nullable=False)
    
    risk_reward = Column(Float, nullable=False)
    score = Column(Integer, nullable=False)
    
    rsi = Column(Float, nullable=False)
    rsi_1h = Column(Float, nullable=False)
    rsi_4h = Column(Float, nullable=False)
    
    volume_change_percent = Column(Float, nullable=False)
    
    support_1 = Column(Float, nullable=False)
    resistance_1 = Column(Float, nullable=False)
    
    timeframe = Column(String(10), nullable=False, default="5m")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    
    reasons = Column(Text, nullable=False)  # JSON string
    indicators = Column(Text, nullable=False)  # JSON string
    
    # Relationships
    results = relationship("SignalResultModel", back_populates="signal", cascade="all, delete-orphan")


class SignalResultModel(Base):
    """Signal result tracking model."""
    
    __tablename__ = "signal_results"
    
    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    
    entry_price = Column(Float, nullable=False)
    max_price = Column(Float, nullable=False)
    max_price_percent = Column(Float, nullable=False)
    
    tp1_reached = Column(Boolean, default=False)
    tp2_reached = Column(Boolean, default=False)
    tp3_reached = Column(Boolean, default=False)
    tp4_reached = Column(Boolean, default=False)
    stop_hit = Column(Boolean, default=False)
    
    tracked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    
    # Relationships
    signal = relationship("SignalModel", back_populates="results")


class UserSettingsModel(Base):
    """User settings model."""
    
    __tablename__ = "user_settings"
    
    user_id = Column(Integer, primary_key=True)
    min_score = Column(Integer, default=75)
    min_tp_percent = Column(Integer, default=1)
    timeframes = Column(String(50), default="5m,15m,1h,4h")  # Comma-separated
    max_signals_displayed = Column(Integer, default=10)
    auto_notifications_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CooldownModel(Base):
    """Signal cooldown tracking."""
    
    __tablename__ = "signal_cooldowns"
    
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)
    last_signal_score = Column(Integer, nullable=False)
    last_signal_time = Column(DateTime, nullable=False, default=datetime.utcnow)
