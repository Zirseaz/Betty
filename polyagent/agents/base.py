"""Base agent class and signal bus implementation for PolyAgent."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Set

from sqlalchemy.ext.asyncio import AsyncSession
from polyagent.config import Settings
from polyagent.database import get_session_factory
from polyagent.models import AgentLog

logger = logging.getLogger(__name__)


# ── Signal Bus (Publish-Subscribe Pattern) ─────────────────────────

class SignalBus:
    """In-memory publish-subscribe event bus for inter-agent communication."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, Set[Callable[[Any], Coroutine[Any, Any, None]]]] = {}

    def subscribe(self, topic: str, callback: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
        """Register a callback for a specific signal topic."""
        if topic not in self._subscribers:
            self._subscribers[topic] = set()
        self._subscribers[topic].add(callback)
        logger.debug("Subscriber registered for topic: %s", topic)

    def unsubscribe(self, topic: str, callback: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
        """Remove a callback for a topic."""
        if topic in self._subscribers:
            self._subscribers[topic].discard(callback)

    async def publish(self, topic: str, data: Any) -> None:
        """Asynchronously dispatch data to all subscribers of a topic."""
        if topic not in self._subscribers or not self._subscribers[topic]:
            return

        callbacks = list(self._subscribers[topic])
        tasks = []
        for callback in callbacks:
            tasks.append(self._safe_dispatch(callback, data))
        
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_dispatch(self, callback: Callable[[Any], Coroutine[Any, Any, None]], data: Any) -> None:
        """Safely call a subscriber callback, handling exceptions."""
        try:
            await callback(data)
        except Exception as e:
            logger.error("Error in signal bus callback: %s", e, exc_info=True)


# Global Signal Bus singleton
signal_bus = SignalBus()


# ── Base Agent ───────────────────────────────────────────────────

class BaseAgent(ABC):
    """Abstract base class for all PolyAgent autonomous agents.
    
    Provides lifecycle hooks, periodic execution, structured logging, and DB audit logging.
    """

    name: str = "BaseAgent"

    def __init__(self, settings: Settings, interval_seconds: int = 30) -> None:
        self.settings = settings
        self.interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._session_factory = get_session_factory()
        
        # Performance/diagnostic metrics
        self.cycle_count = 0
        self.error_count = 0
        self.last_run_time: datetime | None = None

    async def setup(self) -> None:
        """One-time agent initialization (e.g. database warmup, API auth)."""
        logger.info("[%s] Setup completed", self.name)

    async def teardown(self) -> None:
        """Cleanup handler on shutdown."""
        logger.info("[%s] Teardown completed", self.name)

    @abstractmethod
    async def run_cycle(self) -> None:
        """Main execution logic for the agent. Called periodically by start()."""
        pass

    async def log_action(self, action: str, details: dict[str, Any] | None = None) -> None:
        """Log an agent action to the database for historical auditing."""
        details_str = json.dumps(details) if details else None
        
        async with self._session_factory() as session:
            try:
                log_entry = AgentLog(
                    agent_name=self.name,
                    action=action,
                    details_json=details_str,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(log_entry)
                await session.commit()
            except Exception as e:
                logger.error("[%s] Failed to log action to DB: %s", self.name, e)
                await session.rollback()

    async def emit_signal(self, topic: str, data: Any) -> None:
        """Publish a signal to the global event bus."""
        logger.debug("[%s] Emitting signal on topic '%s': %s", self.name, topic, data)
        await signal_bus.publish(topic, data)

    def subscribe_to(self, topic: str, callback: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
        """Convenience method to subscribe this agent to a topic."""
        signal_bus.subscribe(topic, callback)

    async def start(self) -> None:
        """Starts the periodic execution loop of this agent."""
        if self._running:
            return
        self._running = True
        logger.info("[%s] Starting agent loop with interval %ds", self.name, self.interval)
        await self.setup()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stops the agent loop and runs cleanup."""
        if not self._running:
            return
        self._running = False
        logger.info("[%s] Stopping agent loop", self.name)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.teardown()

    async def _loop(self) -> None:
        """Infinite loop executing the agent cycle at defined intervals."""
        while self._running:
            start_time = asyncio.get_event_loop().time()
            try:
                self.cycle_count += 1
                self.last_run_time = datetime.now(timezone.utc)
                await self.run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.error_count += 1
                logger.error("[%s] Error in execution cycle: %s", self.name, e, exc_info=True)
                # Ensure we log the crash to the database
                try:
                    await self.log_action("cycle_error", {"error": str(e)})
                except Exception:
                    pass

            # Calculate sleep to maintain consistent interval
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(0.1, self.interval - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break
