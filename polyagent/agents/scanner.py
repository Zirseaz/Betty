"""Scanner Agent for discovering and updating Polymarket prediction markets."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from sqlalchemy import select

from polyagent.agents.base import BaseAgent
from polyagent.clients.polymarket import PolymarketClient
from polyagent.config import Settings
from polyagent.models import Market, MarketStatus, Signal, SignalType, SignalStatus
from polyagent.signals.price import detect_price_discrepancy
from polyagent.signals.orderbook import detect_orderbook_imbalance
from polyagent.signals.volume import detect_volume_spike

logger = logging.getLogger(__name__)


class ScannerAgent(BaseAgent):
    """Scans Polymarket Gamma API for active markets.
    
    Updates prices, volume, and liquidity in the database, and emits raw signals
    for internal YES/NO arbitrage, volume spikes, and order book imbalance.
    """

    name = "ScannerAgent"

    def __init__(self, settings: Settings) -> None:
        # Default to 30 second scan interval or from settings
        super().__init__(settings, interval_seconds=settings.scan_interval_seconds)
        self.client = PolymarketClient(settings)

    async def setup(self) -> None:
        await super().setup()
        await self.log_action("started", {"interval": self.interval})

    async def teardown(self) -> None:
        await self.client.close()
        await self.log_action("stopped")
        await super().teardown()

    async def run_cycle(self) -> None:
        logger.info("[%s] Beginning market scan cycle...", self.name)
        
        # 1. Fetch active markets from Gamma API
        try:
            raw_markets = await self.client.get_active_markets(limit=50)
            logger.info("[%s] Retrieved %d active markets from Gamma", self.name, len(raw_markets))
        except Exception as e:
            logger.error("[%s] Failed to fetch active markets: %s", self.name, e)
            return

        # 2. Filter and update/insert markets in the database
        target_categories = self.settings.category_list
        scanned_count = 0
        signal_count = 0

        async with self._session_factory() as session:
            for raw_m in raw_markets:
                condition_id = raw_m.get("conditionId")
                if not condition_id:
                    continue

                category = raw_m.get("category", "")
                # Filter by category if target_categories is set
                if target_categories and category.lower() not in target_categories:
                    continue

                question = raw_m.get("question", "")
                volume_24h = float(raw_m.get("volume24hr", 0.0))
                liquidity = float(raw_m.get("liquidity", 0.0))

                # Parse token IDs from Gamma response
                token_ids_raw = raw_m.get("clobTokenIds")
                yes_token_id = None
                no_token_id = None
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                        if len(token_ids) >= 2:
                            yes_token_id = token_ids[0]
                            no_token_id = token_ids[1]
                    except Exception:
                        pass
                elif isinstance(token_ids_raw, list) and len(token_ids_raw) >= 2:
                    yes_token_id = token_ids_raw[0]
                    no_token_id = token_ids_raw[1]

                if not yes_token_id or not no_token_id:
                    continue

                # Query database for existing market
                stmt = select(Market).where(Market.condition_id == condition_id)
                res = await session.execute(stmt)
                market = res.scalar_one_or_none()

                # Get prices from CLOB
                yes_price = 0.0
                no_price = 0.0
                try:
                    # Async calls to fetch midpoint prices
                    yes_price = await self.client.get_midpoint_async(yes_token_id)
                    no_price = await self.client.get_midpoint_async(no_token_id)
                except Exception as e:
                    logger.debug("Failed to get prices for condition %s: %s", condition_id, e)
                    # Fallback to Gamma prices if CLOB fails
                    outcome_prices = raw_m.get("outcomePrices")
                    if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
                        try:
                            yes_price = float(outcome_prices[0])
                            no_price = float(outcome_prices[1])
                        except ValueError:
                            pass

                # If prices are 0, skip this market
                if yes_price <= 0.0 or no_price <= 0.0:
                    continue

                if market is None:
                    market = Market(
                        condition_id=condition_id,
                        question=question,
                        category=category,
                        yes_token_id=yes_token_id,
                        no_token_id=no_token_id,
                        yes_price=yes_price,
                        no_price=no_price,
                        volume_24h=volume_24h,
                        liquidity=liquidity,
                        status=MarketStatus.ACTIVE,
                    )
                    session.add(market)
                    logger.info("[%s] Adding new market: %s", self.name, question[:50])
                else:
                    market.yes_price = yes_price
                    market.no_price = no_price
                    market.volume_24h = volume_24h
                    market.liquidity = liquidity
                    market.last_updated = datetime.now(timezone.utc)

                scanned_count += 1

                # 3. Anomaly check & signal emission
                # A. Internal YES/NO Arbitrage (gross YES+NO < 1.0)
                sum_prices = yes_price + no_price
                if sum_prices < 1.0:
                    # Potential internal arb detected
                    edge = 1.0 - sum_prices
                    # Save signal to database
                    signal = Signal(
                        market_id=market.id,
                        signal_type=SignalType.INTERNAL_ARB,
                        edge_pct=edge,
                        confidence=0.8,
                        data_json=json.dumps({
                            "yes_price": yes_price,
                            "no_price": no_price,
                            "yes_token_id": yes_token_id,
                            "no_token_id": no_token_id,
                        }),
                        status=SignalStatus.PENDING,
                    )
                    session.add(signal)
                    signal_count += 1
                    
                    # Emit signal via pub/sub bus
                    # We commit to get the signal.id first
                    await session.commit()
                    # Re-select the market & signal to avoid session detached issues
                    await session.refresh(signal)
                    await session.refresh(market)
                    
                    await self.emit_signal(
                        "signal_detected",
                        {
                            "signal_id": signal.id,
                            "market_id": market.id,
                            "question": market.question,
                            "type": SignalType.INTERNAL_ARB.value,
                            "edge": edge,
                            "confidence": 0.8,
                            "yes_token_id": yes_token_id,
                            "no_token_id": no_token_id,
                            "yes_price": yes_price,
                            "no_price": no_price,
                        }
                    )

                # B. Price Discrepancies
                price_anomaly = detect_price_discrepancy(yes_price, no_price)
                if price_anomaly:
                    await self.emit_signal("price_anomaly", {
                        "condition_id": condition_id,
                        "question": question,
                        "anomaly": price_anomaly.__dict__,
                    })

                # C. Orderbook Imbalance (only run on higher volume markets to save bandwidth)
                if volume_24h > 10_000:
                    try:
                        book = await self.client.get_order_book_async(yes_token_id)
                        imbalance = detect_orderbook_imbalance(book.get("bids", []), book.get("asks", []))
                        if imbalance and abs(imbalance.imbalance_ratio) >= 0.5:
                            await self.emit_signal("orderbook_imbalance", {
                                "condition_id": condition_id,
                                "question": question,
                                "imbalance": imbalance.__dict__,
                            })
                    except Exception:
                        pass

            # Flush everything remaining to database
            await session.commit()

        # Let's run a paper fill check as part of the scanner cycle
        if self.settings.is_paper:
            try:
                filled_orders = self.client.paper_check_fills()
                for fo in filled_orders:
                    await self.emit_signal("paper_order_filled", fo)
            except Exception as e:
                logger.error("[%s] Error checking paper fills: %s", self.name, e)

        logger.info(
            "[%s] Cycle completed. Scanned: %d, Signals generated: %d",
            self.name, scanned_count, signal_count
        )
        await self.log_action("cycle_complete", {
            "scanned_count": scanned_count,
            "signal_count": signal_count,
        })
