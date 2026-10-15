from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List


class Config(BaseSettings):
    # Mode
    bot_mode: str = Field("paper", alias="BOT_MODE")

    # Polymarket
    poly_api_key: str = Field("", alias="POLY_API_KEY")
    poly_api_secret: str = Field("", alias="POLY_API_SECRET")
    poly_api_passphrase: str = Field("", alias="POLY_API_PASSPHRASE")
    poly_private_key: str = Field("", alias="POLY_PRIVATE_KEY")
    poly_chain_id: int = Field(137, alias="POLY_CHAIN_ID")

    # Capital
    initial_capital: float = Field(1000.0, alias="INITIAL_CAPITAL")
    max_bet_pct: float = Field(0.05, alias="MAX_BET_PCT")

    # Thresholds
    min_edge: float = Field(0.03, alias="MIN_EDGE")
    min_win_probability: float = Field(0.60, alias="MIN_WIN_PROBABILITY")
    min_expected_value: float = Field(0.02, alias="MIN_EXPECTED_VALUE")
    max_open_positions: int = Field(8, alias="MAX_OPEN_POSITIONS")
    kelly_fraction: float = Field(0.25, alias="KELLY_FRACTION")

    # Timing
    cycle_interval_ms: int = Field(100, alias="CYCLE_INTERVAL_MS")          # only the no-websocket / long-market mode
    react_min_interval_ms: float = Field(1.0, alias="REACT_MIN_INTERVAL_MS")  # minimum gap between event-driven cycles

    # APIs
    alpha_vantage_key: str = Field("", alias="ALPHA_VANTAGE_KEY")
    binance_testnet: bool = Field(False, alias="BINANCE_TESTNET")

    # Filters
    target_categories: str = Field("crypto,commodities", alias="TARGET_CATEGORIES")
    min_market_liquidity: float = Field(500.0, alias="MIN_MARKET_LIQUIDITY")
    min_hours_to_expiry: float = Field(0.5, alias="MIN_HOURS_TO_EXPIRY")
    max_hours_to_expiry: float = Field(48.0, alias="MAX_HOURS_TO_EXPIRY")

    # Fast strategy: 5m/15m Up/Down markets (backtest only supported BTC and ETH)
    enable_fast: bool = Field(True, alias="ENABLE_FAST")
    # ALL of these are followed (and written to the journal); only the backtest-validated ones are TRADED
    fast_assets: str = Field("BTC,ETH,SOL,XRP,DOGE,BNB,HYPE", alias="FAST_ASSETS")
    fast_timeframes: str = Field("5m,15m,4h", alias="FAST_TIMEFRAMES")
    fast_trade_assets: str = Field("BTC,ETH,SOL,XRP,DOGE,BNB", alias="FAST_TRADE_ASSETS")
    fast_trade_timeframes: str = Field("5m,15m", alias="FAST_TRADE_TIMEFRAMES")
    fast_min_edge: float = Field(0.08, alias="FAST_MIN_EDGE")
    fast_sigma_mult: float = Field(1.0, alias="FAST_SIGMA_MULT")
    fast_use_ws: bool = Field(True, alias="FAST_USE_WS")
    fast_journal: bool = Field(True, alias="FAST_JOURNAL")
    # Model price: "chainlink" (Chainlink stream only) or "blend" (exchange median + Chainlink basis).
    # The journal records both calculations so they can be compared with real outcomes.
    fast_price_source: str = Field("chainlink", alias="FAST_PRICE_SOURCE")
    # Trade only windows whose Chainlink reference was seen live. With the approximate reference the market
    # (which knows the exact price to beat) knows more than the model when price is pinned to that reference.
    fast_require_exact_ref: bool = Field(True, alias="FAST_REQUIRE_EXACT_REF")
    fast_basis: float = Field(0.0001, alias="FAST_BASIS")
    # Long markets (expire in months): capital is locked, not validated
    enable_long_markets: bool = Field(False, alias="ENABLE_LONG_MARKETS")

    # Realistic paper simulation: the order takes time and fills only if the price stays within 1 tick
    paper_fill_latency_ms: int = Field(150, alias="PAPER_FILL_LATENCY_MS")
    paper_max_slippage: float = Field(0.01, alias="PAPER_MAX_SLIPPAGE")

    # Profit collection
    auto_collect: bool = Field(True, alias="AUTO_COLLECT")
    lock_profit_pct: float = Field(0.30, alias="LOCK_PROFIT_PCT")        # sell a position after a net gain of +X%
    basket_target_pct: float = Field(0.03, alias="BASKET_TARGET_PCT")    # collect the basket at +X% of capital (0 = off)

    max_exposure_pct: float = Field(0.25, alias="MAX_EXPOSURE_PCT")   # combined cap (positions + in-flight orders)

    @property
    def trade_assets(self) -> List[str]:
        return [a.strip().upper() for a in self.fast_trade_assets.split(",") if a.strip()]

    @property
    def trade_timeframes(self) -> List[str]:
        return [t.strip() for t in self.fast_trade_timeframes.split(",") if t.strip()]

    @property
    def mode(self) -> str:
        return self.bot_mode

    @property
    def is_live(self) -> bool:
        return self.bot_mode == "live"

    @property
    def categories(self) -> List[str]:
        return [c.strip() for c in self.target_categories.split(",")]

    @property
    def max_bet_size(self) -> float:
        return self.initial_capital * self.max_bet_pct

    model_config = {"env_file": ".env", "populate_by_name": True}
