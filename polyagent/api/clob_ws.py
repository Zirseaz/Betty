import asyncio
import json
import logging
from typing import Any, Dict, Set
import websockets
from polyagent.config import Settings

logger = logging.getLogger(__name__)

class OrderBookCache:
    """In-memory cache for Polymarket Order Books using WebSockets."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ws_url = settings.polymarket_ws_url
        self.books: Dict[str, Dict[str, Any]] = {}
        self._running = False
        self._ws_task = None
        self._subscribed_tokens: Set[str] = set()
        self._ws = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self):
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()

    def get_midpoint(self, token_id: str) -> float:
        book = self.books.get(token_id)
        if not book:
            return 0.0
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        
        if best_bid and best_ask:
            return (best_bid + best_ask) / 2
        return best_bid or best_ask

    def get_best_ask(self, token_id: str) -> float:
        book = self.books.get(token_id)
        if not book:
            return 0.0
        asks = book.get("asks", [])
        return float(asks[0]["price"]) if asks else 0.0

    def get_best_bid(self, token_id: str) -> float:
        book = self.books.get(token_id)
        if not book:
            return 0.0
        bids = book.get("bids", [])
        return float(bids[0]["price"]) if bids else 0.0

    def get_book(self, token_id: str) -> Dict[str, Any]:
        return self.books.get(token_id, {"bids": [], "asks": []})

    async def subscribe(self, token_ids: list[str]):
        new_tokens = set(token_ids) - self._subscribed_tokens
        if not new_tokens:
            return
        self._subscribed_tokens.update(new_tokens)
        
        if self._ws and self._ws.open:
            try:
                sub_msg = {"type": "market", "assets_ids": list(new_tokens)}
                await self._ws.send(json.dumps(sub_msg))
            except Exception as e:
                logger.error(f"Error subscribing to new tokens: {e}")

    async def _ws_loop(self):
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        while self._running:
            try:
                async with websockets.connect(self.ws_url, ssl=ssl_context) as ws:
                    self._ws = ws
                    logger.info("Connected to Polymarket CLOB WS")
                    
                    if self._subscribed_tokens:
                        sub_msg = {"type": "market", "assets_ids": list(self._subscribed_tokens)}
                        await ws.send(json.dumps(sub_msg))
                    
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("event") == "price_change":
                            for item in data.get("changes", []):
                                token_id = item.get("asset_id")
                                if token_id:
                                    if token_id not in self.books:
                                        self.books[token_id] = {"bids": [], "asks": []}
                                    
                                    side = item.get("side")
                                    price = item.get("price")
                                    size = item.get("size")
                                    
                                    if side == "BUY":
                                        self.books[token_id]["bids"] = [{"price": price, "size": size}]
                                    elif side == "SELL":
                                        self.books[token_id]["asks"] = [{"price": price, "size": size}]
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polymarket WS error: {e}")
                self._ws = None
                await asyncio.sleep(5)

_global_cache = None

def get_global_cache(settings: Settings) -> OrderBookCache:
    global _global_cache
    if _global_cache is None:
        _global_cache = OrderBookCache(settings)
    return _global_cache

