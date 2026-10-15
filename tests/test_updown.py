from datetime import datetime

from bot.models import Market, Side
from bot.updown import prob_up_twap, side_edges, taker_fee_per_share


def _market(p_up, ask_up, ask_dn):
    return Market(
        id="1", question="BTC 5m", condition_id="", token_id_yes="a", token_id_no="b",
        yes_price=ask_up, no_price=ask_dn, volume=0, liquidity=1000,
        end_date=datetime.utcnow(), category="updown-5m", asset="BTC",
        kind="updown", p_model_yes=p_up,
    )


def test_fee_is_largest_at_fifty_cents_and_zero_at_extremes():
    assert taker_fee_per_share(0.5) > taker_fee_per_share(0.9) > taker_fee_per_share(0.99)
    assert abs(taker_fee_per_share(0.5) - 0.0175) < 1e-9


def test_prob_up_half_when_at_reference_far_from_end():
    assert abs(prob_up_twap(100, 100, 100, 200, 1e-4) - 0.5) < 1e-6


def test_prob_up_is_continuous_at_the_twap_boundary():
    a = prob_up_twap(100, 100.02, 100.02, 60.001, 1e-4)
    b = prob_up_twap(100, 100.02, 100.02, 59.999, 1e-4)
    assert abs(a - b) < 1e-3


def test_prob_up_locks_in_as_time_runs_out():
    early = prob_up_twap(100, 100.05, 100.05, 200, 1e-4)
    late = prob_up_twap(100, 100.05, 100.05, 5, 1e-4)
    assert 0.5 < early < late <= 1.0


def test_prob_up_symmetric_around_reference():
    up = prob_up_twap(100, 100.05, 100.05, 30, 1e-4)
    down = prob_up_twap(100, 99.95, 99.95, 30, 1e-4)
    assert abs(up + down - 1.0) < 1e-3


def test_prob_up_settled_window():
    assert prob_up_twap(100, 101, 100.5, 0, 1e-4) == 1.0
    assert prob_up_twap(100, 99, 99.5, 0, 1e-4) == 0.0


def test_avg_of_last_minute_matters_near_the_end():
    # spot equals the reference, but the last minute was above it -> Up is more likely
    assert prob_up_twap(100, 100.0, 100.1, 10, 1e-4) > 0.9
    assert prob_up_twap(100, 100.0, 99.9, 10, 1e-4) < 0.1


def test_side_edges_pick_underpriced_side_and_include_fee():
    m = _market(p_up=0.80, ask_up=0.55, ask_dn=0.46)
    edges = {s: e for s, _, _, _, e in side_edges(m)}
    assert edges[Side.YES] > 0.05 > edges[Side.NO]
    # YES edge is p - haircut - (ask + fee)
    yes = [x for x in side_edges(m) if x[0] == Side.YES][0]
    assert abs(yes[4] - (yes[1] - (0.55 + taker_fee_per_share(0.55)))) < 1e-9


def test_windows_with_approximate_reference_are_not_traded():
    from bot.analyzer import MarketAnalyzer
    from config import Config
    import time as _t

    cfg = Config(fast_require_exact_ref=True, fast_min_edge=0.01)
    an = MarketAnalyzer(cfg)
    m = _market(p_up=0.90, ask_up=0.40, ask_dn=0.65)
    m.start_ts = int(_t.time()) - 100
    m.window_s = 300
    m.yes_ask_size = m.no_ask_size = 100
    m.category, m.asset = "updown-5m", "BTC"
    m.ref_price, m.spot = 100.0, 100.1
    m.ref_kind = "kline"
    assert an._analyze_updown(m) is None
    m.ref_kind = "chainlink"
    assert an._analyze_updown(m) is not None
