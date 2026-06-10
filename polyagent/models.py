"""SQLAlchemy 2.0 ORM models for the PolyAgent trading system.

All tables live under a single ``Base`` declarative base so they can be
created together via ``Base.metadata.create_all``.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


# ── Helper ───────────────────────────────────────────────────────
def _utcnow() -> datetime:
    """Return the current UTC timestamp (timezone-aware)."""
    return datetime.now(timezone.utc)


# ── Enums ────────────────────────────────────────────────────────


class SignalType(str, enum.Enum):
    """Classification of trading signals detected by agents."""

    INTERNAL_ARB = "internal_arb"
    NEGATIVE_RISK = "negative_risk"
    CROSS_PLATFORM = "cross_platform"
    SENTIMENT = "sentiment"
    STATISTICAL = "statistical"
    VOLUME_SPIKE = "volume_spike"
    ORDERBOOK_IMBALANCE = "orderbook_imbalance"


class SignalStatus(str, enum.Enum):
    """Lifecycle status of a signal."""

    PENDING = "pending"
    EXECUTED = "executed"
    EXPIRED = "expired"
    REJECTED = "rejected"


class OrderStatus(str, enum.Enum):
    """Lifecycle status of an order."""

    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


class OrderSide(str, enum.Enum):
    """Trade direction."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    """Polymarket order type (maps to CLOB API order types)."""

    GTC = "GTC"  # Good-til-cancelled – maker, 0 % fee
    FOK = "FOK"  # Fill-or-kill – taker
    GTD = "GTD"  # Good-til-date
    FAK = "FAK"  # Fill-and-kill


class PositionStatus(str, enum.Enum):
    """Whether a position is currently held."""

    OPEN = "open"
    CLOSED = "closed"


class MarketStatus(str, enum.Enum):
    """Polymarket market lifecycle."""

    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"


# ── Declarative base ─────────────────────────────────────────────


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    pass


# ── Market ───────────────────────────────────────────────────────


class Market(Base):
    """Snapshot of a Polymarket binary-outcome market."""

    __tablename__ = "markets"
    __table_args__ = (
        Index("ix_markets_condition_id", "condition_id", unique=True),
        Index("ix_markets_status", "status"),
        Index("ix_markets_category", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    condition_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    yes_token_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    no_token_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    yes_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    no_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    volume_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    liquidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)

    status: Mapped[MarketStatus] = mapped_column(
        Enum(MarketStatus, native_enum=False, length=16),
        nullable=False,
        default=MarketStatus.ACTIVE,
    )

    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    # Relationships
    signals: Mapped[list["Signal"]] = relationship("Signal", back_populates="market", lazy="selectin")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="market", lazy="selectin")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="market", lazy="selectin")
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="market", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Market id={self.id} q={self.question[:40]!r} yes={self.yes_price} no={self.no_price}>"


# ── Signal ───────────────────────────────────────────────────────


class Signal(Base):
    """A trading signal emitted by one of the detection agents."""

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_market_id", "market_id"),
        Index("ix_signals_status", "status"),
        Index("ix_signals_signal_type", "signal_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)

    signal_type: Mapped[SignalType] = mapped_column(
        Enum(SignalType, native_enum=False, length=32), nullable=False
    )
    edge_pct: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    data_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[SignalStatus] = mapped_column(
        Enum(SignalStatus, native_enum=False, length=16),
        nullable=False,
        default=SignalStatus.PENDING,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    # Relationships
    market: Mapped["Market"] = relationship("Market", back_populates="signals")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="signal", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Signal id={self.id} type={self.signal_type.value} edge={self.edge_pct:.2f}%>"


# ── Order ────────────────────────────────────────────────────────


class Order(Base):
    """An order submitted (or simulated) on Polymarket."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_market_id", "market_id"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, comment="Polymarket CLOB order ID"
    )
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)

    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, native_enum=False, length=8), nullable=False
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, native_enum=False, length=8), nullable=False, default=OrderType.GTC
    )

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=20),
        nullable=False,
        default=OrderStatus.SUBMITTED,
    )

    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fill_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    market: Mapped["Market"] = relationship("Market", back_populates="orders")
    signal: Mapped[Optional["Signal"]] = relationship("Signal", back_populates="orders")
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="order", lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} side={self.side.value} "
            f"price={self.price} size={self.size} status={self.status.value}>"
        )


# ── Position ─────────────────────────────────────────────────────


class Position(Base):
    """An open or closed position in a market outcome token."""

    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_market_id", "market_id"),
        Index("ix_positions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, native_enum=False, length=8), nullable=False
    )

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus, native_enum=False, length=8),
        nullable=False,
        default=PositionStatus.OPEN,
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    market: Mapped["Market"] = relationship("Market", back_populates="positions")

    def __repr__(self) -> str:
        return (
            f"<Position id={self.id} side={self.side.value} "
            f"size={self.size} pnl={self.unrealized_pnl:+.2f} status={self.status.value}>"
        )


# ── Trade ────────────────────────────────────────────────────────


class Trade(Base):
    """A completed fill / execution record."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_market_id", "market_id"),
        Index("ix_trades_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)

    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, native_enum=False, length=8), nullable=False
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strategy: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="trades")
    market: Mapped["Market"] = relationship("Market", back_populates="trades")

    def __repr__(self) -> str:
        return f"<Trade id={self.id} side={self.side.value} price={self.price} size={self.size}>"


# ── AgentLog ─────────────────────────────────────────────────────


class AgentLog(Base):
    """Audit log of actions taken by any agent in the system."""

    __tablename__ = "agent_logs"
    __table_args__ = (
        Index("ix_agent_logs_agent_name", "agent_name"),
        Index("ix_agent_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AgentLog id={self.id} agent={self.agent_name} action={self.action}>"
