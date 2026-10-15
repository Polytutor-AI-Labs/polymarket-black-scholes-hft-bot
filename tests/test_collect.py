import asyncio
import time
from datetime import datetime

from bot.executor import OrderExecutor
from bot.models import BotStats, Market, Position, Side
from bot.position_manager import PositionManager
from bot.updown import taker_fee_per_share
from config import Config


def _cfg(**kw):
    base = dict(bot_mode="paper", paper_fill_latency_ms=0, auto_collect=True, lock_profit_pct=0.30,
                basket_target_pct=0.0, initial_capital=1000.0)
    base.update(kw)
    return Config(**base)


def _pos(pid, bid, p_model=0.5, size=10.0, entry=0.5):
    m = Market(
        id=pid, question="x", condition_id="", token_id_yes="a", token_id_no="b",
        yes_price=bid + 0.01, no_price=1 - bid, volume=0, liquidity=1000, end_date=datetime.utcnow(),
        category="updown-5m", asset="BTC", kind="updown", p_model_yes=p_model, yes_bid=bid, no_bid=1 - bid - 0.01,
        start_ts=int(time.time()) - 100, window_s=300,
    )
    shares = size / (entry + taker_fee_per_share(entry))
    return Position(id=pid, market=m, side=Side.YES, size_usdc=size, entry_price=entry,
                    entry_time=datetime.utcnow(), shares=shares, target_exit_price=0.99, stop_loss_price=-1.0)


def _manager(**kw):
    cfg = _cfg(**kw)
    return PositionManager(cfg, OrderExecutor(cfg)), BotStats(current_capital=1000.0)


def test_lock_profit_triggers_when_net_gain_reaches_threshold():
    pm, stats = _manager()
    p = _pos("w", bid=0.80, p_model=0.95)          # the model still sees more value, but net gain is already +50%
    pm.add_position(p)
    assert pm._decide_updown(p) == "LOCK_PROFIT"


def test_no_lock_when_auto_collect_is_off():
    pm, stats = _manager(auto_collect=False)
    p = _pos("w", bid=0.80, p_model=0.95)
    assert pm._decide_updown(p) == ""


def test_collect_only_sells_what_is_in_profit():
    pm, stats = _manager(auto_collect=False)
    win, lose = _pos("win", bid=0.70), _pos("lose", bid=0.30)
    pm.add_position(win)
    pm.add_position(lose)
    closed = asyncio.run(pm.collect(stats))
    assert [p.id for p in closed] == ["win"]
    assert win.status == "closed" and win.realized_pnl > 0
    assert lose.status == "open"
    assert stats.total_pnl == win.realized_pnl


def test_collect_everything_closes_losers_too():
    pm, stats = _manager(auto_collect=False)
    pm.add_position(_pos("win", bid=0.70))
    pm.add_position(_pos("lose", bid=0.30))
    closed = asyncio.run(pm.collect(stats, everything=True))
    assert len(closed) == 2 and not pm.open_positions()


def test_basket_target_collects_all_profitable_positions():
    pm, stats = _manager(basket_target_pct=0.002, lock_profit_pct=0.0)   # $2 target on total net, no per-position lock
    w1, w2, loser = _pos("w1", bid=0.65, p_model=0.95), _pos("w2", bid=0.66, p_model=0.95), _pos("l", bid=0.45, p_model=0.95)
    for p in (w1, w2, loser):
        pm.add_position(p)
    assert pm.basket_pnl() > 0
    closed = asyncio.run(pm.monitor(stats))
    assert {p.id for p in closed} == {"w1", "w2"}
    assert loser.status == "open"
    assert all(pm.last_reason[p.id] == "BASKET" for p in closed)


def test_positions_near_expiry_are_not_sellable():
    pm, _ = _manager()
    p = _pos("late", bid=0.9)
    p.market.start_ts = int(time.time()) - 298      # about 2 s left
    assert not pm.sellable(p)


def test_a_position_being_closed_is_not_sold_twice():
    pm, stats = _manager(auto_collect=False)
    p = _pos("dup", bid=0.95, p_model=0.5)          # the model sees it as expensive: TAKE_PROFIT
    pm.add_position(p)
    assert [i[1] for i in pm.plan()] == ["TAKE_PROFIT"]
    pm._closing.add(p.id)                             # a sell is already in flight
    assert pm.plan() == []
    assert asyncio.run(pm.collect(stats)) == []
