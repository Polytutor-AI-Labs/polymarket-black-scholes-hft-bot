"""
Backtest lab (96 h: 48 h train + 48 h test) on Up/Down windows that have already resolved.

Answers from the data: (A) Binance/Chainlink basis noise, (B) baseline return, (C) by price/time/edge bucket,
(D) exit and profit-lock rules, (E) edge threshold, (F) final combinations.
Downloads and caches data in bt_cache.pkl (delete it to refresh).

Usage:  python backtest_lab.py
"""

import asyncio, sys, time, math, pickle, os
sys.path.insert(0, ".")
import aiohttp
import backtest as bt
from bot.updown import TF_SECONDS, TWAP_S, norm_cdf, taker_fee_per_share, MIN_ELAPSED_S, MIN_LEFT_S, MIN_ASK, MAX_ASK, MODEL_HAIRCUT

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "bt_cache.pkl")
SPREAD = 0.01


def p_twap(ref, spot, avg_obs, left, sigma_s, basis):
    W = TWAP_S
    if left <= 0:
        std = 0.0
        mean = avg_obs
    elif left > W:
        mean = spot
        std = spot * sigma_s * math.sqrt(left - 2 * W / 3)
    else:
        w = (W - left) / W
        mean = w * avg_obs + (1 - w) * spot
        std = (1 - w) * spot * sigma_s * math.sqrt(left / 3)
    std = math.sqrt(std ** 2 + (basis * spot) ** 2)
    if std <= 0:
        return 1.0 if mean >= ref else 0.0
    return norm_cdf((mean - ref) / std)


def features(kl, start, t, total):
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
    avg = sum(obs) / len(obs) if obs else spot
    return ref, spot, avg, max(sd / math.sqrt(60), 2e-5)


async def load(hours):
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    now = int(time.time())
    t0 = now - hours * 3600
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        kl = {a: await bt.klines(s, a, (t0 - 4000) * 1000, now * 1000) for a in bt.ASSETS}
        sem = asyncio.Semaphore(12)
        jobs = []
        for a in bt.ASSETS:
            for tf, step in TF_SECONDS.items():
                st = t0 - t0 % step
                while st + step < now - 1800:
                    jobs.append(bt.window_data(s, sem, a, tf, st))
                    st += step
        wins = [w for w in await asyncio.gather(*jobs) if w]
    pickle.dump((kl, wins, t0, now), open(CACHE, "wb"))
    return kl, wins, t0, now


BASES = (0.0, 1e-4, 2e-4, 4e-4, 8e-4)


def build(kl, wins):
    for w in wins:
        total = TF_SECONDS[w["tf"]]
        out = []
        for t, mid in w["hist"]:
            el = t - w["start"]
            left = total - el
            if el < MIN_ELAPSED_S or left < 5:
                continue
            f = features(kl[w["asset"]], w["start"], t, total)
            if not f:
                continue
            ref, spot, avg, sig = f
            out.append({"t": t, "left": left, "mid": mid, "p": {b: p_twap(ref, spot, avg, left, sig, b) for b in BASES}})
        w["pts"] = out
    return wins


def brier(wins, b, lo, hi, maxleft=10 ** 9):
    n = s = sm = 0
    for w in wins:
        if not (lo <= w["start"] < hi):
            continue
        y = 1.0 if w["up_won"] else 0.0
        for q in w["pts"]:
            if q["left"] > maxleft:
                continue
            s += (q["p"][b] - y) ** 2
            sm += (q["mid"] - y) ** 2
            n += 1
    return s / n, sm / n, n


def simulate(wins, b, lo, hi, th=0.05, lock=None, margin=0.01, use_rule=True, filt=None):
    res = []
    for w in wins:
        if not (lo <= w["start"] < hi):
            continue
        pts = w["pts"]
        for i, q in enumerate(pts):
            if q["left"] < MIN_LEFT_S:
                continue
            p = q["p"][b]
            mid = q["mid"]
            cands = []
            for side, ask, pp in (("UP", min(0.99, mid + SPREAD), p), ("DOWN", min(0.99, 1 - mid + SPREAD), 1 - p)):
                if not (MIN_ASK <= ask <= MAX_ASK):
                    continue
                cost = ask + taker_fee_per_share(ask)
                cands.append(((pp - MODEL_HAIRCUT) - cost, side, ask, cost))
            if not cands:
                continue
            edge, side, ask, cost = max(cands)
            if edge < th:
                continue
            meta = {"tf": w["tf"], "asset": w["asset"], "ask": ask, "left": q["left"], "edge": edge, "t": w["start"]}
            if filt and not filt(meta):
                continue
            won = w["up_won"] if side == "UP" else not w["up_won"]
            hold = (1.0 if won else 0.0) - cost
            ex = hold
            reason = "settle"
            for q2 in pts[i + 1:]:
                if q2["left"] < 5:
                    continue
                fair = q2["p"][b] if side == "UP" else 1 - q2["p"][b]
                bid = (q2["mid"] if side == "UP" else 1 - q2["mid"]) - SPREAD
                if bid <= 0.01:
                    continue
                nb = bid - taker_fee_per_share(bid)
                if lock is not None and nb >= cost * (1 + lock):
                    ex = nb - cost
                    reason = "lock"
                    break
                if use_rule and nb >= fair + margin:
                    ex = nb - cost
                    reason = "rule"
                    break
            res.append((cost, hold, ex, meta, reason))
            break
    return res


def roi(tr):
    if not tr:
        return "n=0"
    c = sum(t[0] for t in tr)
    return (f"n={len(tr):4d} hold={100 * sum(t[1] for t in tr) / c:+6.1f}% "
            f"exit={100 * sum(t[2] for t in tr) / c:+6.1f}% win={100 * sum(1 for t in tr if t[2] > 0) / len(tr):3.0f}%")


async def main():
    kl, wins, t0, now = await load(96)
    wins = build(kl, wins)
    mid_t = t0 + (now - t0) // 2
    TR = (t0, mid_t)
    TE = (mid_t, now + 1)
    print(f"windows={len(wins)} (train = older 48h | test = recent 48h)", flush=True)
    print("\n== A) BASIS NOISE (Brier; lower=better) ==")
    for b in BASES:
        a = brier(wins, b, *TR, maxleft=120)
        c = brier(wins, b, *TE, maxleft=120)
        a2 = brier(wins, b, *TR)
        c2 = brier(wins, b, *TE)
        print(f"basis={b:.4f} | left<=120s: train {a[0]:.4f} (mkt {a[1]:.4f}) test {c[0]:.4f} (mkt {c[1]:.4f}) | all: train {a2[0]:.4f} test {c2[0]:.4f}")
    best_b = min(BASES, key=lambda b: brier(wins, b, *TR, maxleft=120)[0])
    print(f"-> basis chosen on TRAIN: {best_b}", flush=True)
    print("\n== B) return at edge>=0.05 (current exit rule) ==")
    for b in sorted({0.0, best_b}):
        for nm, rg in (("train", TR), ("test", TE)):
            print(f"basis={b:.4f} {nm:5s}", roi(simulate(wins, b, *rg)))

    def bucket(name, fn, labels):
        for lab in labels:
            a = simulate(wins, best_b, *TR, filt=lambda m, lab=lab: fn(m) == lab)
            c = simulate(wins, best_b, *TE, filt=lambda m, lab=lab: fn(m) == lab)
            print(f"{name:7s} {lab:10s} TRAIN {roi(a)} || TEST {roi(c)}")

    print("\n== C) by bucket ==")
    ab = lambda m: "ask<0.15" if m["ask"] < 0.15 else "0.15-0.35" if m["ask"] < 0.35 else "0.35-0.65" if m["ask"] < 0.65 else "0.65-0.85" if m["ask"] < 0.85 else "ask>0.85"
    lb = lambda m: "left<60" if m["left"] < 60 else "60-150" if m["left"] < 150 else "150-300" if m["left"] < 300 else "left>300"
    eb = lambda m: "e5-8%" if m["edge"] < 0.08 else "e8-12%" if m["edge"] < 0.12 else "e12-20%" if m["edge"] < 0.2 else "e>20%"
    bucket("ask", ab, ["ask<0.15", "0.15-0.35", "0.35-0.65", "0.65-0.85", "ask>0.85"])
    bucket("time", lb, ["left<60", "60-150", "150-300", "left>300"])
    bucket("edge", eb, ["e5-8%", "e8-12%", "e12-20%", "e>20%"])
    bucket("tf", lambda m: m["tf"], ["5m", "15m"])
    bucket("asset", lambda m: m["asset"], ["BTC", "ETH"])
    print("\n== D) exits ==")
    for nm, kw in (("hold", dict(use_rule=False)), ("rule m=+0.01 (current)", dict()), ("rule m=+0.03", dict(margin=0.03)),
                   ("rule m=-0.03", dict(margin=-0.03)), ("rule m=-0.08", dict(margin=-0.08)),
                   ("rule+lock 15%", dict(lock=0.15)), ("rule+lock 30%", dict(lock=0.30)),
                   ("rule+lock 50%", dict(lock=0.50)), ("rule+lock 80%", dict(lock=0.80)),
                   ("lock only 30%", dict(lock=0.30, use_rule=False)), ("lock only 50%", dict(lock=0.50, use_rule=False))):
        a = simulate(wins, best_b, *TR, **kw)
        c = simulate(wins, best_b, *TE, **kw)
        print(f"{nm:24s} TRAIN {roi(a)} || TEST {roi(c)}")
    print("\n== E) edge threshold (current rule) ==")
    for th in (0.03, 0.05, 0.08, 0.12, 0.16):
        a = simulate(wins, best_b, *TR, th=th)
        c = simulate(wins, best_b, *TE, th=th)
        print(f"edge>={th:.2f} TRAIN {roi(a)} || TEST {roi(c)}")


def _streak(tr):
    worst = cur = 0
    for t in sorted(tr, key=lambda x: x[3].get("t", 0)):
        if t[2] <= 0:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def _line(tr):
    if not tr:
        return "n=0"
    c = sum(t[0] for t in tr)
    return (f"n={len(tr):4d} ROI={100 * sum(t[2] for t in tr) / c:+6.1f}% "
            f"win={100 * sum(1 for t in tr if t[2] > 0) / len(tr):3.0f}% worst_loss_streak={_streak(tr):2d}")


async def combos():
    kl, wins, t0, now = await load(96)
    wins = build(kl, wins)
    mid = t0 + (now - t0) // 2
    TR, TE = (t0, mid), (mid, now + 1)
    print("")
    print("== F) final combinations (basis 1e-4) ==")
    ask15 = lambda m: m["ask"] >= 0.15
    for nm, kw in (
        ("edge>=5% (with current MIN_ASK 0.15)", dict(th=0.05)),
        ("edge>=8% + ask>=0.15", dict(th=0.08, filt=ask15)),
        ("edge>=10% + ask>=0.15", dict(th=0.10, filt=ask15)),
        ("edge>=8% + ask>=0.15 + lock 80%", dict(th=0.08, lock=0.80, filt=ask15)),
        ("edge>=8% + ask>=0.15 + lock 50%", dict(th=0.08, lock=0.50, filt=ask15)),
        ("edge>=8% + ask>=0.15 + lock 30% (default)", dict(th=0.08, lock=0.30, filt=ask15)),
        ("edge>=8% + ask>=0.15 + lock 15%", dict(th=0.08, lock=0.15, filt=ask15)),
    ):
        print(nm)
        print("    TRAIN " + _line(simulate(wins, 1e-4, *TR, **kw)))
        print("    TEST  " + _line(simulate(wins, 1e-4, *TE, **kw)))


async def run_all():
    await main()
    await combos()


if __name__ == "__main__":
    asyncio.run(run_all())
