#!/usr/bin/env python3
"""Mon Desk : backtest du fondamental chiffré (taux directeurs).

G1 portage : chaque lundi, les K paires au plus grand écart de taux, dans le sens du portage positif, gardées jusqu'au vendredi
   20 h UTC (stop 3 ATR D1). Le swap est compté : écart de taux − marge du courtier (1 %/an), en R.
G2 virage des banques centrales : les K paires dont l'écart de taux a le plus bougé sur 6 mois (sens du durcissement relatif).
G3 score combiné : écart de taux + virage sur 6 mois + momentum des prix 60 j (chacun ramené à son rang).
G4 filtre : on ne garde que les trades des autres méthodes (retour à la moyenne, momentum, méthode actuelle M15/H1)
   qui vont dans le sens du portage (ou du virage).
Résultats en R (1 R = 1 % du compte avec 1 % de risque par trade).
"""
import argparse, gzip, json, math, os
from datetime import datetime, timedelta, timezone

import engine
import methods_bt as M
import rates

UTC = timezone.utc
MARKUP = 1.0   # % par an retenu par le courtier sur le swap


def swap_R(pair, up, e, risk, t0, t1):
    d = rates.diff(pair, t0) * (1 if up else -1)
    days = max(0, (t1 - t0) / 86400)
    carry_pct = (d - MARKUP) if d > 0 else (d - MARKUP)  # le courtier prend sa marge dans les deux sens
    return carry_pct / 100 * days / 365 * e / risk


def weekly(datas, pick, K, k_atr=3, name=""):
    out = []
    t = M.START
    while t.weekday() != 0:
        t += timedelta(days=1)
    end = datetime.fromtimestamp(datas["EURUSD"].h1t[-1], UTC)
    while t + timedelta(days=7) <= end:
        ts = int(t.timestamp())
        for pair, d in pick(ts)[:K]:
            D = datas[pair]
            j = M.h1_index_at(D, ts); k = D.d1_closed_index(ts)
            if j >= len(D.h1) or k < 20:
                continue
            up = d == "buy"; e = D.h1[j]["o"]
            stop = e - k_atr * D.d1_atr[k] if up else e + k_atr * D.d1_atr[k]
            res = M.simulate(D, j, up, stop, {"until": ts + 4 * 86400 + 20 * 3600}, max_days=8)
            if res:
                risk = k_atr * D.d1_atr[k]
                sw = swap_R(pair, up, e, risk, ts, res[1])
                out.append({"pair": pair, "dir": d, "t": engine.iso(t), "R": round(res[0] + sw, 3), "swapR": round(sw, 3),
                            "closeTs": res[1], "reason": res[2]})
        t += timedelta(days=7)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", required=True)
    ap.add_argument("--trades", nargs="*", default=[], help="fichiers trades.json(.gz) des autres backtests à filtrer")
    ap.add_argument("--methods", help="résultats de methods_bt.py à filtrer")
    ap.add_argument("--out", default="results_fund.json")
    a = ap.parse_args()
    pairs = sorted({f.split("_")[0] for f in os.listdir(a.hist) if f.endswith("_1h.json.gz")})
    datas = {p: M.Data(a.hist, p) for p in pairs}
    for D in datas.values():
        D.h4t = [r["ts"] for r in D.h4]
    fx = [p for p in pairs if not p.startswith("XAU")]
    S = M.Strength(datas)

    def carry_pick(ts):
        rows = sorted(((abs(rates.diff(p, ts)), p, "buy" if rates.diff(p, ts) > 0 else "sell") for p in fx), reverse=True)
        return [(p, d) for _, p, d in rows]

    def shift_pick(ts, months=6):
        rows = []
        for p in fx:
            ch = rates.diff(p, ts) - rates.diff(p, ts - months * 30 * 86400)
            if abs(ch) > 0.01:
                rows.append((abs(ch), p, "buy" if ch > 0 else "sell"))
        rows.sort(reverse=True)
        return [(p, d) for _, p, d in rows]

    def combo_pick(ts):
        mom = S.scores(ts, 60)
        rows = []
        for p in fx:
            c = rates.diff(p, ts)
            ch = c - rates.diff(p, ts - 180 * 86400)
            m = mom.get(p[:3], 0) - mom.get(p[3:], 0)
            rows.append((p, c, ch, m))
        def rank(i):
            vals = sorted(r[i] for r in rows)
            return {r[0]: (vals.index(r[i]) / (len(vals) - 1) - 0.5) * 2 for r in rows}
        rc, rch, rm = rank(1), rank(2), rank(3)
        sc = sorted(((rc[p] + rch[p] + rm[p], p) for p, *_ in rows), key=lambda x: -abs(x[0]))
        return [(p, "buy" if s > 0 else "sell") for s, p in sc]

    res = []
    for K in (3, 6):
        for nm, pk in (("G1 portage", carry_pick), ("G2 virage des banques centrales 6 mois", shift_pick), ("G3 score combiné taux + virage + momentum", combo_pick)):
            tr = weekly(datas, pk, K)
            st = M.split(tr)
            res.append({"name": f"{nm} · {K} paires · 1 semaine · stop 3 ATR", "stats": st, "trades": tr})
            print(res[-1]["name"], "|", json.dumps(st["train"]), "|", json.dumps(st["test"]), flush=True)

    # G4 : filtre de sens sur les autres méthodes
    def aligned(pair, d, t_iso, mode):
        if pair.startswith("XAU"):
            return None
        ts = int(engine.parse_t(t_iso).timestamp())
        if mode == "portage":
            v = rates.diff(pair, ts)
        else:
            v = rates.diff(pair, ts) - rates.diff(pair, ts - 180 * 86400)
        if abs(v) < 0.01:
            return None
        return (v > 0) == (d == "buy")

    sources = []
    if a.methods:
        for r in json.load(open(a.methods)):
            sources.append((r["name"], r["trades"], "methods"))
    for f in a.trades:
        rows = json.load(gzip.open(f)) if f.endswith(".gz") else json.load(open(f))
        for v in sorted({r["variant"] for r in rows}):
            for rr in (1.5, 3):
                X = [dict(r, closeTs=int(engine.parse_t(r["closedAt"]).timestamp()) if r.get("closedAt") else 0)
                     for r in rows if r["variant"] == v and r["traded"] and r["rr"] >= rr and r["tech"] >= 60 and r["setup"] in ("S1", "S2")]
                sources.append((f"{os.path.basename(os.path.dirname(f))} {v} S1+S2 RR≥{rr}", X, "engine"))
    for name, X, kind in sources:
        for mode in ("portage", "virage"):
            Y = [x for x in X if aligned(x["pair"], x["dir"], x["t"], mode)]
            Z = [x for x in X if aligned(x["pair"], x["dir"], x["t"], mode) is False]
            st, sz = M.split(Y), M.split(Z)
            res.append({"name": f"G4 {name} · seulement dans le sens du {mode}", "stats": st, "against": sz, "base": M.split(X)})
            print(res[-1]["name"], "| avec", json.dumps(st["train"]), json.dumps(st["test"]), "| contre", sz["all"].get("avgR"), flush=True)
    json.dump(res, open(a.out, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main()
