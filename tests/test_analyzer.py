"""Tests for the statistical analysis engine."""
import pytest
from bot.analyzer import MarketAnalyzer


def test_black_scholes_above_strike():
    """Price far above the strike -> high probability."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=70_000, strike=60_000, days=1, sigma=0.65, direction="above"
    )
    assert p > 0.85, f"Expected p>0.85 but got {p:.4f}"


def test_black_scholes_below_strike():
    """Price far below the strike -> low probability."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=50_000, strike=80_000, days=1, sigma=0.65, direction="above"
    )
    assert p < 0.15, f"Expected p<0.15 but got {p:.4f}"


def test_black_scholes_at_expiry_above():
    """At T=0, spot > strike must return 1.0."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=100, strike=90, days=0, sigma=0.5, direction="above"
    )
    assert p == 1.0


def test_black_scholes_at_expiry_below():
    """At T=0, spot < strike must return 0.0."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=80, strike=90, days=0, sigma=0.5, direction="above"
    )
    assert p == 0.0


def test_direction_below():
    """Direction 'below': high probability when spot < strike."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=1800, strike=2000, days=1, sigma=0.80, direction="below"
    )
    assert p > 0.80


def test_expected_value_positive():
    """Positive EV when the true probability is above the market price."""
    ev = MarketAnalyzer._expected_value(true_prob=0.80, market_price=0.60)
    assert ev > 0, f"EV should be positive: {ev:.4f}"


def test_expected_value_negative():
    """Negative EV when the true probability is below the market price."""
    ev = MarketAnalyzer._expected_value(true_prob=0.40, market_price=0.70)
    assert ev < 0, f"EV should be negative: {ev:.4f}"


def test_kelly_fraction_positive_edge():
    """Kelly > 0 when there is positive edge."""
    kelly = MarketAnalyzer._kelly_fraction(true_prob=0.75, market_price=0.55, multiplier=0.25)
    assert kelly > 0


def test_kelly_fraction_no_edge():
    """Kelly is about 0 when there is no real edge."""
    kelly = MarketAnalyzer._kelly_fraction(true_prob=0.50, market_price=0.55, multiplier=0.25)
    assert kelly <= 0


def test_confidence_far_from_strike():
    """Higher confidence when spot is far from the strike."""
    high_conf = MarketAnalyzer._calculate_confidence(
        spot=100_000, strike=50_000, days=0.5, sigma=0.65
    )
    low_conf = MarketAnalyzer._calculate_confidence(
        spot=51_000, strike=50_000, days=0.5, sigma=0.65
    )
    assert high_conf > low_conf
