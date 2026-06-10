"""Dedicated async client for the Polymarket Gamma API (market discovery).

The Gamma API provides event and market metadata that is not available on the
CLOB.  All calls are async using ``httpx``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class GammaClient:
    """Lightweight async wrapper around the Polymarket Gamma REST API.

    Usage::

        async with GammaClient() as gamma:
            events = await gamma.get_events(tag="politics")
    """

    BASE_URL: str = "https://gamma-api.polymarket.com"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout,
            headers={"Accept": "application/json"},
            verify=False,
        )

    async def __aenter__(self) -> "GammaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Gracefully close the underlying HTTP session."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Issue a GET request and return decoded JSON.

        Raises:
            httpx.HTTPStatusError: on non-2xx responses.
        """
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def get_events(
        self,
        tag: str | None = None,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch events from the Gamma API.

        Args:
            tag: Optional category / tag filter (e.g. ``"politics"``).
            status: Event status filter.  Defaults to ``"active"``.
            limit: Maximum number of events to return.
            offset: Pagination offset.

        Returns:
            A list of event dictionaries.
        """
        params: dict[str, Any] = {
            "closed": str(status != "active").lower(),
            "limit": limit,
            "offset": offset,
        }
        if tag:
            params["tag"] = tag

        logger.debug("Gamma: GET /events  params=%s", params)
        data = await self._get("/events", params=params)

        # Gamma returns a list directly for /events
        if isinstance(data, list):
            return data
        # Some endpoints wrap in a dict with a data key
        return data.get("data", data.get("events", []))

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch markets (binary outcomes) from Gamma.

        Args:
            limit: Page size.
            offset: Pagination offset.
            active: If ``True``, return only active (non-closed) markets.
            order: Sort field.
            ascending: Sort direction.

        Returns:
            List of market dicts with fields like ``condition_id``,
            ``question``, ``outcomes``, ``outcomePrices``, etc.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }

        logger.debug("Gamma: GET /markets  params=%s", params)
        data = await self._get("/markets", params=params)

        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Fetch a single market by its ``condition_id``.

        Args:
            condition_id: The market condition identifier.

        Returns:
            Market metadata dict.

        Raises:
            httpx.HTTPStatusError: if the market is not found (404).
        """
        logger.debug("Gamma: GET /markets/%s", condition_id)
        data = await self._get(f"/markets/{condition_id}")
        return data  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_markets(
        self,
        query: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Full-text search for markets by keyword.

        Args:
            query: Search string.
            limit: Maximum results.

        Returns:
            List of matching market dicts.
        """
        params: dict[str, Any] = {"query": query, "limit": limit}
        logger.debug("Gamma: GET /markets (search) params=%s", params)
        data = await self._get("/markets", params=params)

        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))
