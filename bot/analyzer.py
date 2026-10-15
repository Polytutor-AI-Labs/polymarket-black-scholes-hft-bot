"""
Statistical engine that looks for mispriced markets.

Strategies:
1. Black-Scholes reference arbitrage: a log-normal true probability
   compared with the probability implied by the Polymarket price.
2. Near-resolution sweep: markets close to expiry where the outcome
   is nearly certain given the current asset price.
3. Spread arbitrage: logical inconsistencies between correlated markets.
"""
import time
from typing import List, Dict, Optional
import numpy as np
from scipy.stats import norm
from loguru import logger

from config import Config
from bot.models import Market, Opportunity, Side
from bot.updown import MAX_ASK, MIN_ASK, MIN_SHARES, entry_window_ok, side_edges


MAX_EDGE = 0.35


class MarketAnalyzer:
    def __init__(self, config: Config):
        self.config = config

    def find_opportunities(
        self,
        markets: List[Market],
        prices: Dict[str, float],
        volatilities: Optional[Dict[str, float]] = None,
    ) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        vols = volatilities or {}

        for market in markets:
            if market.kind == "updown":
                opp = self._analyze_updown(market)
                if opp:
                    opportunities.append(opp)
                continue
            if not self.config.enable_long_markets:
                continue
            spot = prices.get(market.asset)
            if spot is None or spot <= 0:
                continue
            if market.target_price is None or market.target_price <= 0:
                continue

            sigma = vols.get(market.asset, self._default_vol(market.asset))
            T = market.days_to_expiry

            opp = self._analyze_market(market, spot, sigma, T)
            if opp:
                opportunities.append(opp)

        # Sort by score, highest first
        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _analyze_updown(self, market: Market) -> Optional[Opportunity]:
        """5m/15m windows: fair probability vs the real ask, net of the fee."""
        tf = market.category.split("-")[1] if "-" in market.category else ""
        if market.asset not in self.config.trade_assets or tf not in self.config.trade_timeframes:
            return None                      # watch only: journaled, not traded
        if self.config.fast_require_exact_ref and market.ref_kind != "chainlink":
            return None                      # approximate reference: false edge near the price to beat
        if not entry_window_ok(market):
            return None
        best = None
        for side, p, ask, cost, edge in side_edges(market):
            depth = market.yes_ask_size if side == Side.YES else market.no_ask_size
            if not (MIN_ASK <= ask <= MAX_ASK) or depth < MIN_SHARES:
                continue
            if best is None or edge > best[4]:
                best = (side, p, ask, cost, edge)
        if best is None or best[4] < self.config.fast_min_edge:
            return None
        side, p, ask, cost, edge = best
        ev = p / cost - 1.0
        kelly = max(0.0, (p - cost) / (1.0 - cost)) * self.config.kelly_fraction
        label = "UP" if side == Side.YES else "DOWN"
        reason = (
            f"{market.asset} {label} | ref={market.ref_price:,.2f} spot={market.spot:,.2f} "
            f"P_model={p:.1%} ask={ask:.2f} cost(fee)={cost:.3f} edge={edge:+.1%}"
        )
        return Opportunity(
            market=market, side=side, true_probability=p, implied_probability=ask,
            edge=edge, expected_value=ev, kelly_fraction=kelly, confidence=0.9, reason=reason,
        )

    def _analyze_market(
        self,
        market: Market,
        spot: float,
        sigma: float,
        days: float,
    ) -> Optional[Opportunity]:
        try:
            true_prob = self._black_scholes_probability(
                spot, market.target_price, days, sigma, market.direction
            )
        except Exception as e:
            logger.debug(f"BS error {market.asset}: {e}")
            return None

        # Confidence: higher when time is short and the gap is large
        confidence = self._calculate_confidence(spot, market.target_price, days, sigma)

        # Which side to bet
        implied_yes = market.yes_price
        implied_no = market.no_price

        # true_prob is already P(price finishes beyond the target in the market's direction)
        true_yes_prob = true_prob

        if market.barrier:
            # "hit/reach/dip to X": touching the level is enough (reflection principle, no drift)
            already = spot >= market.target_price if market.direction == "above" else spot <= market.target_price
            true_yes_prob = 0.99 if already else min(0.99, 2.0 * true_prob)

        # Look for edge on YES
        edge_yes = true_yes_prob - implied_yes
        ev_yes = self._expected_value(true_yes_prob, implied_yes)
        kelly_yes = self._kelly_fraction(true_yes_prob, implied_yes, self.config.kelly_fraction)

        # Look for edge on NO
        true_no_prob = 1.0 - true_yes_prob
        edge_no = true_no_prob - implied_no
        ev_no = self._expected_value(true_no_prob, implied_no)
        kelly_no = self._kelly_fraction(true_no_prob, implied_no, self.config.kelly_fraction)

        # Pick the better side
        if edge_yes >= edge_no and edge_yes >= self.config.min_edge:
            side = Side.YES
            edge = edge_yes
            true_p = true_yes_prob
            implied_p = implied_yes
            ev = ev_yes
            kelly = kelly_yes
        elif edge_no > edge_yes and edge_no >= self.config.min_edge:
            side = Side.NO
            edge = edge_no
            true_p = true_no_prob
            implied_p = implied_no
            ev = ev_no
            kelly = kelly_no
        else:
            return None

        # A huge edge is almost always a parse or model error, not free money
        if edge > MAX_EDGE:
            logger.debug(f"Implausible edge discarded ({edge:.0%}): {market.question[:60]}")
            return None

        # Extra filters
        if true_p < self.config.min_win_probability:
            return None
        if ev < self.config.min_expected_value:
            return None
        if kelly <= 0:
            return None

        reason = self._build_reason(market, spot, true_p, implied_p, days, sigma)

        return Opportunity(
            market=market,
            side=side,
            true_probability=true_p,
            implied_probability=implied_p,
            edge=edge,
            expected_value=ev,
            kelly_fraction=kelly,
            confidence=confidence,
            reason=reason,
        )

    @staticmethod
    def _black_scholes_probability(
        spot: float,
        strike: float,
        days: float,
        sigma: float,
        direction: str = "above",
    ) -> float:
        """
        Risk-neutral probability that the price is above (or below) the strike
        in T days, under a log-normal model (GBM with no drift).
        P(S_T > K) = N(d2)  where d2 = (ln(S/K) - 0.5*σ²*T) / (σ*√T)
        """
        if days <= 0:
            if direction == "above":
                return 1.0 if spot > strike else 0.0
            else:
                return 1.0 if spot < strike else 0.0

        T = days / 365.0
        if T <= 0 or sigma <= 0:
            return 0.5

        ln_moneyness = np.log(spot / strike)
        d2 = (ln_moneyness - 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))

        prob_above = float(norm.cdf(d2))
        if direction == "above":
            return prob_above
        else:
            return 1.0 - prob_above

    @staticmethod
    def _expected_value(true_prob: float, market_price: float) -> float:
        """EV per USDC staked: a win pays 1/price, a loss costs 1."""
        if market_price <= 0 or market_price >= 1:
            return -1.0
        payout = 1.0 / market_price  # payout if you win
        ev = true_prob * payout - 1.0  # expected value per USDC invested
        return float(ev)

    @staticmethod
    def _kelly_fraction(true_prob: float, market_price: float, multiplier: float = 0.25) -> float:
        """
        Fractional Kelly criterion.
        f* = (b*p - q) / b  where b = (1/price) - 1
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0
        b = (1.0 / market_price) - 1.0  # net odds
        p = true_prob
        q = 1.0 - p
        full_kelly = (b * p - q) / b if b > 0 else 0.0
        return max(0.0, full_kelly * multiplier)

    @staticmethod
    def _calculate_confidence(
        spot: float, strike: float, days: float, sigma: float
    ) -> float:
        """
        Confidence based on:
        - Distance from spot to strike (farther = more certain)
        - Time to expiry (less time = more certain when far from the strike)
        """
        if sigma <= 0 or days <= 0:
            return 0.5

        T = days / 365.0
        # Standard deviations between spot and strike
        moneyness = abs(np.log(spot / strike)) / (sigma * np.sqrt(T))
        # Map to confidence (0-1) with a sigmoid
        confidence = float(1.0 / (1.0 + np.exp(-moneyness + 1.5)))
        return max(0.1, min(confidence, 0.99))

    @staticmethod
    def _default_vol(asset: str) -> float:
        defaults = {
            "BTC": 0.65,
            "ETH": 0.80,
            "SOL": 1.10,
            "MATIC": 1.20,
            "DOGE": 1.30,
            "XRP": 0.90,
            "AVAX": 1.05,
            "LINK": 0.95,
            "BNB": 0.70,
            "GOLD": 0.15,
            "SILVER": 0.25,
            "OIL": 0.35,
        }
        return defaults.get(asset.upper(), 0.80)

    @staticmethod
    def _build_reason(
        market: Market,
        spot: float,
        true_prob: float,
        implied_prob: float,
        days: float,
        sigma: float,
    ) -> str:
        direction = "↑" if market.direction == "above" else "↓"
        dist_pct = (spot / market.target_price - 1) * 100
        return (
            f"Spot={spot:,.2f} Target={market.target_price:,.0f} ({dist_pct:+.1f}%) "
            f"{direction} σ={sigma:.0%} T={days:.1f}d | "
            f"P_real={true_prob:.1%} vs P_mkt={implied_prob:.1%} "
            f"edge={true_prob-implied_prob:+.1%}"
        )
