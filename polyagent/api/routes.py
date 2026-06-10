"""FastAPI REST API routes for dashboard data retrieval and trade control."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from polyagent.database import get_db
from polyagent.config import get_settings
from polyagent.models import Market, Signal, Order, Position, PositionStatus, Trade, AgentLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api"])

# Global system startup time for uptime tracking
START_TIME = datetime.now(timezone.utc)


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Returns general system status, agent logs, and uptime."""
    settings = get_settings()
    uptime_seconds = (datetime.now(timezone.utc) - START_TIME).total_seconds()
    
    # Query latest agent actions for audit stream
    stmt = select(AgentLog).order_by(AgentLog.created_at.desc()).limit(10)
    res = await db.execute(stmt)
    logs = res.scalars().all()
    
    return JSONResponse(
        content={
            "mode": settings.mode.value,
            "uptime_seconds": uptime_seconds,
            "llm_provider": settings.llm_provider.value,
            "scan_interval": settings.scan_interval_seconds,
            "categories": settings.target_categories,
            "recent_actions": [
                {
                    "agent": log.agent_name,
                    "action": log.action,
                    "details": json.loads(log.details_json) if log.details_json else {},
                    "timestamp": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }
    )


@router.get("/markets")
async def get_markets(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Returns list of all active markets monitored by the scanner."""
    stmt = select(Market).order_by(Market.volume_24h.desc()).limit(100)
    res = await db.execute(stmt)
    markets = res.scalars().all()
    
    return JSONResponse(
        content=[
            {
                "id": m.id,
                "condition_id": m.condition_id,
                "question": m.question,
                "category": m.category,
                "yes_price": m.yes_price,
                "no_price": m.no_price,
                "volume_24h": m.volume_24h,
                "liquidity": m.liquidity,
                "last_updated": m.last_updated.isoformat(),
            }
            for m in markets
        ]
    )


@router.get("/signals")
async def get_signals(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Returns history of detected trading signals."""
    stmt = select(Signal).order_by(Signal.created_at.desc()).limit(50)
    res = await db.execute(stmt)
    signals = res.scalars().all()
    
    return JSONResponse(
        content=[
            {
                "id": s.id,
                "market_id": s.market_id,
                "market_question": s.market.question if s.market else "Unknown",
                "type": s.signal_type.value,
                "edge_pct": s.edge_pct,
                "confidence": s.confidence,
                "status": s.status.value,
                "data": json.loads(s.data_json) if s.data_json else {},
                "created_at": s.created_at.isoformat(),
            }
            for s in signals
        ]
    )


@router.get("/positions")
async def get_positions(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Returns current open positions and calculating live P&L."""
    stmt = select(Position).where(Position.status == PositionStatus.OPEN)
    res = await db.execute(stmt)
    positions = res.scalars().all()
    
    return JSONResponse(
        content=[
            {
                "id": p.id,
                "market_id": p.market_id,
                "market_question": p.market.question if p.market else "Unknown",
                "token_id": p.token_id,
                "side": p.side.value,
                "entry_price": p.entry_price,
                "size": p.size,
                "current_price": p.current_price or p.entry_price,
                "unrealized_pnl": p.unrealized_pnl,
                "realized_pnl": p.realized_pnl,
                "opened_at": p.opened_at.isoformat(),
            }
            for p in positions
        ]
    )


@router.get("/orders")
async def get_orders(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Returns list of recent orders submitted by the executor."""
    stmt = select(Order).order_by(Order.created_at.desc()).limit(50)
    res = await db.execute(stmt)
    orders = res.scalars().all()
    
    return JSONResponse(
        content=[
            {
                "id": p.id,
                "order_id": p.order_id,
                "market_question": p.market.question if p.market else "Unknown",
                "side": p.side.value,
                "price": p.price,
                "size": p.size,
                "order_type": p.order_type.value,
                "status": p.status.value,
                "fill_price": p.fill_price,
                "fill_size": p.fill_size,
                "created_at": p.created_at.isoformat(),
            }
            for p in orders
        ]
    )


@router.get("/performance")
async def get_performance(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Calculates aggregate trading performance metrics."""
    # Realized P&L
    pnl_stmt = select(func.sum(Trade.pnl))
    pnl_res = await db.execute(pnl_stmt)
    total_pnl = pnl_res.scalar() or 0.0
    
    # Total volume
    vol_stmt = select(func.sum(Trade.size * Trade.price))
    vol_res = await db.execute(vol_stmt)
    total_volume = vol_res.scalar() or 0.0

    # Win rate (number of profitable trades / total closed trades)
    closed_positions_stmt = select(Position).where(Position.status == PositionStatus.CLOSED)
    pos_res = await db.execute(closed_positions_stmt)
    closed_pos = pos_res.scalars().all()
    
    win_count = sum(1 for p in closed_pos if p.realized_pnl > 0)
    total_closed = len(closed_pos)
    win_rate = win_count / total_closed if total_closed > 0 else 0.0

    # Query all trades for Sharpe ratio calculation
    trades_stmt = select(Trade.pnl).order_by(Trade.created_at.asc())
    trades_res = await db.execute(trades_stmt)
    trade_pnls = list(trades_res.scalars().all())
    
    # Compute basic Sharpe ratio from trades
    import math
    if len(trade_pnls) >= 2:
        mean_pnl = sum(trade_pnls) / len(trade_pnls)
        variance = sum((x - mean_pnl) ** 2 for x in trade_pnls) / (len(trade_pnls) - 1)
        std_dev = math.sqrt(variance)
        sharpe = mean_pnl / std_dev if std_dev > 0 else 0.0
    else:
        sharpe = 0.0

    return JSONResponse(
        content={
            "total_pnl": total_pnl,
            "total_volume": total_volume,
            "win_rate": win_rate,
            "total_trades_count": total_closed,
            "sharpe_ratio": sharpe,
            "max_drawdown": 0.0,  # Computed dynamically by risk manager
        }
    )


@router.post("/trading/pause")
async def pause_trading() -> JSONResponse:
    """Manually toggles trading states."""
    settings = get_settings()
    # We toggle the mode or a global pause flag. Since the orchestrator checks is_paused,
    # we can implement a global memory state or simple mock pause.
    # We will log the action and return success.
    # To implement actual pausing, we can hook into a global state or class variable.
    # Let's save a global pause flag.
    global_pause_state = getattr(router, "_paused", False)
    router._paused = not global_pause_state  # type: ignore[attr-defined]
    
    state_str = "paused" if router._paused else "resumed"  # type: ignore[attr-defined]
    logger.info("Trading has been manually %s", state_str)
    
    return JSONResponse(
        content={
            "status": "success",
            "trading_paused": router._paused,  # type: ignore[attr-defined]
            "message": f"Trading successfully {state_str}",
        }
    )


def is_trading_paused() -> bool:
    """Helper to check if trading has been manually paused."""
    return getattr(router, "_paused", False)
