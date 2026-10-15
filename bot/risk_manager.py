"""
Risk management: filter opportunities and size positions.
Fractional Kelly with portfolio limits.
"""
from typing import List
from loguru import logger

from config import Config
from bot.models import Opportunity, Position, BotStats


class RiskManager:
    def __init__(self, config: Config):
        self.config = config
        self.pending_usdc = 0.0      # in-flight orders (not yet a position)
        self.pending_count = 0

    def filter_opportunities(
        self,
        opportunities: List[Opportunity],
        open_positions: List[Position],
        stats: BotStats,
    ) -> List[Opportunity]:
        approved: List[Opportunity] = []
        open_market_ids = {p.market.id for p in open_positions if p.status == "open"}

        available_capital = self._available_capital(open_positions, stats)
        if available_capital < 1.0:
            logger.debug("Not enough capital, skipping cycle")
            return []

        for opp in opportunities:
            # Do not open a second position in the same market
            if opp.market.id in open_market_ids:
                continue

            # Open-position limit
            if len(open_positions) + self.pending_count + len(approved) >= self.config.max_open_positions:
                break

            # Threshold checks
            if opp.edge < self.config.min_edge:
                continue
            # Up/Down buys on price edge (e.g. p=48% at 0.40), not on a high win probability
            if opp.market.kind != "updown" and opp.true_probability < self.config.min_win_probability:
                continue
            if opp.expected_value < self.config.min_expected_value:
                continue

            # Size
            size = self.calculate_bet_size(opp, available_capital)
            if size < 1.0:
                continue

            opp._bet_size = size  # type: ignore[attr-defined]
            approved.append(opp)
            available_capital -= size

        return approved

    def calculate_bet_size(self, opp: Opportunity, available_capital: float) -> float:
        """
        Bet size from fractional Kelly with portfolio limits.
        size = min(kelly_fraction * capital, max_bet_size)
        """
        kelly_size = opp.kelly_fraction * available_capital
        max_size = min(self.config.max_bet_size, available_capital * 0.10)
        size = min(kelly_size, max_size)
        # Round to 2 decimals and keep a $1 floor when the size is positive
        return max(0.0, round(size, 2))

    def calculate_exits(self, opp: Opportunity) -> tuple[float, float]:
        """Take-profit and stop-loss from the edge."""
        price = opp.bet_price
        # Take profit: when the market moves toward our estimated true probability
        take_profit = min(0.97, price + opp.edge * 0.6)
        # Stop loss: if the price falls 40% of the edge
        stop_loss = max(0.03, price - opp.edge * 0.4)
        return round(take_profit, 4), round(stop_loss, 4)

    def _available_capital(self, open_positions: List[Position], stats: BotStats) -> float:
        invested = sum(p.size_usdc for p in open_positions if p.status == "open")
        room = stats.current_capital - invested - self.pending_usdc
        cap = self.config.max_exposure_pct * stats.current_capital - invested - self.pending_usdc   # combined cap
        return max(0.0, min(room, cap))

    def portfolio_exposure(self, open_positions: List[Position], stats: BotStats) -> float:
        invested = sum(p.size_usdc for p in open_positions if p.status == "open")
        return invested / stats.current_capital if stats.current_capital > 0 else 0.0

    def log_risk_summary(self, open_positions: List[Position], stats: BotStats):
        exposure = self.portfolio_exposure(open_positions, stats)
        available = self._available_capital(open_positions, stats)
        logger.info(
            f"Risk | Capital={stats.current_capital:.2f} USDC "
            f"Exposure={exposure:.1%} Available={available:.2f} "
            f"Positions={len(open_positions)}/{self.config.max_open_positions}"
        )
