"""Risk Manager Agent for protecting portfolio, sizing trades, and enforcing drawdown limits."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func

from polyagent.agents.base import BaseAgent
from polyagent.config import Settings
from polyagent.models import Signal, SignalStatus, Position, PositionStatus, Trade
from polyagent.utils.math import fractional_kelly, calculate_sharpe

logger = logging.getLogger(__name__)


class RiskManagerAgent(BaseAgent):
    """Gatekeeper for execution.
    
    Validates signals, sizes positions using fractional Kelly, checks exposure
    limits, drawdowns, and daily losses.
    """

    name = "RiskManagerAgent"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, interval_seconds=30)
        self.is_paused = False
        self.current_drawdown = 0.0
        self.peak_portfolio_value = 10_000.0  # Initial benchmark
        self.current_portfolio_value = 10_000.0

    async def setup(self) -> None:
        await super().setup()
        # Subscribe to signals detected by scanner or arbitrage agents
        self.subscribe_to("signal_detected", self.on_signal_detected)
        await self.log_action("started", {"max_drawdown": self.settings.max_drawdown_pct})

    async def run_cycle(self) -> None:
        """Periodic metrics calculation and risk evaluation."""
        logger.info("[%s] Running risk assessment cycle...", self.name)
        
        # 1. Fetch open positions and historical trades to evaluate portfolio value
        async with self._session_factory() as session:
            # Aggregate open positions cost
            pos_stmt = select(Position).where(Position.status == PositionStatus.OPEN)
            pos_res = await session.execute(pos_stmt)
            open_positions = list(pos_res.scalars().all())
            
            # Aggregate realized profit/loss
            trade_stmt = select(func.sum(Trade.pnl))
            trade_res = await session.execute(trade_stmt)
            total_realized_pnl = trade_res.scalar() or 0.0

        # Calculate current exposure and valuation
        current_exposure = sum(p.size * (p.current_price or p.entry_price) for p in open_positions)
        unrealized_pnl = sum(p.unrealized_pnl for p in open_positions)
        
        # In paper mode, we query the balance from Client or Settings. Let's assume a paper starting balance.
        starting_balance = float(self.settings.paper_balance)
        self.current_portfolio_value = starting_balance + total_realized_pnl + unrealized_pnl
        
        # Track drawdown
        if self.current_portfolio_value > self.peak_portfolio_value:
            self.peak_portfolio_value = self.current_portfolio_value
            
        if self.peak_portfolio_value > 0:
            self.current_drawdown = (self.peak_portfolio_value - self.current_portfolio_value) / self.peak_portfolio_value
            
        logger.info(
            "[%s] Portfolio Value: $%.2f | Exposure: $%.2f | Drawdown: %.2f%% | Paused: %s",
            self.name, self.current_portfolio_value, current_exposure, self.current_drawdown * 100, self.is_paused
        )

        # Check for drawdown trigger
        max_allowed_drawdown = float(self.settings.max_drawdown_pct) / 100.0
        if self.current_drawdown >= max_allowed_drawdown and not self.is_paused:
            self.is_paused = True
            logger.critical(
                "[%s] MAX DRAWDOWN EXCEEDED (%.2f%% >= %.2f%%). PAUSING ALL TRADING.",
                self.name, self.current_drawdown * 100, max_allowed_drawdown * 100
            )
            await self.emit_signal("system_alert", {
                "level": "CRITICAL",
                "message": f"Trading paused due to drawdown breach: {self.current_drawdown * 100:.2f}%"
            })
            await self.log_action("trading_paused_drawdown", {"drawdown": self.current_drawdown})

    async def on_signal_detected(self, signal_msg: dict[str, Any]) -> None:
        """Callback triggered whenever a new signal is emitted on the bus."""
        signal_id = signal_msg.get("signal_id")
        market_id = signal_msg.get("market_id")
        signal_type = signal_msg.get("type")
        edge = signal_msg.get("edge", 0.0)
        confidence = signal_msg.get("confidence", 0.0)
        question = signal_msg.get("question", "")

        logger.info("[%s] Assessing risk for signal %s (%s)", self.name, signal_id, signal_type)
        
        if self.is_paused:
            logger.warning("[%s] Signal rejected: Trading is paused.", self.name)
            await self._reject_signal(signal_id, "trading_paused")
            return

        # 1. Fetch current open positions to calculate exposure
        async with self._session_factory() as session:
            pos_stmt = select(Position).where(Position.status == PositionStatus.OPEN)
            pos_res = await session.execute(pos_stmt)
            open_positions = list(pos_res.scalars().all())
            
            # Count positions in this specific market
            market_pos = [p for p in open_positions if p.market_id == market_id]

        # Enforce single position per market rule unless it is arbitrage
        if market_pos and "arb" not in signal_type.lower() and "negative_risk" not in signal_type.lower():
            logger.warning("[%s] Signal rejected: Already have an open position in market %s", self.name, market_id)
            await self._reject_signal(signal_id, "already_positioned")
            return

        # Enforce total exposure limit
        current_exposure = sum(p.size * (p.current_price or p.entry_price) for p in open_positions)
        max_exposure = float(self.settings.max_total_exposure_usd)
        if current_exposure >= max_exposure:
            logger.warning("[%s] Signal rejected: Total exposure limit reached ($%.2f >= $%.2f)", self.name, current_exposure, max_exposure)
            await self._reject_signal(signal_id, "exposure_limit_exceeded")
            return

        # 2. Position Sizing
        # Determine target price and sizing parameters
        target_price = signal_msg.get("target_price")
        size_hint = None
        
        if signal_type == "statistical" and target_price:
            # Kelly position sizing: f = edge / net_odds. Capped by fractional Kelly (0.25).
            # Odds in prediction markets are 1 / target_price
            odds = 1.0 / target_price
            kelly_frac = fractional_kelly(edge, odds, fraction=0.25)
            # Size in cash = kelly fraction * current portfolio value
            suggested_cash_size = kelly_frac * self.current_portfolio_value
            # Cap size at maximum position size USD
            size_hint = min(float(self.settings.max_position_size_usd), suggested_cash_size)
            logger.debug(
                "[%s] Kelly Sizing for %s: odds=%.2f, kelly_frac=%.4f, suggested_cash=$%.2f, final=$%.2f",
                self.name, question[:40], odds, kelly_frac, suggested_cash_size, size_hint
            )
        elif signal_type == "internal_arb":
            # For internal arbitrage, the scanner suggests size_hint based on budget
            # We respect and limit it to our max position size
            size_hint = min(float(self.settings.max_position_size_usd), signal_msg.get("size_hint", 50.0))
        elif signal_type == "negative_risk":
            # Negative risk is multi-leg, total budget is scaled
            size_hint = min(float(self.settings.max_position_size_usd) * 3, 300.0)

        # Enforce minimal sizing floor
        if size_hint is None or size_hint < 1.0:
            logger.warning("[%s] Signal rejected: Sized below minimum threshold ($%.2f)", self.name, size_hint or 0.0)
            await self._reject_signal(signal_id, "size_too_small")
            return

        # If proposed size pushes total exposure over the limit, scale down
        if current_exposure + size_hint > max_exposure:
            size_hint = max(0.0, max_exposure - current_exposure)
            if size_hint < 1.0:
                logger.warning("[%s] Signal rejected: Sized below minimum threshold after scaling ($%.2f)", self.name, size_hint)
                await self._reject_signal(signal_id, "exposure_limit_breached_on_scale")
                return
            logger.info("[%s] Sized scaled down to $%.2f to respect total exposure limit.", self.name, size_hint)

        # 3. Approve Signal
        logger.info(
            "[%s] Signal APPROVED: Sized to $%.2f for market %s (%s)",
            self.name, size_hint, market_id, question[:40]
        )
        
        # Approve and emit signal
        await self._approve_signal(signal_id, size_hint, signal_msg)

    async def _approve_signal(self, signal_id: int, approved_size: float, signal_msg: dict[str, Any]) -> None:
        async with self._session_factory() as session:
            stmt = select(Signal).where(Signal.id == signal_id)
            res = await session.execute(stmt)
            signal = res.scalar_one_or_none()
            if signal:
                signal.status = SignalStatus.PENDING  # Kept in pending until executor fills it
                await session.commit()
        
        # Forward approval message to signal bus
        approved_msg = dict(signal_msg)
        approved_msg["approved_size_usd"] = approved_size
        await self.emit_signal("signal_approved", approved_msg)

    async def _reject_signal(self, signal_id: int, reason: str) -> None:
        async with self._session_factory() as session:
            stmt = select(Signal).where(Signal.id == signal_id)
            res = await session.execute(stmt)
            signal = res.scalar_one_or_none()
            if signal:
                signal.status = SignalStatus.REJECTED
                # Update details in JSON if possible
                data = json.loads(signal.data_json) if signal.data_json else {}
                data["rejection_reason"] = reason
                signal.data_json = json.dumps(data)
                await session.commit()
                
        await self.log_action("signal_rejected", {"signal_id": signal_id, "reason": reason})
