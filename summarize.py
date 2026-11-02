#!/usr/bin/env python3
"""Mon Desk : statistiques du backtest (trades.json -> summary.json).

Période d'apprentissage : jusqu'au 30/06/2025 ; période de contrôle : à partir du 01/07/2025.
Les réglages sont choisis sur l'apprentissage puis vérifiés, sans retouche, sur le contrôle.
"""
import argparse, json
from collections import defaultdict

SPLIT = "2025-07-01"
RR_GRID = [1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
TECH_GRID = [50, 60, 70, 80]


def stats(rows):
    sig = len(rows)
    t = sorted([r for r in rows if r["traded"]], key=lambda r: r["closedAt"] or r["t"])
    n = len(t)
    if not n:
        return {"signals": sig, "trades": 0}
    wins = [r["R"] for r in t if r["R"] > 0]
    losses = [r["R"] for r in t if r["R"] <= 0]
    eq = peak = dd = 0.0
    streak = maxstreak = 0
    for r in t:
        eq += r["R"]; peak = max(peak, eq); dd = min(dd, eq - peak)
        streak = streak + 1 if r["R"] <= 0 else 0
        maxstreak = max(maxstreak, streak)
    return {"signals": sig, "trades": n, "fillRate": round(n / sig, 3) if sig else None,
            "winRate": round(len(wins) / n, 3), "avgR": round(sum(r["R"] for r in t) / n, 3),
            "totalR": round(eq, 1), "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "maxLosingStreak": maxstreak, "maxDrawdownR": round(dd, 1)}


def group(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return {k: stats(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


def bucket(v, edges):
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"{lo}-{hi}"
    return f"{edges[-1]}+"


def analyse(rows):
    current = [r for r in rows if r["rr"] >= 1.5 and r["tech"] >= 60]
    train = [r for r in rows if r["t"] < SPLIT]
    test = [r for r in rows if r["t"] >= SPLIT]
    grid = []
    for rr in RR_GRID:
        for tm in TECH_GRID:
            f = lambda r: r["rr"] >= rr and r["tech"] >= tm
            grid.append({"rrMin": rr, "techMin": tm, "train": stats([r for r in train if f(r)]),
                         "test": stats([r for r in test if f(r)])})
    ok = [g for g in grid if g["train"].get("trades", 0) >= 100]
    best = max(ok, key=lambda g: g["train"]["avgR"]) if ok else None
    validity = {}
    for vh in ("6", "12", "24"):
        rv = [dict(r, traded=r["byValidity"][vh]["traded"], R=r["byValidity"][vh]["R"]) for r in current if "byValidity" in r]
        validity[vh + " h"] = {"all": stats(rv), "test": stats([r for r in rv if r["t"] >= SPLIT])}
    return {
        "all": stats(rows),
        "currentRules": {"rule": "RR >= 1,5 et score technique >= 60 (sans fondamental)", "all": stats(current),
                          "train": stats([r for r in current if r["t"] < SPLIT]), "test": stats([r for r in current if r["t"] >= SPLIT]),
                          "bySetup": group(current, lambda r: r["setup"]), "bySession": group(current, lambda r: r["session"]),
                          "byPair": group(current, lambda r: r["pair"]), "byYear": group(current, lambda r: r["t"][:4]),
                          "byOrder": group(current, lambda r: r["order"]), "byHour": group(current, lambda r: r["hourParis"])},
        "validity": validity,
        "bySetup": group(rows, lambda r: r["setup"]),
        "byRR": group(rows, lambda r: bucket(r["rr"], [0, 1, 1.5, 2, 3, 5])),
        "byTech": group(rows, lambda r: bucket(r["tech"], [0, 50, 60, 70, 80, 90])),
        "grid": grid,
        "bestOnTrain": best,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = [r for r in json.load(open(a.trades)) if r["rr"] > 0]
    variants = sorted({r.get("variant", "base") for r in rows})
    out = {"period": {"from": min(r["t"] for r in rows) if rows else None, "to": max(r["t"] for r in rows) if rows else None,
                      "split": SPLIT}, "variants": {}}
    for v in variants:
        out["variants"][v] = analyse([r for r in rows if r.get("variant", "base") == v])
        c = out["variants"][v]["currentRules"]; b = out["variants"][v]["bestOnTrain"]
        print(v, "| règles actuelles :", json.dumps(c["all"]), "| contrôle :", json.dumps(c["test"]))
        if b:
            print("   meilleur réglage :", b["rrMin"], b["techMin"], "train", json.dumps(b["train"]), "-> contrôle", json.dumps(b["test"]))
    json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
