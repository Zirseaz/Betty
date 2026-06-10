"""Executor Agent for executing Polymarket order books, paper fills, and recording trades."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select

from polyagent.agents.base import BaseAgent
from polyagent.clients.polymarket import PolymarketClient
from polyagent.config import Settings
from polyagent.models import (
    Order, OrderSide, OrderStatus, OrderType,
    Position, PositionStatus, Trade, Signal, SignalStatus
)
from polyagent.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


class ExecutorAgent(BaseAgent):
    """Processes approved signals and executes orders on Polymarket (or simulates them)."""

    name = "ExecutorAgent"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, interval_seconds=15)
        self.client = PolymarketClient(settings)
        self.notifier = TelegramNotifier(settings)

    async def setup(self) -> None:
        await super().setup()
        # Subscribe to approved trading signals from Risk Manager
        self.subscribe_to("signal_approved", self.on_signal_approved)
        if self.settings.is_paper:
            self.subscribe_to("paper_order_filled", self.on_paper_order_filled)
        await self.log_action("started")

    async def teardown(self) -> None:
        await self.client.close()
        await self.log_action("stopped")
        await super().teardown()

    async def run_cycle(self) -> None:
        """Keepalive cycle. Most action is event-driven via subscribers."""
        logger.debug("[%s] Executor cycle running...", self.name)

    async def on_signal_approved(self, msg: dict[str, Any]) -> None:
        """Triggered when the Risk Manager approves a signal for execution."""
        signal_id = msg.get("signal_id")
        market_id = msg.get("market_id")
        signal_type = msg.get("type")
        approved_size_usd = msg.get("approved_size_usd", 0.0)
        question = msg.get("question", "")

        logger.info(
            "[%s] Executing approved signal %s (%s) with size $%.2f",
            self.name, signal_id, signal_type, approved_size_usd
        )

        async with self._session_factory() as session:
            # Re-fetch the signal to confirm it exists and update its status
            sig_stmt = select(Signal).where(Signal.id == signal_id)
            sig_res = await session.execute(sig_stmt)
            signal = sig_res.scalar_one_or_none()
            if not signal:
                logger.error("[%s] Approved signal %s not found in DB", self.name, signal_id)
                return

        try:
            if signal_type == "internal_arb":
                # Multi-leg binary arb: BUY YES and BUY NO
                await self._execute_internal_arb(signal, approved_size_usd, question)
            elif signal_type == "negative_risk":
                # Multi-leg negative risk: BUY NO on all outcomes
                await self._execute_negative_risk(signal, approved_size_usd, question)
            else:
                # Standard value signal (statistical)
                target_token_id = msg.get("target_token_id")
                target_price = msg.get("target_price")
                side = msg.get("side", "buy") # statistical trades are directional buy/sell YES
                if target_token_id and target_price:
                    await self._execute_standard_trade(signal, target_token_id, target_price, side, approved_size_usd, question)
                else:
                    logger.error("[%s] Standard trade missing token ID or price info", self.name)
        except Exception as e:
            logger.error("[%s] Execution failed for signal %s: %s", self.name, signal_id, e, exc_info=True)
            async with self._session_factory() as session:
                session.add(signal)
                signal.status = SignalStatus.REJECTED
                await session.commit()

    async def _execute_standard_trade(
        self, signal: Signal, token_id: str, target_price: float, side: str, approved_size_usd: float, question: str
    ) -> None:
        """Executes a single directional trade (GTC buy/sell)."""
        # Shares to buy = approved size / price
        shares = approved_size_usd / target_price
        
        # In live mode we use GTC (maker) to avoid high taker fees; in paper mode, we simulate immediately
        order_type = "GTC"
        
        # Place order via PolymarketClient wrapper
        loop = asyncio.get_running_loop()
        receipt = await loop.run_in_executor(
            None, self.client.place_order, token_id, "BUY", target_price, shares, order_type
        )
        
        order_id = receipt.get("order_id")
        status_str = receipt.get("status")

        async with self._session_factory() as session:
            # Create Order entry in database
            db_order = Order(
                order_id=order_id,
                market_id=signal.market_id,
                signal_id=signal.id,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                price=target_price,
                size=shares,
                order_type=OrderType.GTC,
                status=OrderStatus.FILLED if status_str == "FILLED" else OrderStatus.SUBMITTED,
                fill_price=receipt.get("fill_price") if status_str == "FILLED" else None,
                fill_size=receipt.get("filled") if status_str == "FILLED" else None,
            )
            session.add(db_order)
            
            # If filled immediately, process position and trade
            if status_str == "FILLED":
                fill_price = receipt.get("fill_price", target_price)
                fill_size = receipt.get("filled", shares)
                
                await self._process_fill(session, db_order, fill_price, fill_size, "statistical")
                signal.status = SignalStatus.EXECUTED
                
                # Send Telegram fill notification
                await self.notifier.send_trade(
                    side="BUY", size=fill_size, price=fill_price,
                    question=question, strategy="statistical", order_id=order_id or "simulated"
                )
            else:
                signal.status = SignalStatus.PENDING

            await session.commit()

    async def _execute_internal_arb(self, signal: Signal, approved_size_usd: float, question: str) -> None:
        """Executes a binary YES/NO arbitrage by submitting FOK orders on both sides."""
        sig_data = json.loads(signal.data_json) if signal.data_json else {}
        yes_token_id = sig_data.get("yes_token_id")
        no_token_id = sig_data.get("no_token_id")
        yes_price = sig_data.get("yes_price")
        no_price = sig_data.get("no_price")

        if not all([yes_token_id, no_token_id, yes_price, no_price]):
            logger.error("[%s] Internal arb missing token IDs or prices in signal details", self.name)
            return

        # Arbitrage is executed with Fill-Or-Kill (FOK) to avoid partial fills
        # Buy YES and buy NO at the same time
        total_price = yes_price + no_price
        shares = approved_size_usd / total_price

        logger.info("[%s] Placing FOK orders for internal arb. YES=%s NO=%s Shares=%s", self.name, yes_price, no_price, shares)
        
        loop = asyncio.get_running_loop()
        
        # Parallel order execution for both legs
        yes_task = loop.run_in_executor(None, self.client.place_order, yes_token_id, "BUY", yes_price, shares, "FOK")
        no_task = loop.run_in_executor(None, self.client.place_order, no_token_id, "BUY", no_price, shares, "FOK")
        
        yes_receipt, no_receipt = await asyncio.gather(yes_task, no_task)

        yes_status = yes_receipt.get("status")
        no_status = no_receipt.get("status")

        async with self._session_factory() as session:
            # Insert orders into DB
            yes_order = Order(
                order_id=yes_receipt.get("order_id"),
                market_id=signal.market_id,
                signal_id=signal.id,
                side=OrderSide.BUY,
                price=yes_price,
                size=shares,
                order_type=OrderType.FOK,
                status=OrderStatus.FILLED if yes_status == "FILLED" else OrderStatus.FAILED,
                fill_price=yes_receipt.get("fill_price") if yes_status == "FILLED" else None,
                fill_size=yes_receipt.get("filled") if yes_status == "FILLED" else None,
            )
            no_order = Order(
                order_id=no_receipt.get("order_id"),
                market_id=signal.market_id,
                signal_id=signal.id,
                side=OrderSide.BUY,
                price=no_price,
                size=shares,
                order_type=OrderType.FOK,
                status=OrderStatus.FILLED if no_status == "FILLED" else OrderStatus.FAILED,
                fill_price=no_receipt.get("fill_price") if no_status == "FILLED" else None,
                fill_size=no_receipt.get("filled") if no_status == "FILLED" else None,
            )
            session.add(yes_order)
            session.add(no_order)

            # If both legs filled, success!
            if yes_status == "FILLED" and no_status == "FILLED":
                logger.info("[%s] Both arbitrage legs FILLED successfully!", self.name)
                
                await self._process_fill(session, yes_order, yes_receipt.get("fill_price"), yes_receipt.get("filled"), "internal_arb")
                await self._process_fill(session, no_order, no_receipt.get("fill_price"), no_receipt.get("filled"), "internal_arb")
                
                signal.status = SignalStatus.EXECUTED
                
                # Notify Telegram
                await self.notifier.send_trade(
                    side="BUY BOTH", size=shares, price=total_price,
                    question=question, strategy="internal_arb", order_id=f"YES:{yes_order.order_id} / NO:{no_order.order_id}"
                )
            else:
                logger.warning("[%s] Arbitrage execution failed. YES=%s, NO=%s. Reverting.", self.name, yes_status, no_status)
                signal.status = SignalStatus.REJECTED
                
                # If one leg filled but not the other, we are in a bad state (legacy tail risk). Cancel the open one.
                if yes_status == "FILLED" and no_status != "FILLED":
                    # Paper trading reverts balance automatically, in live we would sell YES back or log error
                    logger.error("[%s] Leg imbalance: YES filled, NO failed. Manual intervention required.", self.name)
                elif no_status == "FILLED" and yes_status != "FILLED":
                    logger.error("[%s] Leg imbalance: NO filled, YES failed. Manual intervention required.", self.name)

            await session.commit()

    async def _execute_negative_risk(self, signal: Signal, approved_size_usd: float, question: str) -> None:
        """Executes a negative risk multi-outcome trade by buying NO on all outcomes."""
        sig_data = json.loads(signal.data_json) if signal.data_json else {}
        allocations = sig_data.get("allocations", [])

        if not allocations:
            logger.error("[%s] Negative risk signal missing allocations in details", self.name)
            return

        logger.info("[%s] Executing multi-leg negative risk. outcomes count: %d", self.name, len(allocations))
        
        loop = asyncio.get_running_loop()
        tasks = []
        for alloc in allocations:
            no_token_id = alloc.get("no_token_id")
            no_price = alloc.get("no_price")
            shares = alloc.get("shares")
            # We buy NO using GTC/FOK. FOK is safer to ensure we get filled on all legs
            tasks.append(
                loop.run_in_executor(None, self.client.place_order, no_token_id, "BUY", no_price, shares, "FOK")
            )

        receipts = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Verify if all legs succeeded
        all_success = True
        for i, receipt in enumerate(receipts):
            if isinstance(receipt, Exception) or receipt.get("status") != "FILLED":
                all_success = False
                logger.warning("[%s] Negative-risk leg %d failed to fill: %s", self.name, i, receipt)

        async with self._session_factory() as session:
            # We process order records
            db_orders = []
            for alloc, receipt in zip(allocations, receipts):
                is_ok = not isinstance(receipt, Exception) and receipt.get("status") == "FILLED"
                db_order = Order(
                    order_id=None if isinstance(receipt, Exception) else receipt.get("order_id"),
                    market_id=alloc["market_id"],
                    signal_id=signal.id,
                    side=OrderSide.BUY,
                    price=alloc["no_price"],
                    size=alloc["shares"],
                    order_type=OrderType.FOK,
                    status=OrderStatus.FILLED if is_ok else OrderStatus.FAILED,
                    fill_price=None if isinstance(receipt, Exception) else receipt.get("fill_price"),
                    fill_size=None if isinstance(receipt, Exception) else receipt.get("filled"),
                )
                session.add(db_order)
                db_orders.append((db_order, receipt, is_ok, alloc))

            if all_success:
                logger.info("[%s] All negative risk legs filled successfully!", self.name)
                for db_order, receipt, _, alloc in db_orders:
                    await self._process_fill(session, db_order, receipt.get("fill_price"), receipt.get("filled"), "negative_risk")
                signal.status = SignalStatus.EXECUTED
                await self.notifier.send_alert(f"Negative risk arbitrage successfully executed on: {question}", level="INFO")
            else:
                logger.warning("[%s] Negative risk execution failed or was incomplete.", self.name)
                signal.status = SignalStatus.REJECTED
                # Log critical alert if we got filled on some legs but not all (unhedged exposure)
                filled_count = sum(1 for _, _, ok, _ in db_orders if ok)
                if filled_count > 0:
                    logger.critical("[%s] Partial fill on negative-risk arb (%d/%d legs filled). Portfolio is unhedged!", self.name, filled_count, len(allocations))
                    await self.notifier.send_alert(
                        f"CRITICAL: Negative-risk execution failed with partial fills ({filled_count}/{len(allocations)}). Portfolio is unhedged!",
                        level="CRITICAL"
                    )

            await session.commit()

    async def _process_fill(self, session: AsyncSession, order: Order, fill_price: float, fill_size: float, strategy: str) -> None:
        """Updates positions and creates a Trade record upon order fill."""
        # 1. Create Trade record
        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=fill_price,
            size=fill_size,
            fee=0.018 * fill_price * fill_size if order.order_type == OrderType.FOK else 0.0,
            pnl=0.0,
            strategy=strategy,
            created_at=datetime.now(timezone.utc),
        )
        session.add(trade)

        # 2. Update Position record
        # Query for open position in this market for the YES/NO token
        # In this context, order.side is always BUY since we only buy outcomes to open trades
        # (polymarket shorting is just buying NO)
        token_id = ""
        # Find token_id from Market table
        market_stmt = select(Market).where(Market.id == order.market_id)
        market_res = await session.execute(market_stmt)
        market = market_res.scalar_one_or_none()
        
        if market:
            # Check if this order is buying YES or NO token
            # We match the price of the order against YES/NO price, or check token IDs in signal details
            # If we buy NO, token_id = market.no_token_id. If YES, token_id = market.yes_token_id
            if abs(order.price - (market.yes_price or 0.0)) < abs(order.price - (market.no_price or 1.0)):
                token_id = market.yes_token_id or ""
            else:
                token_id = market.no_token_id or ""

        pos_stmt = select(Position).where(
            (Position.market_id == order.market_id) &
            (Position.token_id == token_id) &
            (Position.status == PositionStatus.OPEN)
        )
        pos_res = await session.execute(pos_stmt)
        position = pos_res.scalar_one_or_none()

        if position is None:
            # Create new open position
            position = Position(
                market_id=order.market_id,
                token_id=token_id,
                side=order.side,
                entry_price=fill_price,
                size=fill_size,
                current_price=fill_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                status=PositionStatus.OPEN,
                opened_at=datetime.now(timezone.utc),
            )
            session.add(position)
        else:
            # Scale up existing position
            new_size = position.size + fill_size
            position.entry_price = ((position.entry_price * position.size) + (fill_price * fill_size)) / new_size
            position.size = new_size
            position.current_price = fill_price
            position.unrealized_pnl = 0.0

    async def on_paper_order_filled(self, msg: dict[str, Any]) -> None:
        """Event fired in paper mode when a resting order is simulated as filled."""
        order_id = msg.get("order_id")
        fill_price = msg.get("fill_price", 0.0)
        fill_size = msg.get("filled", 0.0)

        logger.info("[%s] Paper fill event received for order %s", self.name, order_id)
        
        async with self._session_factory() as session:
            stmt = select(Order).where(Order.order_id == order_id)
            res = await session.execute(stmt)
            order = res.scalar_one_or_none()
            
            if order and order.status != OrderStatus.FILLED:
                order.status = OrderStatus.FILLED
                order.fill_price = fill_price
                order.fill_size = fill_size
                order.updated_at = datetime.now(timezone.utc)
                
                # Fetch signal to get strategy name
                sig_name = "statistical"
                if order.signal_id:
                    sig_stmt = select(Signal).where(Signal.id == order.signal_id)
                    sig_res = await session.execute(sig_stmt)
                    sig = sig_res.scalar_one_or_none()
                    if sig:
                        sig.status = SignalStatus.EXECUTED
                        sig_name = sig.signal_type.value

                await self._process_fill(session, order, fill_price, fill_size, sig_name)
                await session.commit()
                
                # Query question for notification
                market_stmt = select(Market).where(Market.id == order.market_id)
                market_res = await session.execute(market_stmt)
                market = market_res.scalar_one_or_none()
                question = market.question if market else "Unknown"
                
                await self.notifier.send_trade(
                    side=order.side.value.upper(), size=fill_size, price=fill_price,
                    question=question, strategy=sig_name, order_id=order_id
                )
