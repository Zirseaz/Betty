"""Unified Polymarket client – wraps py-clob-client (CLOB) and httpx (Gamma).

This module provides a single entry-point for all Polymarket interactions:
market discovery, order-book queries, order management, and paper-trading
simulation.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from enum import Enum
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType as ClobOrderType
from py_clob_client.order_builder.constants import BUY, SELL

from polyagent.clients.gamma import GammaClient
from polyagent.config import Settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class PaperOrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


def _order_type_to_clob(order_type: str) -> ClobOrderType:
    """Map a human-readable order-type string to the py-clob-client enum."""
    mapping = {
        "GTC": ClobOrderType.GTC,
        "FOK": ClobOrderType.FOK,
        "GTD": ClobOrderType.GTD,
    }
    return mapping.get(order_type.upper(), ClobOrderType.GTC)


def _side_to_clob(side: str) -> str:
    """Map a human-readable side string to the py-clob-client constant."""
    return BUY if side.upper() == "BUY" else SELL


# ──────────────────────────────────────────────────────────────────────
# Main client
# ──────────────────────────────────────────────────────────────────────

class PolymarketClient:
    """Unified Polymarket client with paper-trading support.

    *Paper mode* (``settings.MODE == "paper"``) keeps an in-memory order book
    and simulates fills against live CLOB mid-point prices.  No real orders
    are sent in paper mode.
    """

    GAMMA_BASE = "https://gamma-api.polymarket.com"
    CLOB_BASE = "https://clob.polymarket.com"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._paper_mode: bool = settings.is_paper

        # ── Gamma (async) ───────────────────────────────────────────
        self._gamma = GammaClient()

        # ── CLOB (sync via py-clob-client) ──────────────────────────
        self._clob: ClobClient | None = None
        if not self._paper_mode:
            private_key = settings.poly_private_key or ""
            if not private_key:
                raise RuntimeError(
                    "LIVE mode requires a wallet private key (poly_private_key) "
                    "to sign transactions"
                )
            api_creds = ApiCreds(
                api_key=settings.poly_api_key or "",
                api_secret=settings.poly_api_secret or "",
                api_passphrase=settings.poly_api_passphrase or "",
            )
            self._clob = ClobClient(
                self.CLOB_BASE,
                key=private_key,  # wallet private key used for signing
                creds=api_creds,
                chain_id=137,  # Polygon mainnet
            )
            logger.info("PolymarketClient initialised in LIVE mode")
        else:
            # In paper mode we still need a read-only CLOB client for price queries.
            self._clob = ClobClient(self.CLOB_BASE, chain_id=137)
            logger.info("PolymarketClient initialised in PAPER mode")

        # ── Paper-trading state ─────────────────────────────────────
        self._paper_orders: dict[str, dict[str, Any]] = {}
        self._paper_positions: dict[str, dict[str, Any]] = {}
        self._paper_balance: float = settings.paper_balance
        self._paper_pnl: float = 0.0

    # ==================================================================
    # Market Discovery  (Gamma API – async)
    # ==================================================================

    async def get_active_markets(
        self,
        category: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return active markets, optionally filtered by category/tag.

        Args:
            category: Optional tag filter forwarded to the Gamma API.
            limit: Maximum number of markets to return.
        """
        if category:
            return await self._gamma.get_markets(limit=limit, active=True)
        return await self._gamma.get_markets(limit=limit, active=True)

    async def get_market_detail(self, condition_id: str) -> dict[str, Any]:
        """Fetch full metadata for a single market."""
        return await self._gamma.get_market(condition_id)

    async def search_markets(self, query: str) -> list[dict[str, Any]]:
        """Full-text search across market questions."""
        return await self._gamma.search_markets(query)

    # ==================================================================
    # Order Book & Pricing  (CLOB API – sync, run in executor)
    # ==================================================================

    def _ensure_clob(self) -> ClobClient:
        if self._clob is None:
            raise RuntimeError("ClobClient not initialised")
        return self._clob

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Return the full order book for a given token.

        This is a synchronous call that uses py-clob-client under the hood.
        """
        clob = self._ensure_clob()
        try:
            book = clob.get_order_book(token_id)
            return {
                "market": book.market,
                "asset_id": book.asset_id,
                "bids": [{"price": str(o.price), "size": str(o.size)} for o in book.bids] if book.bids else [],
                "asks": [{"price": str(o.price), "size": str(o.size)} for o in book.asks] if book.asks else [],
                "hash": book.hash,
                "timestamp": book.timestamp,
            }
        except Exception:
            logger.exception("Failed to fetch order book for token=%s", token_id)
            return {"bids": [], "asks": [], "market": token_id, "asset_id": token_id}

    def get_price(self, token_id: str, side: str) -> float:
        """Return best price for *side* (``BUY`` or ``SELL``).

        Returns ``0.0`` if the book is empty on the requested side.
        """
        clob = self._ensure_clob()
        try:
            book = clob.get_order_book(token_id)
            if side.upper() == "BUY":
                bids = book.bids or []
                return float(bids[0].price) if bids else 0.0
            else:
                asks = book.asks or []
                return float(asks[0].price) if asks else 0.0
        except Exception:
            logger.exception("get_price failed for token=%s side=%s", token_id, side)
            return 0.0

    def get_midpoint(self, token_id: str) -> float:
        """Return mid-point between best bid and best ask.

        Falls back to ``0.0`` if both sides are empty.
        """
        clob = self._ensure_clob()
        try:
            result = clob.get_midpoint(token_id)
            if result is None:
                return 0.0
            # py-clob-client returns a dict or string depending on version
            price = result.get("mid") if isinstance(result, dict) else result
            return float(price) if price is not None else 0.0
        except Exception:
            logger.exception("get_midpoint failed for token=%s", token_id)
            return 0.0

    async def get_order_book_async(self, token_id: str) -> dict[str, Any]:
        """Async wrapper around the synchronous order-book call."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_order_book, token_id)

    async def get_midpoint_async(self, token_id: str) -> float:
        """Async wrapper around get_midpoint."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_midpoint, token_id)

    async def get_price_async(self, token_id: str, side: str) -> float:
        """Async wrapper around get_price."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_price, token_id, side)

    # ==================================================================
    # Order Management
    # ==================================================================

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> dict[str, Any]:
        """Place an order (paper or live).

        Args:
            token_id: Asset / token identifier.
            side: ``"BUY"`` or ``"SELL"``.
            price: Limit price (0–1 range for binary markets).
            size: Order size in shares.
            order_type: ``"GTC"`` (default), ``"FOK"``, or ``"GTD"``.

        Returns:
            Order receipt dict with at minimum ``order_id`` and ``status``.
        """
        if self._paper_mode:
            return self._paper_place_order(token_id, side, price, size, order_type)
        return self._live_place_order(token_id, side, price, size, order_type)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID."""
        if self._paper_mode:
            return self._paper_cancel_order(order_id)
        return self._live_cancel_order(order_id)

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if self._paper_mode:
            return self._paper_cancel_all()
        return self._live_cancel_all()

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Return all open orders."""
        if self._paper_mode:
            return self._paper_get_open_orders()
        return self._live_get_open_orders()

    def get_positions(self) -> dict[str, dict[str, Any]]:
        """Return current positions (paper or live)."""
        if self._paper_mode:
            return dict(self._paper_positions)
        # Live: query CLOB API for positions
        clob = self._ensure_clob()
        try:
            # py-clob-client does not have a direct positions endpoint;
            # callers should track positions in the DB.  Return empty dict
            # for live to signal the caller should use DB records.
            return {}
        except Exception:
            logger.exception("get_positions failed")
            return {}

    # ------------------------------------------------------------------
    # Paper-trading internals
    # ------------------------------------------------------------------

    def _paper_place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str,
    ) -> dict[str, Any]:
        """Simulate an order against the live CLOB midpoint."""
        order_id = str(uuid.uuid4())
        cost = price * size
        ts = time.time()

        midpoint = self.get_midpoint(token_id)

        # Determine if order fills immediately (simplified matching)
        fills_immediately = False
        if side.upper() == "BUY" and midpoint > 0 and price >= midpoint:
            fills_immediately = True
        elif side.upper() == "SELL" and midpoint > 0 and price <= midpoint:
            fills_immediately = True

        # For FOK orders, fill or kill
        if order_type.upper() == "FOK" and not fills_immediately:
            logger.info("[PAPER] FOK order killed – no fill at price=%.4f mid=%.4f", price, midpoint)
            return {
                "order_id": order_id,
                "status": PaperOrderStatus.CANCELLED.value,
                "token_id": token_id,
                "side": side.upper(),
                "price": price,
                "size": size,
                "filled": 0.0,
                "timestamp": ts,
                "paper": True,
            }

        status = PaperOrderStatus.FILLED if fills_immediately else PaperOrderStatus.OPEN
        fill_price = midpoint if fills_immediately else 0.0
        filled_size = size if fills_immediately else 0.0

        order_record: dict[str, Any] = {
            "order_id": order_id,
            "token_id": token_id,
            "side": side.upper(),
            "price": price,
            "size": size,
            "order_type": order_type.upper(),
            "status": status.value,
            "fill_price": fill_price,
            "filled": filled_size,
            "timestamp": ts,
            "paper": True,
        }
        self._paper_orders[order_id] = order_record

        if fills_immediately:
            self._paper_update_position(token_id, side.upper(), filled_size, fill_price)
            logger.info(
                "[PAPER] Order FILLED: id=%s side=%s token=%s price=%.4f size=%.2f",
                order_id, side, token_id, fill_price, filled_size,
            )
        else:
            logger.info(
                "[PAPER] Order OPEN: id=%s side=%s token=%s price=%.4f size=%.2f",
                order_id, side, token_id, price, size,
            )

        return order_record

    def _paper_update_position(
        self, token_id: str, side: str, size: float, price: float
    ) -> None:
        """Update simulated position state after a fill."""
        pos = self._paper_positions.get(token_id, {
            "token_id": token_id,
            "size": 0.0,
            "avg_price": 0.0,
            "cost_basis": 0.0,
            "realized_pnl": 0.0,
        })

        if side == "BUY":
            new_size = pos["size"] + size
            if new_size != 0:
                pos["avg_price"] = (
                    (pos["avg_price"] * pos["size"]) + (price * size)
                ) / new_size
            pos["size"] = new_size
            pos["cost_basis"] += price * size
            self._paper_balance -= price * size
        else:  # SELL
            # Realise PnL
            pnl = (price - pos["avg_price"]) * size
            pos["realized_pnl"] += pnl
            pos["size"] -= size
            pos["cost_basis"] -= pos["avg_price"] * size
            self._paper_balance += price * size
            self._paper_pnl += pnl

        if abs(pos["size"]) < 1e-9:
            self._paper_positions.pop(token_id, None)
        else:
            self._paper_positions[token_id] = pos

    def _paper_cancel_order(self, order_id: str) -> bool:
        order = self._paper_orders.get(order_id)
        if order and order["status"] == PaperOrderStatus.OPEN.value:
            order["status"] = PaperOrderStatus.CANCELLED.value
            logger.info("[PAPER] Order cancelled: %s", order_id)
            return True
        return False

    def _paper_cancel_all(self) -> bool:
        cancelled = 0
        for oid, order in self._paper_orders.items():
            if order["status"] == PaperOrderStatus.OPEN.value:
                order["status"] = PaperOrderStatus.CANCELLED.value
                cancelled += 1
        logger.info("[PAPER] Cancelled %d open orders", cancelled)
        return True

    def _paper_get_open_orders(self) -> list[dict[str, Any]]:
        return [
            o for o in self._paper_orders.values()
            if o["status"] == PaperOrderStatus.OPEN.value
        ]

    def paper_check_fills(self) -> list[dict[str, Any]]:
        """Check open paper orders against current midpoints and fill if matched.

        Returns list of newly-filled order records.
        """
        filled: list[dict[str, Any]] = []
        for order in list(self._paper_orders.values()):
            if order["status"] != PaperOrderStatus.OPEN.value:
                continue
            mid = self.get_midpoint(order["token_id"])
            if mid <= 0:
                continue

            should_fill = False
            if order["side"] == "BUY" and mid <= order["price"]:
                should_fill = True
            elif order["side"] == "SELL" and mid >= order["price"]:
                should_fill = True

            if should_fill:
                order["status"] = PaperOrderStatus.FILLED.value
                order["fill_price"] = mid
                order["filled"] = order["size"]
                self._paper_update_position(
                    order["token_id"], order["side"], order["size"], mid
                )
                filled.append(order)
                logger.info(
                    "[PAPER] Resting order filled: id=%s at %.4f", order["order_id"], mid
                )
        return filled

    @property
    def paper_balance(self) -> float:
        return self._paper_balance

    @property
    def paper_pnl(self) -> float:
        return self._paper_pnl

    # ------------------------------------------------------------------
    # Live order internals
    # ------------------------------------------------------------------

    def _live_place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str,
    ) -> dict[str, Any]:
        """Place a real order via py-clob-client."""
        clob = self._ensure_clob()
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=_side_to_clob(side),
            )
            clob_ot = _order_type_to_clob(order_type)
            signed = clob.create_and_post_order(order_args, order_type=clob_ot)
            logger.info(
                "[LIVE] Order placed: side=%s token=%s price=%.4f size=%.2f type=%s result=%s",
                side, token_id, price, size, order_type, signed,
            )
            return {
                "order_id": signed.get("orderID", signed.get("id", "")),
                "status": signed.get("status", "UNKNOWN"),
                "token_id": token_id,
                "side": side.upper(),
                "price": price,
                "size": size,
                "order_type": order_type,
                "raw": signed,
                "paper": False,
            }
        except Exception as exc:
            logger.exception("Live order placement failed")
            return {
                "order_id": "",
                "status": "ERROR",
                "error": str(exc),
                "token_id": token_id,
                "paper": False,
            }

    def _live_cancel_order(self, order_id: str) -> bool:
        clob = self._ensure_clob()
        try:
            result = clob.cancel(order_id)
            logger.info("[LIVE] Cancel order %s  result=%s", order_id, result)
            return True
        except Exception:
            logger.exception("Live cancel failed for order %s", order_id)
            return False

    def _live_cancel_all(self) -> bool:
        clob = self._ensure_clob()
        try:
            result = clob.cancel_all()
            logger.info("[LIVE] Cancel all orders  result=%s", result)
            return True
        except Exception:
            logger.exception("Live cancel_all failed")
            return False

    def _live_get_open_orders(self) -> list[dict[str, Any]]:
        clob = self._ensure_clob()
        try:
            orders = clob.get_orders()
            if isinstance(orders, list):
                return orders
            return []
        except Exception:
            logger.exception("Failed to fetch live open orders")
            return []

    # ==================================================================
    # Lifecycle
    # ==================================================================

    async def close(self) -> None:
        """Shut down HTTP clients gracefully."""
        await self._gamma.close()
        logger.info("PolymarketClient closed")
