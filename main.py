"""
Polymarket HFT Bot - visual dashboard
======================================
Usage:
    python main.py              # Paper mode (simulation)
    python main.py --live       # Live mode (requires a configured .env)
    python main.py --cycles 50  # Run N cycles and exit
    python main.py --debug      # Verbose logs on screen

Live keys (Windows):  C = collect the basket (positions in profit)   X = close everything
                      P = pause/resume entries                       A = auto-collect on/off

Speed: the decision loop wakes on each new websocket update and only reads memory;
orders and settlements run as background tasks.
"""
import asyncio
import os
import sys
import time
from collections import deque
from pathlib import Path
from loguru import logger

from config import Config
from bot.scanner import MarketScanner
from bot.price_feed import PriceFeed
from bot.analyzer import MarketAnalyzer
from bot.risk_manager import RiskManager
from bot.executor import OrderExecutor
from bot.position_manager import PositionManager
from bot.dashboard import Dashboard, side_label, MINT, RED, AMBER, DIM
from bot.models import BotStats
from bot.updown import UpDownFeed
from bot.keys import KeyListener

LOG_DIR = Path(__file__).parent / "logs"
FAIL_COOLDOWN_S = 3.0
RENDER_EVERY_S = 0.12


def setup_logger():
    logger.remove()
    try:
        LOG_DIR.mkdir(exist_ok=True)
        logger.add(str(LOG_DIR / "bot_{time:YYYY-MM-DD}.log"), rotation="1 day",
                   retention="7 days", level="DEBUG", enqueue=True)
    except OSError:
        fallback = Path.home() / "polymarket-logs"
        fallback.mkdir(exist_ok=True)
        logger.add(str(fallback / "bot_{time:YYYY-MM-DD}.log"), rotation="1 day",
                   retention="7 days", level="DEBUG", enqueue=True)
    if "--debug" in sys.argv:
        logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level:<7} | {message}")


def tune_windows():
    """1 ms timer (Windows otherwise rounds to ~15.6 ms) and high process priority."""
    if os.name != "nt":
        return lambda: None
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)
        return lambda: ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        return lambda: None


async def _empty_list():
    return []


async def _fetch_static(scanner, price_feed):
    if not scanner:
        return [], {}
    return await asyncio.gather(scanner.get_active_markets(), price_feed.get_prices())


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0.0


async def main():
    if "--live" in sys.argv:
        os.environ["BOT_MODE"] = "live"

    config = Config()
    max_cycles = None
    if "--cycles" in sys.argv:
        idx = sys.argv.index("--cycles")
        if idx + 1 < len(sys.argv):
            max_cycles = int(sys.argv[idx + 1])

    setup_logger()
    restore_timer = tune_windows()

    scanner          = MarketScanner(config) if config.enable_long_markets else None
    price_feed       = PriceFeed(config) if config.enable_long_markets else None
    feed             = UpDownFeed(config) if config.enable_fast else None
    analyzer         = MarketAnalyzer(config)
    risk_manager     = RiskManager(config)
    executor         = OrderExecutor(config)
    position_manager = PositionManager(config, executor)
    if feed:
        position_manager.resolver = feed.resolution
        executor.quote_fn = feed.quote
    stats = BotStats(current_capital=config.initial_capital)
    dash  = Dashboard(config.mode, config.initial_capital, config.max_open_positions)
    dash.min_edge = config.fast_min_edge
    dash.trade_assets = config.trade_assets
    dash.trade_tfs = config.trade_timeframes
    dash.require_exact = config.fast_require_exact_ref
    keys = KeyListener()
    keys.start()

    dash.log.add("»", f"Bot started - Capital {config.initial_capital:.0f} USDC | mode {config.mode.upper()} | "
                      f"Kelly x{config.kelly_fraction}", AMBER)
    if feed:
        dash.log.add("◆", f"Up/Down {','.join(feed.tfs)} on {','.join(feed.assets)} | net edge >= "
                          f"{config.fast_min_edge:.0%} after fees", AMBER)

    traded_ids: set = set()
    seen_opps: set = set()
    fail_until: dict = {}
    pending: dict = {}                   # market.id -> USDC of in-flight orders
    bg: set = set()                      # background tasks (orders, closes, settlement)
    react_hist: deque = deque(maxlen=2000)
    paused = False
    x_armed_until = 0.0
    last_render = 0.0
    t_start = time.monotonic()

    def spawn(coro):
        task = asyncio.create_task(coro)
        bg.add(task)
        task.add_done_callback(bg.discard)
        return task

    def log_closed(closed):
        for p in closed:
            dash.log_close(p.market.asset, p.realized_pnl, position_manager.last_reason.get(p.id, "CLOSE"))

    async def do_entry(opp, size):
        try:
            result = await executor.execute(opp, size)
            if result.success and result.position:
                position_manager.add_position(result.position)
                dash.log_trade(side_label(opp.market, opp.side), opp.market.asset,
                               result.position.size_usdc, result.position.entry_price)
                if config.is_live:
                    stats.current_capital -= result.position.size_usdc
            else:
                traded_ids.discard(opp.market.id)
                fail_until[opp.market.id] = time.time() + FAIL_COOLDOWN_S
                dash.log.add("·", f"not filled {opp.market.asset}: {result.error or 'no fill'}", DIM)
        finally:
            pending.pop(opp.market.id, None)

    async def do_close(items):
        log_closed(await position_manager.close_items(items, stats))

    async def do_collect(everything):
        closed = await position_manager.collect(stats, everything=everything)
        log_closed(closed)
        total = sum(p.realized_pnl for p in closed)
        if everything:
            dash.log.add("$", f"FULL CLOSE: {len(closed)} positions {total:+.2f} USDC", AMBER)
        else:
            dash.log.add("$", f"BASKET COLLECTED: {len(closed)} positions  {total:+.2f} USDC" if closed
                         else "BASKET: nothing in profit to collect", MINT if closed else DIM)

    async def settle_loop():
        while True:
            try:
                log_closed(await position_manager.settle_due(stats))
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug(f"settlement: {ex}")
            await asyncio.sleep(1.0)

    settle_task = asyncio.create_task(settle_loop())

    with dash.make_live() as live:
        try:
            while True:
                # Event-driven: wakes on each Binance tick or book change
                if feed:
                    await feed.wait_update(0.25)
                t0 = time.monotonic()
                stats.cycles += 1

                try:
                    # 0. Keys
                    for k in keys.poll():
                        if k == "c":
                            spawn(do_collect(False))
                        elif k == "x":
                            if config.is_live and time.time() > x_armed_until:
                                x_armed_until = time.time() + 3
                                dash.log.add("!", "LIVE: press X again within 3 s to close EVERYTHING", RED)
                            else:
                                spawn(do_collect(True))
                        elif k == "p":
                            paused = not paused
                            dash.log.add("‖" if paused else "»", "entries PAUSED" if paused else "entries resumed", AMBER)
                        elif k == "a":
                            position_manager.auto_collect = not position_manager.auto_collect
                            dash.log.add("»", f"auto-collect {'ON' if position_manager.auto_collect else 'OFF'}", AMBER)

                    # 1. Data (memory fed by websockets)
                    (static_markets, prices), fast_markets = await asyncio.gather(
                        _fetch_static(scanner, price_feed),
                        feed.snapshot() if feed else _empty_list(),
                    )
                    markets = static_markets + fast_markets
                    position_manager.refresh_prices(markets)

                    # 2. Analysis
                    volatilities = {}
                    if static_markets and price_feed:
                        assets = list({m.asset for m in static_markets})
                        vols = await asyncio.gather(*[price_feed.get_historical_volatility(a) for a in assets],
                                                    return_exceptions=True)
                        volatilities = {a: (v if isinstance(v, float) else 0.8) for a, v in zip(assets, vols)}
                    opportunities = analyzer.find_opportunities(markets, prices, volatilities)

                    now = time.time()
                    opportunities = [o for o in opportunities
                                     if not (o.market.kind == "updown" and
                                             (o.market.id in traded_ids or fail_until.get(o.market.id, 0) > now))]
                    for opp in opportunities:
                        key = (opp.market.id, opp.side)
                        if key not in seen_opps:
                            seen_opps.add(key)
                            dash.log_opportunity(opp)
                    stats.opportunities_found = len(seen_opps)

                    # 3. Risk and execution: orders go out in the background and the loop continues
                    risk_manager.pending_usdc = sum(pending.values())
                    risk_manager.pending_count = len(pending)
                    approved = [] if paused else risk_manager.filter_opportunities(
                        opportunities, position_manager.open_positions(), stats)
                    if feed:
                        react_hist.append((time.perf_counter() - feed.snap_arrival) * 1000.0)   # data -> decision
                    for opp in approved:
                        size = getattr(opp, "_bet_size", risk_manager.calculate_bet_size(opp, stats.current_capital))
                        if opp.market.kind == "updown":
                            traded_ids.add(opp.market.id)
                        pending[opp.market.id] = size
                        spawn(do_entry(opp, size))

                    # 4. Exits: decision with no network; the sell goes out in the background
                    items = position_manager.plan()
                    if items:
                        spawn(do_close(items))

                    stats.open_positions = len(position_manager.open_positions())
                    dash.windows = fast_markets
                    dash.paused = paused
                    dash.auto_collect = position_manager.auto_collect
                    dash.basket_pnl = position_manager.basket_pnl()
                    dash.basket_target = position_manager.basket_target
                    dash.lock_pct = position_manager.lock_pct
                    dash.update(stats, len(markets), opportunities, position_manager.positions)

                except Exception as e:
                    dash.log_error(str(e)[:80])
                    logger.exception(f"Cycle error {stats.cycles}: {e}")

                # Drawing the panel costs ~10-20 ms: cap it at ~8 times per second
                nowm = time.monotonic()
                if nowm - last_render >= RENDER_EVERY_S:
                    if feed:
                        dash.feed_mode = feed.mode_label()
                        dash.data_lag_ms = feed.data_lag_ms()
                        dash.subtitle = f"{' · '.join(feed.assets)}  |  {' · '.join(feed.tfs)}  |  {feed.sources_label()}"
                        if feed.last_error:
                            logger.debug(feed.last_error)
                            feed.last_error = ""
                    dash.react_ms = _pct(list(react_hist)[-300:], 0.5)
                    dash.cycle_ms = (time.monotonic() - t0) * 1000
                    live.update(dash.render())
                    last_render = nowm

                elapsed = time.monotonic() - t0
                if feed:
                    await asyncio.sleep(max(0.0, config.react_min_interval_ms / 1000.0 - elapsed))
                else:
                    await asyncio.sleep(max(0.0, config.cycle_interval_ms / 1000.0 - elapsed))

                if max_cycles and stats.cycles >= max_cycles:
                    dash.log.add("■", f"Done: {max_cycles} cycles completed", AMBER)
                    live.update(dash.render())
                    await asyncio.sleep(2)
                    break

        except (KeyboardInterrupt, asyncio.CancelledError):
            dash.log.add("■", "Bot stopped by the user", AMBER)
            live.update(dash.render())
            await asyncio.sleep(1)

        finally:
            settle_task.cancel()
            for t in list(bg):
                t.cancel()
            await asyncio.gather(settle_task, *bg, return_exceptions=True)
            for c in (scanner, price_feed, feed):
                if c:
                    await c.close()
            restore_timer()

    runtime = max(1e-9, time.monotonic() - t_start)
    hist = list(react_hist)
    open_left = len(position_manager.open_positions())
    print(f"\n{'-'*55}")
    print(f"  FINAL SUMMARY")
    print(f"  Cycles : {stats.cycles}  ({stats.cycles / runtime:.0f} per second)")
    if hist:
        print(f"  Reaction data->decision: median {_pct(hist, 0.5):.2f} ms | p95 {_pct(hist, 0.95):.2f} ms | p99 {_pct(hist, 0.99):.2f} ms")
    print(f"  Trades : {stats.total_trades}  (W:{stats.winning_trades} / L:{stats.total_trades - stats.winning_trades})")
    print(f"  WinRate: {stats.win_rate:.1%}")
    print(f"  P&L    : {stats.total_pnl:+.2f} USDC   (positions still open: {open_left})")
    print(f"  ROI    : {stats.roi:+.2%}")
    print(f"{'-'*55}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
