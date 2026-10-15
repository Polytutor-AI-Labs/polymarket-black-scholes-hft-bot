from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass
class Market:
    id: str
    question: str
    condition_id: str
    token_id_yes: str
    token_id_no: str
    yes_price: float        # 0.0 - 1.0
    no_price: float         # 0.0 - 1.0
    volume: float           # USDC volume total
    liquidity: float        # available USDC liquidity
    end_date: datetime
    category: str
    asset: str              # 'BTC', 'ETH', 'GOLD', etc.
    target_price: Optional[float] = None   # target price in the question
    direction: str = "above"               # 'above' or 'below'
    barrier: bool = False                  # True = touches the level at any time
    # --- 5m/15m Up/Down markets (kind="updown"); yes=Up, no=Down; *_price = best ask ---
    kind: str = "static"
    ref_price: Optional[float] = None      # reference price at the start of the window
    spot: Optional[float] = None
    p_model_yes: Optional[float] = None    # P(Up) from the model
    yes_bid: Optional[float] = None        # best bid (price we can sell at)
    no_bid: Optional[float] = None
    yes_ask_size: float = 0.0
    no_ask_size: float = 0.0
    start_ts: int = 0
    window_s: int = 0
    slug: str = ""
    ref_kind: str = ""                   # 'chainlink' (exact) or 'kline' (approximate)

    @property
    def spread(self) -> float:
        return 1.0 - (self.yes_price + self.no_price)

    @property
    def hours_to_expiry(self) -> float:
        delta = self.end_date - datetime.utcnow()
        return max(0.0, delta.total_seconds() / 3600)

    @property
    def days_to_expiry(self) -> float:
        return self.hours_to_expiry / 24.0

    def __str__(self) -> str:
        return (
            f"[{self.asset}] {self.question[:60]}... "
            f"YES={self.yes_price:.2f} NO={self.no_price:.2f} "
            f"exp={self.hours_to_expiry:.1f}h"
        )


@dataclass
class Opportunity:
    market: Market
    side: Side
    true_probability: float   # probability from the statistical model
    implied_probability: float  # probability implied by the market price
    edge: float               # true_prob - implied_prob
    expected_value: float     # EV per USDC staked
    kelly_fraction: float     # optimal fractional Kelly
    confidence: float         # model confidence (0-1)
    reason: str               # why this looks like a good bet

    @property
    def bet_price(self) -> float:
        return self.market.yes_price if self.side == Side.YES else self.market.no_price

    @property
    def score(self) -> float:
        return self.edge * self.confidence * self.expected_value

    def __str__(self) -> str:
        return (
            f"[{self.side}] {self.market.asset} | "
            f"edge={self.edge:.2%} prob_real={self.true_probability:.2%} "
            f"EV={self.expected_value:.3f} kelly={self.kelly_fraction:.3f} | "
            f"{self.reason}"
        )


@dataclass
class Position:
    id: str
    market: Market
    side: Side
    size_usdc: float          # USDC staked
    entry_price: float        # price at entry
    entry_time: datetime
    shares: float             # shares bought
    target_exit_price: float  # target price for taking profit
    stop_loss_price: float    # stop-loss price
    status: str = "open"
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    realized_pnl: float = 0.0
    fee_paid: float = 0.0

    @property
    def current_price(self) -> float:
        """Price we could sell at now (the bid if we have one, otherwise the market price)."""
        m = self.market
        if self.side == Side.YES:
            return m.yes_bid if m.yes_bid is not None else m.yes_price
        return m.no_bid if m.no_bid is not None else m.no_price

    @property
    def current_value(self) -> float:
        return self.shares * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.current_value - self.size_usdc

    @property
    def unrealized_pnl_pct(self) -> float:
        return self.unrealized_pnl / self.size_usdc if self.size_usdc > 0 else 0.0

    @property
    def should_take_profit(self) -> bool:
        return self.current_price >= self.target_exit_price

    @property
    def should_stop_loss(self) -> bool:
        return self.current_price <= self.stop_loss_price

    def __str__(self) -> str:
        pnl_str = f"+{self.unrealized_pnl:.2f}" if self.unrealized_pnl >= 0 else f"{self.unrealized_pnl:.2f}"
        return (
            f"POS [{self.side}] {self.market.asset} | "
            f"entry={self.entry_price:.3f} now={self.current_price:.3f} "
            f"P&L={pnl_str} USDC ({self.unrealized_pnl_pct:.1%})"
        )


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str]
    position: Optional[Position]
    error: Optional[str] = None


@dataclass
class BotStats:
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    current_capital: float = 0.0
    open_positions: int = 0
    cycles: int = 0
    opportunities_found: int = 0
    start_time: datetime = field(default_factory=datetime.utcnow)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def roi(self) -> float:
        if self.current_capital <= 0:
            return 0.0
        initial = self.current_capital - self.total_pnl
        return self.total_pnl / initial if initial > 0 else 0.0

    @property
    def runtime_seconds(self) -> float:
        return (datetime.utcnow() - self.start_time).total_seconds()
