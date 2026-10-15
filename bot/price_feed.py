import asyncio
import time
from typing import Dict, Optional, Tuple
import aiohttp
import numpy as np
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config import Config

# Asset to exchange-symbol map
BINANCE_SYMBOLS: Dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "MATIC": "MATICUSDT",
    "LINK": "LINKUSDT",
    "DOGE": "DOGEUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT",
}

# Commodities use the Yahoo Finance API via a yfinance ticker
YAHOO_SYMBOLS: Dict[str, str] = {
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "OIL": "CL=F",
    "XAU": "GC=F",
}

BINANCE_API = "https://api.binance.com/api/v3"


class PriceFeed:
    def __init__(self, config: Config):
        self.config = config
        self._cache: Dict[str, Tuple[float, float]] = {}  # asset -> (price, timestamp)
        self._vol_cache: Dict[str, Tuple[float, float]] = {}
        self._cache_ttl = 0.5  # seconds
        self._vol_cache_ttl = 300.0
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
                connector=aiohttp.TCPConnector(limit=20),
            )
        return self._session

    async def get_prices(self) -> Dict[str, float]:
        now = time.monotonic()
        cached = {
            asset: price
            for asset, (price, ts) in self._cache.items()
            if (now - ts) < self._cache_ttl
        }
        if len(cached) >= len(BINANCE_SYMBOLS) + 1:
            return cached

        prices: Dict[str, float] = {}

        # Fetch crypto (Binance - bulk endpoint)
        try:
            crypto = await self._fetch_binance_prices()
            prices.update(crypto)
        except Exception as e:
            logger.warning(f"Error fetch Binance: {e}")

        # Fetch commodities
        try:
            gold = await self._fetch_gold_price()
            if gold:
                prices["GOLD"] = gold
                prices["XAU"] = gold
        except Exception as e:
            logger.warning(f"Error fetch gold: {e}")

        # Update cache
        ts = time.monotonic()
        for asset, price in prices.items():
            self._cache[asset] = (price, ts)

        return prices

    async def get_price(self, asset: str) -> Optional[float]:
        prices = await self.get_prices()
        return prices.get(asset.upper())

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.1, max=1))
    async def _fetch_binance_prices(self) -> Dict[str, float]:
        session = await self._get_session()
        symbols = list(BINANCE_SYMBOLS.values())
        params = {"symbols": str(symbols).replace("'", '"').replace(" ", "")}
        async with session.get(f"{BINANCE_API}/ticker/price", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        symbol_to_asset = {v: k for k, v in BINANCE_SYMBOLS.items()}
        return {
            symbol_to_asset[item["symbol"]]: float(item["price"])
            for item in data
            if item["symbol"] in symbol_to_asset
        }

    async def _fetch_gold_price(self) -> Optional[float]:
        # Yahoo Finance via yfinance, async through the thread pool
        loop = asyncio.get_event_loop()
        try:
            price = await loop.run_in_executor(None, self._sync_gold_price)
            return price
        except Exception as e:
            logger.warning(f"yfinance gold error: {e}")
            return None

    def _sync_gold_price(self) -> float:
        import yfinance as yf
        ticker = yf.Ticker("GC=F")
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            raise ValueError("No gold data")
        return float(hist["Close"].iloc[-1])

    async def get_historical_volatility(self, asset: str, days: int = 30) -> float:
        now = time.monotonic()
        cache_key = f"{asset}_{days}"
        if cache_key in self._vol_cache:
            vol, ts = self._vol_cache[cache_key]
            if (now - ts) < self._vol_cache_ttl:
                return vol

        vol = await self._calculate_volatility(asset, days)
        self._vol_cache[cache_key] = (vol, now)
        return vol

    async def _calculate_volatility(self, asset: str, days: int) -> float:
        try:
            if asset.upper() in BINANCE_SYMBOLS:
                closes = await self._fetch_binance_historical(asset, days)
            elif asset.upper() in ("GOLD", "XAU"):
                closes = await self._fetch_commodity_historical("GC=F", days)
            else:
                return 0.8  # conservative default

            if len(closes) < 5:
                return 0.8

            arr = np.array(closes)
            log_returns = np.log(arr[1:] / arr[:-1])
            daily_vol = float(np.std(log_returns))
            annual_vol = daily_vol * np.sqrt(365)
            return max(0.05, min(annual_vol, 3.0))

        except Exception as e:
            logger.warning(f"Error computing vol {asset}: {e}")
            return 0.8  # conservative fallback

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2))
    async def _fetch_binance_historical(self, asset: str, days: int) -> list:
        symbol = BINANCE_SYMBOLS.get(asset.upper())
        if not symbol:
            return []
        session = await self._get_session()
        params = {"symbol": symbol, "interval": "1d", "limit": days + 1}
        async with session.get(f"{BINANCE_API}/klines", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [float(c[4]) for c in data]  # close prices

    async def _fetch_commodity_historical(self, symbol: str, days: int) -> list:
        loop = asyncio.get_event_loop()

        def _sync():
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=f"{days}d")
            return hist["Close"].tolist()

        return await loop.run_in_executor(None, _sync)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
