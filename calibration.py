"""
Live audit: compare what the bot predicted with what Polymarket actually resolved.

Reads logs/calibration.csv (predictions and the book about every 10 s per window) and logs/outcomes.csv (real outcome):
  1) Accuracy of the resolution rule (Chainlink TWAP of the last 60 s >= reference vs the real outcome).
  2) Model Brier score versus the market, by asset and timeframe (lower is better).
  3) SIMULATION ON REAL DATA: enters with the same rules as the bot (exact reference, >=30 s after the open,
     >=20 s left, ask between 0.15 and 0.95, net edge >= --min-edge) and tries exit rules using the real
     bid/ask recorded every 10 s. This is the most reliable evidence for choosing LOCK_PROFIT_PCT and comparing
     it with holding to expiry.

Usage:  python calibration.py [--min-edge 0.08]
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

FEE_RATE = 0.07
TF_SECS = {"5m": 300, "15m": 900, "4h": 14400}


def fee(p):
    return FEE_RATE * p * (1 - p)


def load(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def simulate(series, outcome, min_edge, lock=None, rule=True, margin=0.01, slip=0.0, tfs=("5m", "15m"), assets=None,
             pcol="p_model"):
    """One entry per window; returns [(cost, pnl, won, reason, asset, tf)]."""
    res = []
    for slug, rows in series.items():
        o = outcome.get(slug)
        if not o:
            continue
        tf = rows[0]["tf"]
        if tf not in tfs or (assets and rows[0]["asset"] not in assets):
            continue
        y_up = o["actual_up"] == "1"
        for i, r in enumerate(rows):
            if r["ref_kind"] != "chainlink":
                continue
            left = fl(r["left_s"])
            p, a_up, a_dn = fl(r.get(pcol)), fl(r["ask_up"]), fl(r["ask_dn"])
            if None in (left, p, a_up, a_dn) or left < 20 or TF_SECS[tf] - left < 30:
                continue
            best = None
            for side, ask, pp in (("UP", a_up, p), ("DOWN", a_dn, 1 - p)):
                if 0.15 <= ask <= 0.95:
                    cost = ask + slip + fee(ask + slip)
                    e = (pp - 0.02) - cost
                    if best is None or e > best[0]:
                        best = (e, side, cost)
            if best is None or best[0] < min_edge:
                continue
            _, side, cost = best
            won = y_up if side == "UP" else not y_up
            pnl, why = (1.0 if won else 0.0) - cost, "expiry"
            for r2 in rows[i + 1:]:
                l2, p2 = fl(r2["left_s"]), fl(r2.get(pcol))
                bid = fl(r2["bid_up"] if side == "UP" else r2["bid_dn"])
                if l2 is None or p2 is None or bid is None or l2 < 5 or bid <= 0.01:
                    continue
                nb = bid - fee(bid)
                if lock is not None and nb >= cost * (1 + lock):
                    pnl, why = nb - cost, "lock"
                    break
                fair = p2 if side == "UP" else 1 - p2
                if rule and nb >= fair + margin:
                    pnl, why = nb - cost, "fair value"
                    break
            res.append((cost, pnl, pnl > 0, why, rows[0]["asset"], tf))
            break
    return res


def fmt(res):
    if not res:
        return "n=  0"
    c = sum(x[0] for x in res)
    return f"n={len(res):3d}  ROI={100 * sum(x[1] for x in res) / c:+7.1f}%  hits={100 * sum(x[2] for x in res) / len(res):3.0f}%  P&L/trade={sum(x[1] for x in res) / len(res):+.3f}"


def main():
    min_edge = 0.08
    if "--min-edge" in sys.argv:
        min_edge = float(sys.argv[sys.argv.index("--min-edge") + 1])
    base = Path(__file__).parent / "logs"
    if not base.exists():
        base = Path.home() / "polymarket-logs"
    snaps, outs = load(base / "calibration.csv"), load(base / "outcomes.csv")
    outcome = {o["slug"]: o for o in outs}
    print(f"Windows with a real outcome: {len(outcome)}   |   snapshots: {len(snaps)}   ({base})")
    if not outcome:
        print("No outcomes yet: leave the bot running (5 min windows resolve about 1 min after they close).")
        return

    print("\n== 1) Resolution rule: Chainlink 60 s TWAP >= reference  vs  real outcome ==")
    grp = defaultdict(lambda: [0, 0])
    for o in outcome.values():
        if o["pred_up"] == "":
            continue
        k = (o["asset"], o["ref_kind"])
        grp[k][1] += 1
        grp[k][0] += int(o["pred_up"] == o["actual_up"])
    for (a, kind), (ok, n) in sorted(grp.items()):
        print(f"  {a:5s} ref={kind:9s} hits {100 * ok / n:5.1f}%  (n={n})")

    series = defaultdict(list)
    seen = set()
    for r in snaps:
        key = (r["slug"], r["ts"])
        if key in seen:
            continue
        seen.add(key)
        series[r["slug"]].append(r)
    for rows in series.values():
        rows.sort(key=lambda r: float(r["ts"]))

    print("\n== 2) Model vs market Brier (exact reference only; lower is better) ==")
    by = defaultdict(lambda: [0.0, 0.0, 0])
    for slug, rows in series.items():
        o = outcome.get(slug)
        if not o:
            continue
        y = float(o["actual_up"])
        for r in rows:
            p, au, bu = fl(r["p_model"]), fl(r["ask_up"]), fl(r["bid_up"])
            if r["ref_kind"] != "chainlink" or p is None or au is None:
                continue
            mid = (au + bu) / 2 if bu is not None else au
            g = by[(r["asset"], r["tf"])]
            g[0] += (p - y) ** 2
            g[1] += (mid - y) ** 2
            g[2] += 1
    print(f"  {'asset':6s} {'tf':5s} {'points':>7s} {'model':>8s} {'market':>8s}")
    for (a, tf), g in sorted(by.items()):
        if g[2]:
            print(f"  {a:6s} {tf:5s} {g[2]:>7d} {g[0] / g[2]:>8.4f} {g[1] / g[2]:>8.4f}")

    print(f"\n== 3) Simulation on real books every 10 s (edge >= {min_edge:.0%}, exact ref, same entry logic as the bot) ==")
    variants = (
        ("hold to expiry", dict(lock=None, rule=False)),
        ("fair value only (no lock)", dict(lock=None, rule=True)),
        ("lock +30% + fair value (default)", dict(lock=0.30, rule=True)),
        ("lock +50% + fair value", dict(lock=0.50, rule=True)),
        ("lock +100% + fair value", dict(lock=1.00, rule=True)),
    )
    for tfs, title in ((("5m", "15m"), "5m + 15m"), (("5m",), "5m only"), (("15m",), "15m only")):
        print(f"  -- {title} --")
        for name, kw in variants:
            print(f"     {name:38s} {fmt(simulate(series, outcome, min_edge, tfs=tfs, **kw))}")
    if any(r.get("p_chainlink") for r in snaps):
        print("\n== 4) A/B on the SAME windows: Chainlink-only price vs blend with exchanges ==")
        both = {s: [r for r in rows if r.get("p_chainlink") and r.get("p_blend")] for s, rows in series.items()}
        both = {s: rows for s, rows in both.items() if rows}
        acc = {"p_chainlink": [0.0, 0], "p_blend": [0.0, 0]}
        mk = [0.0, 0]
        for s, rows in both.items():
            o = outcome.get(s)
            if not o:
                continue
            y = float(o["actual_up"])
            for r in rows:
                if r["ref_kind"] != "chainlink" or TF_SECS[r["tf"]] - (fl(r["left_s"]) or 0) < 30:
                    continue
                au, bu = fl(r["ask_up"]), fl(r["bid_up"])
                if au is None or bu is None:
                    continue
                for col in acc:
                    acc[col][0] += (float(r[col]) - y) ** 2
                    acc[col][1] += 1
                mk[0] += ((au + bu) / 2 - y) ** 2
                mk[1] += 1
        if mk[1]:
            print(f"  Brier ({mk[1]} points, lower is better):  Chainlink only {acc['p_chainlink'][0] / mk[1]:.4f}   "
                  f"blend {acc['p_blend'][0] / mk[1]:.4f}   market {mk[0] / mk[1]:.4f}")
        for name, col in (("Chainlink only", "p_chainlink"), ("exchange blend", "p_blend")):
            d = simulate(both, outcome, min_edge, lock=0.30, rule=True, pcol=col)
            h = simulate(both, outcome, min_edge, lock=None, rule=False, pcol=col)
            print(f"  {name:17s} collect 30%: {fmt(d)}   |   hold: {fmt(h)}")
    print("  -- by asset (lock +30% + fair value | hold) --")
    for a in sorted({rows[0]['asset'] for rows in series.values()}):
        d = simulate(series, outcome, min_edge, lock=0.30, rule=True, assets={a})
        h = simulate(series, outcome, min_edge, lock=None, rule=False, assets={a})
        print(f"     {a:5s} default: {fmt(d)}   |   hold: {fmt(h)}")
    print("\nRough bar for trading an asset/timeframe: >= 100 resolved windows, and a positive, stable simulated ROI.")


if __name__ == "__main__":
    main()
