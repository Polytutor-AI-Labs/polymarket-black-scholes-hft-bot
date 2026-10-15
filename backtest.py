"""
Backtest of the Up/Down model against REAL Polymarket windows that have already resolved.

For each window: minute market prices (CLOB prices-history), Binance 1m candles,
and the real outcome (Gamma). Conservative: we buy at the mid + 1c, pay the taker fee,
and the model only sees 1m candles that have already closed (up to 60s of lag).

Usage:  python backtest.py [hours=24] [offset_hours=0]
"""
import asyncio
import json
import math
import sys
import time
from collections import defaultdict

import aiohttp

from bot.updown import (
    MAX_ASK, MIN_ASK, MIN_ELAPSED_S, MIN_LEFT_S, MODEL_HAIRCUT, SLUG_PREFIX, SYMBOLS, TF_SECONDS,
    TWAP_S, prob_up_twap, taker_fee_per_share,
)

ASSETS = ("BTC", "ETH")
THRESHOLDS = (0.05,)
SPREAD_PAD = 0.01
KS = (1.0, 1.5, 2.0)


async def get_json(s, url, **kw):
    for _ in range(3):
        try:
            async with s.get(url, **kw) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return None


async def klines(s, asset, start_ms, end_ms):
    """Binance 1-SECOND candles -> {second: (open, close)}."""
    sem = asyncio.Semaphore(10)

    async def chunk(cur):
        async with sem:
            return await get_json(s, "https://api.binance.com/api/v3/klines",
                                  params={"symbol": SYMBOLS[asset], "interval": "1s", "startTime": cur, "limit": 1000}) or []

    out = {}
    for rows in await asyncio.gather(*[chunk(c) for c in range(start_ms, end_ms, 1_000_000)]):
        for k in rows:
            out[int(k[0] // 1000)] = (float(k[1]), float(k[4]))
    return out


def model_p(kl, start, t, total, k=1.0):
    """P(Up) = P(TWAP of the last 60s >= ref) using only seconds already closed before t (~1s lag)."""
    end = start + total
    if t - 1 not in kl or start not in kl:
        return None
    spot = kl[t - 1][1]
    ref = kl[start][0]
    closes = [kl[m][1] for m in range(t - 3600, t, 60) if m in kl]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(rets) < 20:
        return None
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
    obs = [kl[m][1] for m in range(max(start, end - TWAP_S), t) if m in kl] if t > end - TWAP_S else []
    avg_obs = sum(obs) / len(obs) if obs else spot
    return prob_up_twap(ref, spot, avg_obs, end - t, max(sd / math.sqrt(60), 2e-5) * k)


async def window_data(s, sem, asset, tf, start):
    async with sem:
        slug = f"{SLUG_PREFIX[asset]}-updown-{tf}-{start}"
        ev = await get_json(s, "https://gamma-api.polymarket.com/events", params={"slug": slug})
        if not ev or not ev[0].get("markets"):
            return None
        m = ev[0]["markets"][0]
        if not m.get("closed"):
            return None
        prices = json.loads(m["outcomePrices"])
        if float(prices[0]) not in (0.0, 1.0):
            return None
        tok = json.loads(m["clobTokenIds"])[0]
        end = start + TF_SECONDS[tf]
        h = await get_json(s, "https://clob.polymarket.com/prices-history",
                           params={"market": tok, "startTs": start - 60, "endTs": end + 60, "fidelity": 1})
        if not h or not h.get("history"):
            return None
        return {"asset": asset, "tf": tf, "start": start, "up_won": float(prices[0]) == 1.0,
                "hist": [(x["t"], x["p"]) for x in h["history"]]}


async def main(hours: int, offset_h: int = 0):
    now = int(time.time()) - offset_h * 3600
    t0 = now - hours * 3600
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        kl = {a: await klines(s, a, (t0 - 4000) * 1000, now * 1000) for a in ASSETS}
        sem = asyncio.Semaphore(12)
        jobs = []
        for a in ASSETS:
            for tf, step in TF_SECONDS.items():
                st = t0 - t0 % step
                while st + step < now - 1800:
                    jobs.append(window_data(s, sem, a, tf, st))
                    st += step
        print(f"Downloading {len(jobs)} windows ({hours}h)...", flush=True)
        wins = [w for w in await asyncio.gather(*jobs) if w]
    print(f"Resolved windows with data: {len(wins)}", flush=True)

    for k in KS:
        brier_model = brier_mid = n_pts = 0.0
        trades = {th: [] for th in THRESHOLDS}
        exits = []
        for w in wins:
            total = TF_SECONDS[w["tf"]]
            entered = set()
            for t, mid in w["hist"]:
                el = t - w["start"]
                if el < MIN_ELAPSED_S or total - el < MIN_LEFT_S:
                    continue
                p = model_p(kl[w["asset"]], w["start"], t, total, k)
                if p is None:
                    continue
                y = 1.0 if w["up_won"] else 0.0
                brier_model += (p - y) ** 2
                brier_mid += (mid - y) ** 2
                n_pts += 1
                ask_u = min(0.99, mid + SPREAD_PAD)
                ask_d = min(0.99, (1 - mid) + SPREAD_PAD)
                cands = []
                for side, ask, pp, won in (("UP", ask_u, p, w["up_won"]), ("DOWN", ask_d, 1 - p, not w["up_won"])):
                    if not (MIN_ASK <= ask <= MAX_ASK):
                        continue
                    cost = ask + taker_fee_per_share(ask)
                    cands.append(((pp - MODEL_HAIRCUT) - cost, side, cost, won))
                if not cands:
                    continue
                edge, side, cost, won = max(cands)
                for th in THRESHOLDS:
                    if edge >= th and th not in entered:
                        entered.add(th)
                        trades[th].append((w["tf"], w["asset"], cost, (1.0 if won else 0.0) - cost, won, edge))
                        # --- exit variants on this same entry ---
                        hold = (1.0 if won else 0.0) - cost
                        tp_cut = tp_only = hold
                        done_tpcut = done_tp = False
                        for t2, mid2 in w["hist"]:
                            if t2 <= t or (w["start"] + total) - t2 < 5:
                                continue
                            p2 = model_p(kl[w["asset"]], w["start"], t2, total, k)
                            if p2 is None:
                                continue
                            fair = p2 if side == "UP" else 1 - p2
                            bid = (mid2 if side == "UP" else 1 - mid2) - SPREAD_PAD
                            if bid <= 0.01:
                                continue
                            net_bid = bid - taker_fee_per_share(bid)
                            if net_bid >= fair + 0.01:
                                if not done_tpcut:
                                    tp_cut = net_bid - cost
                                    done_tpcut = True
                                if net_bid > cost and not done_tp:
                                    tp_only = net_bid - cost
                                    done_tp = True
                            if done_tpcut and done_tp:
                                break
                        exits.append((cost, hold, tp_cut, tp_only))
        line = f"k={k:<4} Brier model={brier_model / n_pts:.4f} (market={brier_mid / n_pts:.4f}) |"
        for th in THRESHOLDS:
            tr = trades[th]
            if tr:
                c = sum(t[2] for t in tr); pn = sum(t[3] for t in tr)
                line += f" e>={th:.2f}: n={len(tr)} win={100 * sum(t[4] for t in tr) / len(tr):.0f}% ROI={100 * pn / c:+.1f}% |"
        print(line, flush=True)
        if exits:
            c = sum(e[0] for e in exits)
            print(f"  exits (n={len(exits)}): hold to expiry ROI={100 * sum(e[1] for e in exits) / c:+.1f}% | "
                  f"sell if net_bid>=fair+1c (current bot) ROI={100 * sum(e[2] for e in exits) / c:+.1f}% | "
                  f"take profit only ROI={100 * sum(e[3] for e in exits) / c:+.1f}%", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 24, int(sys.argv[2]) if len(sys.argv) > 2 else 0))
