"""Arbitrage Agent for detecting and emitting internal YES/NO and negative risk arbitrage signals."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from sqlalchemy import select

from polyagent.agents.base import BaseAgent
from polyagent.config import Settings
from polyagent.models import Market, Signal, SignalType, SignalStatus
from polyagent.strategies.internal_arb import detect_internal_arbitrage
from polyagent.strategies.negative_risk import detect_negative_risk

logger = logging.getLogger(__name__)


class ArbitrageAgent(BaseAgent):
    """Monitors active markets in the database for arbitrage opportunities.
    
    Implements:
    1. Internal YES/NO arbitrage (gross sum YES+NO < 1.0)
    2. Negative-risk multi-outcome arbitrage (sum of YES prices > 1.0)
    """

    name = "ArbitrageAgent"

    def __init__(self, settings: Settings) -> None:
        # Runs every 15 seconds
        super().__init__(settings, interval_seconds=15)

    async def setup(self) -> None:
        await super().setup()
        await self.log_action("started")

    async def run_cycle(self) -> None:
        logger.info("[%s] Beginning arbitrage scan cycle...", self.name)
        
        # 1. Fetch active markets from database
        async with self._session_factory() as session:
            stmt = select(Market).where(Market.yes_price != None)
            res = await session.execute(stmt)
            markets = list(res.scalars().all())

        if not markets:
            logger.info("[%s] No active markets found in DB to scan.", self.name)
            return

        # 2. Check binary YES/NO internal arbitrage (YES+NO < 1.0)
        # Note: the scanner does this on write, but the ArbitrageAgent verifies it 
        # and handles the final signal generation with exact risk parameters.
        binary_arb_count = 0
        for m in markets:
            if m.yes_price is None or m.no_price is None:
                continue
            
            try:
                # Run internal arb strategy
                arb_signal = detect_internal_arbitrage(
                    m.yes_price,
                    m.no_price,
                    taker_fee=0.018,
                    max_position_usd=float(self.settings.max_position_size_usd),
                )
                
                if arb_signal:
                    # profitable internal arb found!
                    # Check if a pending signal already exists for this market to avoid duplicates
                    async with self._session_factory() as session:
                        dup_stmt = select(Signal).where(
                            (Signal.market_id == m.id) & 
                            (Signal.signal_type == SignalType.INTERNAL_ARB) &
                            (Signal.status == SignalStatus.PENDING)
                        )
                        dup_res = await session.execute(dup_stmt)
                        if dup_res.scalar_one_or_none() is not None:
                            continue

                        signal = Signal(
                            market_id=m.id,
                            signal_type=SignalType.INTERNAL_ARB,
                            edge_pct=arb_signal.net_edge,
                            confidence=0.9,
                            data_json=json.dumps({
                                "yes_price": arb_signal.yes_price,
                                "no_price": arb_signal.no_price,
                                "direction": arb_signal.direction.value,
                                "size_hint": arb_signal.size_hint,
                                "roi_pct": arb_signal.roi_pct,
                                "yes_token_id": m.yes_token_id,
                                "no_token_id": m.no_token_id,
                            }),
                            status=SignalStatus.PENDING,
                        )
                        session.add(signal)
                        await session.commit()
                        await session.refresh(signal)
                        
                        logger.info(
                            "[%s] Emitting internal arb signal: ROI=%s%%, Edge=%s",
                            self.name, round(arb_signal.roi_pct, 2), round(arb_signal.net_edge * 100, 2)
                        )
                        binary_arb_count += 1
                        
                        await self.emit_signal(
                            "signal_detected",
                            {
                                "signal_id": signal.id,
                                "market_id": m.id,
                                "question": m.question,
                                "type": SignalType.INTERNAL_ARB.value,
                                "edge": arb_signal.net_edge,
                                "confidence": 0.9,
                                "target_token_id": m.yes_token_id,  # Arbitrage executor buys both, executor handles details
                                "target_price": m.yes_price,
                            }
                        )
            except Exception as e:
                logger.error("Error executing internal arb strategy: %s", e)

        # 3. Check negative-risk multi-outcome arbitrage
        # Group markets by parent question prefix to detect multi-outcome events
        # e.g., "Will Candidate X win?" vs "Will Candidate Y win?" grouped under same event
        events = defaultdict(list)
        for m in markets:
            if m.yes_price is None:
                continue
                
            # Polymarket multi-outcome questions usually contain ' - ' or ':' or similar delimiter
            # Example: "Formula 1 Winner - Hamilton", "Formula 1 Winner - Verstappen"
            parent_question = m.question
            for sep in [" - ", ": ", " – "]:
                if sep in m.question:
                    parent_question = m.question.split(sep, maxsplit=1)[0].strip()
                    break
            events[parent_question].append(m)

        neg_risk_count = 0
        for parent_q, m_list in events.items():
            if len(m_list) < 2:
                continue

            # Format outcomes for the negative risk strategy
            outcomes = []
            for m in m_list:
                outcomes.append({
                    "outcome_id": m.condition_id,
                    "outcome_label": m.question,
                    "yes_price": m.yes_price,
                    "no_price": m.no_price,
                    "market_id": m.id,
                    "no_token_id": m.no_token_id,
                })

            try:
                # Run negative risk strategy
                neg_risk_signal = detect_negative_risk(
                    outcomes,
                    market_id=parent_q,
                    taker_fee=0.018,
                    max_budget_usd=float(self.settings.max_position_size_usd) * 3,  # Multi-leg allows larger budget
                )

                if neg_risk_signal:
                    # profitable negative risk opportunity found!
                    # For negative risk, we buy NO on all legs. We create a signal on the first market of the list,
                    # or write a multi-market signal. We link it to the first market and store details.
                    primary_market = m_list[0]
                    
                    # Check if a pending negative risk signal already exists for this group
                    async with self._session_factory() as session:
                        dup_stmt = select(Signal).where(
                            (Signal.market_id == primary_market.id) & 
                            (Signal.signal_type == SignalType.NEGATIVE_RISK) &
                            (Signal.status == SignalStatus.PENDING)
                        )
                        dup_res = await session.execute(dup_stmt)
                        if dup_res.scalar_one_or_none() is not None:
                            continue

                        # Prepare detail allocations
                        allocs = []
                        for alloc in neg_risk_signal.allocations:
                            # Find the matching market ID from our outcomes list
                            matching_m = next(o for o in outcomes if o["outcome_id"] == alloc.outcome_id)
                            allocs.append({
                                "market_id": matching_m["market_id"],
                                "outcome_id": alloc.outcome_id,
                                "outcome_label": alloc.outcome_label,
                                "yes_price": alloc.yes_price,
                                "no_price": alloc.no_price,
                                "no_token_id": matching_m["no_token_id"],
                                "shares": alloc.shares,
                                "cost": alloc.cost,
                            })

                        signal = Signal(
                            market_id=primary_market.id,
                            signal_type=SignalType.NEGATIVE_RISK,
                            edge_pct=neg_risk_signal.net_edge,
                            confidence=0.95,
                            data_json=json.dumps({
                                "parent_question": parent_q,
                                "sum_yes_prices": neg_risk_signal.sum_yes_prices,
                                "gross_edge": neg_risk_signal.gross_edge,
                                "net_edge": neg_risk_signal.net_edge,
                                "total_cost": neg_risk_signal.total_cost,
                                "roi_pct": neg_risk_signal.roi_pct,
                                "allocations": allocs,
                            }),
                            status=SignalStatus.PENDING,
                        )
                        session.add(signal)
                        await session.commit()
                        await session.refresh(signal)

                        logger.info(
                            "[%s] Emitting NEG-RISK signal for event '%s': SumYes=%s, ROI=%s%%",
                            self.name, parent_q[:40], neg_risk_signal.sum_yes_prices, round(neg_risk_signal.roi_pct, 2)
                        )
                        neg_risk_count += 1

                        await self.emit_signal(
                            "signal_detected",
                            {
                                "signal_id": signal.id,
                                "market_id": primary_market.id,
                                "question": parent_q,
                                "type": SignalType.NEGATIVE_RISK.value,
                                "edge": neg_risk_signal.net_edge,
                                "confidence": 0.95,
                                "target_token_id": None,  # Handled inside executor since there are multiple tokens
                                "target_price": None,
                            }
                        )
            except Exception as e:
                logger.error("Error executing negative-risk strategy: %s", e, exc_info=True)

        logger.info(
            "[%s] Cycle completed. Internal Arb signals: %d, Neg-Risk signals: %d",
            self.name, binary_arb_count, neg_risk_count
        )
        await self.log_action("cycle_complete", {
            "binary_arb_count": binary_arb_count,
            "neg_risk_count": neg_risk_count,
        })
