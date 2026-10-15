"""
Backtest by asset and timeframe (BTC, ETH, SOL, XRP, DOGE, BNB x 5m, 15m, 4h) on 96 h of resolved windows.
Splits the data into 48 h of training and 48 h of testing and reports strategy ROI (edge >= 8%,
ask >= 0.15, exit rule) with and without a 30% profit lock.

HYPE cannot be tested here: Binance does not list it and there are no 1 s candles. Validate it live with calibration.py.

Usage:  python backtest_assets.py       (downloads and caches bt_cache_all.pkl; ~5 min the first time)
"""
import asyncio
import os
import time

import backtest as bt
import backtest_lab as L
from bot.updown import TF_SECONDS

L.CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_cache_all.pkl")
bt.ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")


def line(tr):
    if not tr:
        return "n=0"
    c = sum(t[0] for t in tr)
    return (f"n={len(tr):4d} ROI={100 * sum(t[2] for t in tr) / c:+6.1f}% "
            f"win={100 * sum(1 for t in tr if t[2] > 0) / len(tr):3.0f}%")


async def main():
    t = time.time()
    kl, wins, t0, now = await L.load(96)
    print(f"data loaded in {time.time() - t:.0f}s: {len(wins)} windows", flush=True)
    wins = L.build(kl, wins)
    mid = t0 + (now - t0) // 2
    TR, TE = (t0, mid), (mid, now + 1)
    b = 1e-4
    ask15 = lambda m: m["ask"] >= 0.15
    print("\n== by timeframe and asset | edge>=8%, ask>=0.15 | no lock || with 30% lock ==")
    for tf in TF_SECONDS:
        for a in bt.ASSETS:
            f = lambda m, a=a, tf=tf: ask15(m) and m["asset"] == a and m["tf"] == tf
            n_w = sum(1 for w in wins if w["asset"] == a and w["tf"] == tf)
            print(f"{tf:>3} {a:5s} windows={n_w:4d} | "
                  f"TRAIN {line(L.simulate(wins, b, *TR, th=0.08, filt=f))} || TEST {line(L.simulate(wins, b, *TE, th=0.08, filt=f))} | "
                  f"lock30 TRAIN {line(L.simulate(wins, b, *TR, th=0.08, lock=0.30, filt=f))} || "
                  f"TEST {line(L.simulate(wins, b, *TE, th=0.08, lock=0.30, filt=f))}", flush=True)
    print("\n== aggregate by timeframe ==")
    for tf in TF_SECONDS:
        f = lambda m, tf=tf: ask15(m) and m["tf"] == tf
        print(f"{tf:>3} TRAIN {line(L.simulate(wins, b, *TR, th=0.08, filt=f))} || TEST {line(L.simulate(wins, b, *TE, th=0.08, filt=f))}")
    print("\n== Brier by asset (model vs market; lower is better) ==")
    for a in bt.ASSETS:
        sub = [w for w in wins if w["asset"] == a]
        tr, te = L.brier(sub, b, *TR), L.brier(sub, b, *TE)
        print(f"{a:5s} TRAIN model {tr[0]:.4f} market {tr[1]:.4f} || TEST model {te[0]:.4f} market {te[1]:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
