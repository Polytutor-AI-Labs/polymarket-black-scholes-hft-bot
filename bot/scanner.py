import re
import json
import asyncio
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import aiohttp
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config import Config
from bot.models import Market

GAMMA_API = "https://gamma-api.polymarket.com"

ASSET_KEYWORDS: Dict[str, List[str]] = {
    "BTC":   ["bitcoin", "btc"],
    "ETH":   ["ethereum", "eth", "ether"],
    "SOL":   ["solana", "sol"],
    "MATIC": ["polygon", "matic"],
    "DOGE":  ["dogecoin", "doge"],
    "XRP":   ["ripple", "xrp"],
    "BNB":   ["bnb", "binance coin"],
    "ADA":   ["cardano", "ada"],
    "AVAX":  ["avalanche", "avax"],
    "LINK":  ["chainlink", "link"],
    "GOLD":  ["gold", "xau"],
    "SILVER":["silver", "xag"],
    "OIL":   ["oil", "crude", "wti", "brent"],
}
_ASSET_RES = {
    a: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")\b", re.I)
    for a, kws in ASSET_KEYWORDS.items()
}

PRICE_PATTERN = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kKmMbB]?)(?![a-zA-Z])")
_MULT = {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9}

_BELOW_RE = re.compile(r"\b(dip|drop|fall|crash|below|under|lower|decline|plunge|sink)\b", re.I)
_ABOVE_RE = re.compile(r"\b(above|over|higher|exceed|surpass|reach|hit|break|top|rise)\b", re.I)
_TOUCH_RE = re.compile(r"\b(reach|hit|dip|fall|drop|crash|touch|plunge|sink)\b", re.I)


def _extract_asset(question: str) -> Optional[str]:
    for asset, rx in _ASSET_RES.items():
        if rx.search(question):
            return asset
    return None


def _is_barrier(question: str) -> bool:
    """'Will X hit/reach/dip to $Y by DATE' = touches the level at any time."""
    return bool(_TOUCH_RE.search(question))


def _extract_target_price(question: str) -> Optional[float]:
    m = PRICE_PATTERN.search(question)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "")) * _MULT[m.group(2).lower()]
        return value if value > 0 else None
    except ValueError:
        return None


def _extract_direction(question: str) -> str:
    if _BELOW_RE.search(question):
        return "below"
    return "above"


def _parse_outcome_prices(raw_prices) -> Optional[tuple[float, float]]:
    """Parse outcomePrices, which may be a JSON string or a list."""
    try:
        if isinstance(raw_prices, str):
            raw_prices = json.loads(raw_prices)
        if not isinstance(raw_prices, list) or len(raw_prices) < 2:
            return None
        yes_p = float(raw_prices[0])
        no_p = float(raw_prices[1])
        return yes_p, no_p
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _parse_end_date(raw: Dict[str, Any]) -> Optional[datetime]:
    """Try several date fields."""
    for field in ["endDateIso", "endDate"]:
        val = raw.get(field)
        if not val:
            continue
        try:
            # May be a date only ("2027-01-01") or a full ISO timestamp
            if "T" in val:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(val + "T23:59:59+00:00")
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    return None


class MarketScanner:
    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: List[Market] = []
        self._cache_ts: float = 0.0
        self._cache_ttl = 15.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "polymarket-hft-bot/1.0"},
            )
        return self._session

    async def get_active_markets(self) -> List[Market]:
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        raw_markets = await self._fetch_all_markets()
        markets = []
        filtered_counts = {"no_asset": 0, "no_price": 0, "no_target": 0, "no_date": 0, "filter": 0}

        for raw in raw_markets:
            m = self._parse_market(raw, filtered_counts)
            if m and self._passes_filter(m, filtered_counts):
                markets.append(m)

        self._cache = markets
        self._cache_ts = now
        logger.info(
            f"Scanner: {len(raw_markets)} markets total → {len(markets)} usable | "
            f"Filters: {filtered_counts}"
        )
        return markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=5))
    async def _fetch_all_markets(self) -> List[Dict]:
        session = await self._get_session()
        all_markets: List[Dict] = []
        limit = 100

        for offset in range(0, 1500, limit):
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            async with session.get(f"{GAMMA_API}/markets", params=params) as resp:
                resp.raise_for_status()
                page = await resp.json()

            if not page:
                break
            all_markets.extend(page)
            if len(page) < limit:
                break

        return all_markets

    def _parse_market(self, raw: Dict[str, Any], counts: dict) -> Optional[Market]:
        try:
            question = raw.get("question", "")
            asset = _extract_asset(question)
            if not asset:
                counts["no_asset"] += 1
                return None

            prices = _parse_outcome_prices(raw.get("outcomePrices"))
            if prices is None:
                counts["no_price"] += 1
                return None
            yes_price, no_price = prices

            end_date = _parse_end_date(raw)
            if end_date is None:
                counts["no_date"] += 1
                return None

            token_ids = raw.get("clobTokenIds", [])
            token_yes = str(token_ids[0]) if len(token_ids) > 0 else ""
            token_no = str(token_ids[1]) if len(token_ids) > 1 else ""

            target_price = _extract_target_price(question)
            # Without a target price we cannot compute a Black-Scholes probability
            if target_price is None:
                counts["no_target"] += 1
                return None

            liquidity = float(raw.get("liquidityClob") or raw.get("liquidity") or 0)

            return Market(
                id=str(raw.get("id", "")),
                question=question,
                condition_id=str(raw.get("conditionId", "")),
                token_id_yes=token_yes,
                token_id_no=token_no,
                yes_price=yes_price,
                no_price=no_price,
                volume=float(raw.get("volumeClob") or raw.get("volume") or 0),
                liquidity=liquidity,
                end_date=end_date,
                category="crypto" if asset not in ("GOLD", "SILVER", "OIL") else "commodities",
                asset=asset,
                target_price=target_price,
                direction=_extract_direction(question),
                barrier=_is_barrier(question),
            )

        except Exception as e:
            logger.debug(f"Parse error: {e} | {raw.get('question','')[:50]}")
            return None

    def _passes_filter(self, market: Market, counts: dict) -> bool:
        if market.hours_to_expiry < self.config.min_hours_to_expiry:
            counts["filter"] += 1
            return False
        if market.hours_to_expiry > self.config.max_hours_to_expiry:
            counts["filter"] += 1
            return False
        if market.liquidity < self.config.min_market_liquidity:
            counts["filter"] += 1
            return False
        if market.yes_price <= 0.01 or market.yes_price >= 0.99:
            counts["filter"] += 1
            return False
        return True

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
