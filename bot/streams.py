"""
Polymarket order book over websocket (exchange and Chainlink prices live in pricefeeds.py).
- PolyBookStream: order book of the Up/Down tokens on the Polymarket CLOB.
It reconnects on its own; if it drops, UpDownFeed falls back to REST.
"""
import asyncio
import json
import time
from typing import Callable, Dict, Optional, Set

import aiohttp
from loguru import logger

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolyBookStream:
    def __init__(self, on_message: Optional[Callable[[], None]] = None):
        self.on_message = on_message
        self.bids: Dict[str, Dict[float, float]] = {}
        self.asks: Dict[str, Dict[float, float]] = {}
        self.updated: Dict[str, float] = {}
        self.wanted: Set[str] = set()
        self.subscribed: Set[str] = set()
        self.last_msg = 0.0
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    @property
    def healthy(self) -> bool:
        return time.time() - self.last_msg < 5.0

    def want(self, tokens: Set[str]):
        self.wanted = set(tokens)

    async def sync(self):
        """Send pending subscribe/unsubscribe updates (called from the maintenance loop)."""
        ws = self._ws
        if ws is None or ws.closed:
            return
        new = self.wanted - self.subscribed
        if new:
            await ws.send_json({"assets_ids": sorted(new), "operation": "subscribe"})
            self.subscribed |= new
        drop = self.subscribed - self.wanted
        if drop:
            await ws.send_json({"assets_ids": sorted(drop), "operation": "unsubscribe"})
            self.subscribed -= drop
            for t in drop:
                self.bids.pop(t, None)
                self.asks.pop(t, None)
                self.updated.pop(t, None)

    def top(self, token: str) -> Optional[dict]:
        bids, asks = self.bids.get(token), self.asks.get(token)
        if not bids and not asks:
            return None
        ask = min(asks) if asks else None
        bid = max(bids) if bids else None
        return {
            "ask": ask, "ask_size": asks[ask] if ask is not None else 0.0,
            "bid": bid, "ts": self.updated.get(token, 0.0),
        }

    def _apply(self, e: dict, now: float):
        kind = e.get("event_type")
        if kind == "book":
            a = str(e.get("asset_id"))
            self.bids[a] = {float(x["price"]): float(x["size"]) for x in e.get("bids", [])}
            self.asks[a] = {float(x["price"]): float(x["size"]) for x in e.get("asks", [])}
            self.updated[a] = now
        elif kind == "price_change":
            for c in e.get("price_changes", []):
                a = str(c.get("asset_id"))
                book = (self.bids if c.get("side") == "BUY" else self.asks).setdefault(a, {})
                price, size = float(c["price"]), float(c["size"])
                if size <= 0:
                    book.pop(price, None)
                else:
                    book[price] = size
                self.updated[a] = now

    async def _pinger(self, ws):
        while not ws.closed:
            await asyncio.sleep(9)
            try:
                await ws.send_str("PING")
            except Exception:
                return

    async def run(self):
        backoff = 1.0
        while True:
            while not self.wanted:
                await asyncio.sleep(0.2)
            pinger = None
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(POLY_WS, heartbeat=None) as ws:
                        self._ws = ws
                        backoff = 1.0
                        initial = sorted(self.wanted)
                        await ws.send_json({"type": "market", "assets_ids": initial})
                        self.subscribed = set(initial)
                        pinger = asyncio.create_task(self._pinger(ws))
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                if msg.data in ("PONG", "PING"):
                                    continue
                                now = time.time()
                                self.last_msg = now
                                try:
                                    j = json.loads(msg.data)
                                except ValueError:
                                    continue
                                for e in (j if isinstance(j, list) else [j]):
                                    if isinstance(e, dict):
                                        self._apply(e, now)
                                if self.on_message:
                                    self.on_message()
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug(f"polymarket ws: {ex}")
            finally:
                self._ws = None
                self.subscribed = set()
                if pinger:
                    pinger.cancel()
            await asyncio.sleep(min(backoff, 10.0))
            backoff *= 2
