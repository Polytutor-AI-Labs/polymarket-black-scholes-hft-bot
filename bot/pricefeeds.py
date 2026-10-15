"""
Live price sources.

- Chainlink (Polymarket RTDS, ~1 Hz): the price Up/Down markets RESOLVE on.
  It supplies the exact window reference and the real last-minute average.
- Exchanges (Binance, Coinbase, Kraken, Bybit, OKX, Bitget): they lead Chainlink; their median
  estimates where Chainlink will move over the next few seconds.

`PriceHub` merges them: estimated price = median of fresh exchanges, corrected by the
Chainlink/exchange basis (moving average), so no single exchange can distort it.
"""
import asyncio
import json
import time
from collections import deque
from statistics import median
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp
from loguru import logger

STALE_S = 3.0            # an exchange with no data for 3 s does not count
BASIS_ALPHA = 0.15       # smoothing of the Chainlink/exchange basis


class PriceHub:
    def __init__(self, assets: List[str]):
        self.assets = list(assets)
        self.ex: Dict[str, Dict[str, Tuple[float, float]]] = {a: {} for a in self.assets}   # asset -> exchange -> (mid, ts)
        self.cl: Dict[str, deque] = {a: deque(maxlen=20000) for a in self.assets}            # (report_ts_s, value)
        self.basis: Dict[str, float] = {a: 0.0 for a in self.assets}                         # Chainlink/composite - 1
        self.basis_ready: Dict[str, bool] = {a: False for a in self.assets}
        self.last_msg: Dict[str, float] = {}                                                 # source -> last message
        self.cl_first_ts: Dict[str, float] = {}
        self.on_update: Optional[Callable[[], None]] = None

    # ── ingest ─────────────────────────────────────────────────────────────
    def on_exchange(self, name: str, asset: str, mid: float, ts: float):
        if asset not in self.ex or mid <= 0:
            return
        self.ex[asset][name] = (mid, ts)
        self.last_msg[name] = ts
        if self.on_update:
            self.on_update()

    def on_chainlink(self, asset: str, value: float, report_ts: float, now: float):
        if asset not in self.cl or value <= 0:
            return
        self.cl[asset].append((report_ts, value))
        self.cl_first_ts.setdefault(asset, report_ts)
        self.last_msg["chainlink"] = now
        comp = self.composite(asset, now)
        if comp:
            rel = value / comp - 1.0
            if self.basis_ready[asset]:
                self.basis[asset] += BASIS_ALPHA * (rel - self.basis[asset])
            else:
                self.basis[asset], self.basis_ready[asset] = rel, True
        if self.on_update:
            self.on_update()

    # ── queries ────────────────────────────────────────────────────────────
    def fresh(self, asset: str, now: float) -> Dict[str, float]:
        return {n: p for n, (p, t) in self.ex.get(asset, {}).items() if now - t <= STALE_S}

    def composite(self, asset: str, now: Optional[float] = None) -> Optional[float]:
        now = now or time.time()
        vals = list(self.fresh(asset, now).values())
        return median(vals) if vals else None

    def chainlink_last(self, asset: str) -> Optional[float]:
        q = self.cl.get(asset)
        return q[-1][1] if q else None

    def est_spot(self, asset: str, now: Optional[float] = None) -> Optional[float]:
        """Best current estimate of the Chainlink price: leading exchanges plus the basis."""
        now = now or time.time()
        comp = self.composite(asset, now)
        if comp is None:
            return self.chainlink_last(asset)
        return comp * (1.0 + self.basis[asset]) if self.basis_ready[asset] else comp

    def ref_at(self, asset: str, ts: float) -> Optional[float]:
        """Last Chainlink price reported at or before `ts` (None if the bot had not seen it yet)."""
        q = self.cl.get(asset)
        if not q or self.cl_first_ts.get(asset, 1e18) > ts + 0.5:
            return None
        best = None
        for t, v in q:
            if t <= ts:
                best = (t, v)
            else:
                break
        if best is None or ts - best[0] > 15:      # gap too large: the point is not reliable
            return None
        return best[1]

    def twap_obs(self, asset: str, start_ts: float, end_ts: float) -> Optional[float]:
        """Average of Chainlink prices reported between start_ts and end_ts."""
        q = self.cl.get(asset)
        if not q:
            return None
        vals = []
        for t, v in reversed(q):
            if t < start_ts:
                break
            if t <= end_ts:
                vals.append(v)
        return sum(vals) / len(vals) if vals else None

    def sources_ok(self, now: Optional[float] = None) -> Tuple[int, int, bool]:
        """(exchanges with fresh data, known exchanges, chainlink fresh)."""
        now = now or time.time()
        exch = [t for n, t in self.last_msg.items() if n != "chainlink"]
        return (sum(1 for t in exch if now - t < STALE_S), len(exch), now - self.last_msg.get("chainlink", 0) < STALE_S)

    def lag_ms(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        ages = [now - t for n, t in self.last_msg.items() if n != "chainlink"]
        return min(ages) * 1000.0 if ages else 0.0


# ─────────────────────────── exchanges ───────────────────────────────────────
def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _mid(bid, ask, last) -> Optional[float]:
    b, a = _f(bid), _f(ask)
    if b and a:
        return (b + a) / 2.0
    return _f(last)


class ExchangeSpec:
    """name, url, {asset: native symbol}, subscribe messages, and message parsing."""
    name = ""
    url = ""
    symbols: Dict[str, str] = {}

    def __init__(self, assets: List[str]):
        self.want = {a: s for a, s in self.symbols.items() if a in assets}
        self.rev = {s: a for a, s in self.want.items()}

    def subscribe(self) -> List[dict]:
        return []

    def parse(self, data: str) -> List[Tuple[str, float]]:
        return []

    async def available(self, sess: aiohttp.ClientSession, native: str) -> bool:
        return True


class Binance(ExchangeSpec):
    name = "binance"
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT"}

    @property
    def url(self):
        return "wss://stream.binance.com:9443/stream?streams=" + "/".join(f"{s.lower()}@bookTicker" for s in self.want.values())

    def parse(self, data):
        d = json.loads(data).get("data", {})
        a = self.rev.get(str(d.get("s", "")))
        m = _mid(d.get("b"), d.get("a"), None) if a else None
        return [(a, m)] if a and m else []


class Coinbase(ExchangeSpec):
    name = "coinbase"
    url = "wss://ws-feed.exchange.coinbase.com"
    symbols = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD", "HYPE": "HYPE-USD"}

    def subscribe(self):
        return [{"type": "subscribe", "product_ids": list(self.want.values()), "channels": ["ticker"]}]

    def parse(self, data):
        j = json.loads(data)
        if j.get("type") != "ticker":
            return []
        a = self.rev.get(j.get("product_id"))
        m = _mid(j.get("best_bid"), j.get("best_ask"), j.get("price")) if a else None
        return [(a, m)] if a and m else []

    async def available(self, sess, native):
        async with sess.get(f"https://api.exchange.coinbase.com/products/{native}/ticker") as r:
            return r.status == 200


class Kraken(ExchangeSpec):
    name = "kraken"
    url = "wss://ws.kraken.com/v2"
    symbols = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "XRP": "XRP/USD", "DOGE": "DOGE/USD", "HYPE": "HYPE/USD"}

    def subscribe(self):
        return [{"method": "subscribe", "params": {"channel": "ticker", "symbol": list(self.want.values())}}]

    def parse(self, data):
        j = json.loads(data)
        if j.get("channel") != "ticker" or not j.get("data"):
            return []
        out = []
        for x in j["data"]:
            a = self.rev.get(x.get("symbol"))
            m = _mid(x.get("bid"), x.get("ask"), x.get("last")) if a else None
            if a and m:
                out.append((a, m))
        return out

    async def available(self, sess, native):
        async with sess.get("https://api.kraken.com/0/public/Ticker", params={"pair": native.replace("/", "")}) as r:
            j = await r.json()
            return not j.get("error") and bool(j.get("result"))


class Bybit(ExchangeSpec):
    name = "bybit"
    url = "wss://stream.bybit.com/v5/public/spot"
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
               "BNB": "BNBUSDT", "HYPE": "HYPEUSDT"}

    def subscribe(self):
        return [{"op": "subscribe", "args": [f"tickers.{s}" for s in self.want.values()]}]

    def parse(self, data):
        j = json.loads(data)
        if not str(j.get("topic", "")).startswith("tickers.") or not isinstance(j.get("data"), dict):
            return []
        d = j["data"]
        a = self.rev.get(d.get("symbol"))
        m = _f(d.get("lastPrice")) if a else None
        return [(a, m)] if a and m else []

    async def available(self, sess, native):
        async with sess.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot", "symbol": native}) as r:
            j = await r.json()
            return j.get("retCode") == 0 and bool(j.get("result", {}).get("list"))


class OKX(ExchangeSpec):
    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    symbols = {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "SOL": "SOL-USDT", "XRP": "XRP-USDT", "DOGE": "DOGE-USDT",
               "BNB": "BNB-USDT", "HYPE": "HYPE-USDT"}

    def subscribe(self):
        return [{"op": "subscribe", "args": [{"channel": "tickers", "instId": s} for s in self.want.values()]}]

    def parse(self, data):
        j = json.loads(data)
        if j.get("arg", {}).get("channel") != "tickers" or not j.get("data"):
            return []
        out = []
        for x in j["data"]:
            a = self.rev.get(x.get("instId"))
            m = _mid(x.get("bidPx"), x.get("askPx"), x.get("last")) if a else None
            if a and m:
                out.append((a, m))
        return out

    async def available(self, sess, native):
        async with sess.get("https://www.okx.com/api/v5/market/ticker", params={"instId": native}) as r:
            j = await r.json()
            return j.get("code") == "0" and bool(j.get("data"))


class Bitget(ExchangeSpec):
    name = "bitget"
    url = "wss://ws.bitget.com/v2/ws/public"
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
               "BNB": "BNBUSDT", "HYPE": "HYPEUSDT"}

    def subscribe(self):
        return [{"op": "subscribe", "args": [{"instType": "SPOT", "channel": "ticker", "instId": s} for s in self.want.values()]}]

    def parse(self, data):
        j = json.loads(data)
        if j.get("arg", {}).get("channel") != "ticker" or not j.get("data"):
            return []
        out = []
        for x in j["data"]:
            a = self.rev.get(x.get("instId"))
            m = _mid(x.get("bidPr"), x.get("askPr"), x.get("lastPr")) if a else None
            if a and m:
                out.append((a, m))
        return out

    async def available(self, sess, native):
        async with sess.get("https://api.bitget.com/api/v2/spot/market/tickers", params={"symbol": native}) as r:
            j = await r.json()
            return j.get("code") == "00000" and bool(j.get("data"))


ALL_EXCHANGES = (Binance, Coinbase, Kraken, Bybit, OKX, Bitget)


async def filter_available(spec: ExchangeSpec):
    """Drop symbols the exchange does not list (quick REST check at startup)."""
    if not spec.want:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as sess:
            res = await asyncio.gather(*[spec.available(sess, n) for n in spec.want.values()], return_exceptions=True)
        for (asset, native), ok in list(zip(spec.want.items(), res)):
            if ok is False:
                spec.want.pop(asset, None)
                spec.rev.pop(native, None)
                logger.info(f"{spec.name}: {native} is not available, ignoring it")
    except Exception as ex:
        logger.debug(f"{spec.name}: symbol check failed ({ex}); using all of them")


class ExchangeStream:
    def __init__(self, spec: ExchangeSpec, hub: PriceHub):
        self.spec, self.hub = spec, hub

    async def run(self):
        await filter_available(self.spec)
        if not self.spec.want:
            return
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(self.spec.url, heartbeat=20, timeout=10) as ws:
                        backoff = 1.0
                        for msg in self.spec.subscribe():
                            await ws.send_json(msg)
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT:
                                now = time.time()
                                try:
                                    ticks = self.spec.parse(m.data)
                                except (ValueError, KeyError, TypeError):
                                    continue
                                for asset, mid in ticks:
                                    self.hub.on_exchange(self.spec.name, asset, mid, now)
                            elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug(f"{self.spec.name} ws: {ex}")
            await asyncio.sleep(min(backoff, 15.0))
            backoff *= 2


class ChainlinkStream:
    """Chainlink prices from the Polymarket RTDS (topic crypto_prices_chainlink)."""
    URL = "wss://ws-live-data.polymarket.com"

    def __init__(self, hub: PriceHub):
        self.hub = hub
        self._by_symbol = {f"{a.lower()}/usd": a for a in hub.assets}

    async def run(self):
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(self.URL, heartbeat=20, timeout=10) as ws:
                        backoff = 1.0
                        await ws.send_json({"action": "subscribe", "subscriptions": [
                            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]})
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT and m.data:
                                try:
                                    j = json.loads(m.data)
                                except ValueError:
                                    continue
                                p = j.get("payload") or {}
                                a = self._by_symbol.get(str(p.get("symbol", "")).lower())
                                if a and "value" in p:
                                    self.hub.on_chainlink(a, float(p["value"]), float(p.get("timestamp", 0)) / 1000.0, time.time())
                            elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug(f"chainlink ws: {ex}")
            await asyncio.sleep(min(backoff, 15.0))
            backoff *= 2
