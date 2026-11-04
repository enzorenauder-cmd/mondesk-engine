#!/usr/bin/env python3
"""Mon Desk : backtest de méthodes alternatives (suivi de tendance, retour dans la tendance, momentum des devises, mixte).

Signaux calculés uniquement sur des bougies CLÔTURÉES (D1 ou H4), exécution simulée sur les bougies H1 qui suivent
(entrée à l'ouverture de la première bougie H1 après la clôture du signal ; si le stop et l'objectif sont touchés
dans la même bougie H1, on compte le stop). Spread déduit (même estimation que le moteur), swap non compté.
Résultats en R (1 R = risque initial) : avec 1 % de risque par trade, 1 R = 1 % du compte.

Usage : python3 methods_bt.py --hist hist --out results_methods.json
"""
import argparse, bisect, gzip, json, math, os
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool

import engine

UTC = timezone.utc
START = datetime(2023, 3, 15, tzinfo=UTC)   # 9 mois de D1 avant pour les moyennes
SPLIT = "2025-07-01"
CCY = ["EUR", "GBP", "USD", "JPY", "CHF", "CAD", "AUD", "NZD"]


def load(hist, pair, iv):
    p = os.path.join(hist, f"{pair}_{iv}.json.gz")
    if not os.path.exists(p):
        return []
    rows = json.load(gzip.open(p))
    rows.sort(key=lambda r: r["t"])
    for r in rows:
        r["ts"] = int(engine.parse_t(r["t"]).timestamp())
    return rows


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def atr_list(rows, n=20):
    out, prev, a = [], None, None
    for r in rows:
        tr = r["h"] - r["l"] if prev is None else max(r["h"] - r["l"], abs(r["h"] - prev), abs(r["l"] - prev))
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
        prev = r["c"]
    return out


class Data:
    def __init__(self, hist, pair):
        self.pair = pair
        self.d1 = load(hist, pair, "1day")
        self.h4 = load(hist, pair, "4h")
        self.h1 = load(hist, pair, "1h")
        self.h1t = [r["ts"] for r in self.h1]
        self.d1t = [r["ts"] for r in self.d1]
        c = [r["c"] for r in self.d1]
        self.d1_ema50, self.d1_ema100, self.d1_ema200 = ema(c, 50), ema(c, 100), ema(c, 200)
        self.d1_ema20 = ema(c, 20)
        self.d1_atr = atr_list(self.d1, 20)
        c4 = [r["c"] for r in self.h4]
        self.h4_ema20, self.h4_ema50 = ema(c4, 20), ema(c4, 50)
        self.h4_atr = atr_list(self.h4, 14)
        self.sp = engine.spread(pair)

    def d1_closed_index(self, ts):
        """index du dernier D1 clôturé à l'instant ts (bougie D1 du jour J clôturée à J+1 00:00 UTC)"""
        return bisect.bisect_right(self.d1t, ts - 86400) - 1


def simulate(D, i_entry, up, stop, exit_rule, max_days=40):
    """Suit un trade sur les bougies H1 à partir de l'indice i_entry. Renvoie (R, closeTs, reason)."""
    h1 = D.h1
    if i_entry >= len(h1):
        return None
    sgn = 1 if up else -1
    e = h1[i_entry]["o"]
    risk = (e - stop) * sgn
    if risk <= 0:
        return None
    cost = D.sp / risk
    tp = e + sgn * exit_rule.get("rr", 0) * risk if exit_rule.get("rr") else exit_rule.get("tp_price")
    trail_k = exit_rule.get("trail")         # chandelier en ATR D1 (mis à jour à chaque clôture D1)
    trail4_k = exit_rule.get("trail4")       # chandelier en ATR H4 (mis à jour à chaque clôture H4)
    don = exit_rule.get("don")               # sortie sur cassure du plus bas (plus haut) de N jours, à la clôture D1
    until = exit_rule.get("until")           # sortie à une date (momentum hebdomadaire)
    best = e
    t_end = h1[i_entry]["ts"] + max_days * 86400
    last_day = None
    last_h4 = None
    for j in range(i_entry, len(h1)):
        b = h1[j]
        if until and b["ts"] >= until:
            return ((b["o"] - e) * sgn / risk - cost, b["ts"], "date")
        if b["ts"] >= t_end:
            return ((b["o"] - e) * sgn / risk - cost, b["ts"], "durée max")
        # mises à jour du stop à la clôture des bougies D1 / H4 (avant d'utiliser la bougie H1 courante)
        day = b["ts"] // 86400
        if (trail_k or don) and day != last_day and j > i_entry:
            k = D.d1_closed_index(b["ts"])
            if k >= 20:
                if trail_k:
                    best = max(best, D.d1[k]["c"]) if up else min(best, D.d1[k]["c"])
                    ns = best - sgn * trail_k * D.d1_atr[k]
                    stop = max(stop, ns) if up else min(stop, ns)
                if don:
                    lo = min(r["l"] for r in D.d1[k - don + 1:k + 1]) if up else max(r["h"] for r in D.d1[k - don + 1:k + 1])
                    # sortie si la clôture D1 a franchi le plus bas (haut) des N jours précédents
                    prev = min(r["l"] for r in D.d1[k - don:k]) if up else max(r["h"] for r in D.d1[k - don:k])
                    if (D.d1[k]["c"] < prev) if up else (D.d1[k]["c"] > prev):
                        return ((b["o"] - e) * sgn / risk - cost, b["ts"], "sortie Donchian")
        last_day = day
        if trail4_k:
            h4i = bisect.bisect_right(D.h4t, b["ts"] - 4 * 3600) - 1
            if h4i != last_h4 and j > i_entry and h4i >= 20:
                best = max(best, D.h4[h4i]["c"]) if up else min(best, D.h4[h4i]["c"])
                ns = best - sgn * trail4_k * D.h4_atr[h4i]
                stop = max(stop, ns) if up else min(stop, ns)
            last_h4 = h4i
        # ouverture au-delà du stop (gap) : sortie à l'ouverture
        if (b["o"] <= stop) if up else (b["o"] >= stop):
            return ((b["o"] - e) * sgn / risk - cost, b["ts"], "stop (gap)")
        if (b["l"] <= stop) if up else (b["h"] >= stop):
            return ((stop - e) * sgn / risk - cost, b["ts"], "stop")
        if tp is not None and ((b["h"] >= tp) if up else (b["l"] <= tp)):
            return ((tp - e) * sgn / risk - cost, b["ts"], "objectif")
    return None


def h1_index_at(D, ts):
    return bisect.bisect_left(D.h1t, ts)


# ---------------- méthode A : cassure de plus haut / plus bas (suivi de tendance D1) ----------------
def method_breakout(D, p):
    out, busy_until = [], 0
    N, k0 = p["N"], p["k"]
    for i in range(max(N, 120), len(D.d1)):
        r = D.d1[i]
        ts_sig = r["ts"] + 86400   # clôture de la bougie D1
        if ts_sig < START.timestamp() or ts_sig < busy_until:
            continue
        hi = max(x["h"] for x in D.d1[i - N:i]); lo = min(x["l"] for x in D.d1[i - N:i])
        up = r["c"] > hi
        dn = r["c"] < lo
        if not (up or dn):
            continue
        if p.get("filt") == "ema" and not ((up and r["c"] > D.d1_ema100[i]) or (dn and r["c"] < D.d1_ema100[i])):
            continue
        j = h1_index_at(D, ts_sig)
        if j >= len(D.h1):
            break
        e = D.h1[j]["o"]
        stop = e - k0 * D.d1_atr[i] if up else e + k0 * D.d1_atr[i]
        res = simulate(D, j, up, stop, p["exit"], max_days=p.get("maxDays", 60))
        if res:
            out.append({"pair": D.pair, "dir": "buy" if up else "sell", "t": engine.iso(datetime.fromtimestamp(ts_sig, UTC)),
                        "R": round(res[0], 3), "closeTs": res[1], "reason": res[2]})
            busy_until = res[1]
    return out


# ---------------- méthode F : retour à la moyenne (on joue CONTRE la cassure de N jours) ----------------
def method_fade(D, p):
    out, busy_until = [], 0
    N, k0 = p["N"], p["k"]
    for i in range(max(N, 120), len(D.d1)):
        r = D.d1[i]
        ts_sig = r["ts"] + 86400
        if ts_sig < START.timestamp() or ts_sig < busy_until:
            continue
        hi = max(x["h"] for x in D.d1[i - N:i]); lo = min(x["l"] for x in D.d1[i - N:i])
        if r["c"] > hi:
            up = False
        elif r["c"] < lo:
            up = True
        else:
            continue
        if p.get("stretch") and abs(r["c"] - D.d1_ema20[i]) < p["stretch"] * D.d1_atr[i]:
            continue
        if p.get("noTrend") and ((r["c"] > D.d1_ema50[i] > D.d1_ema100[i]) if not up else (r["c"] < D.d1_ema50[i] < D.d1_ema100[i])):
            continue   # pas de contre-tendance quand la tendance de fond est nette dans le sens de la cassure
        j = h1_index_at(D, ts_sig)
        if j >= len(D.h1):
            break
        e = D.h1[j]["o"]
        a = D.d1_atr[i]
        stop = e - k0 * a if up else e + k0 * a
        rule = dict(p["exit"])
        if rule.get("mean"):
            tp = D.d1_ema20[i]
            if (tp - e) * (1 if up else -1) < 0.3 * k0 * a:
                continue
            rule = {"tp_price": tp}
        res = simulate(D, j, up, stop, rule, max_days=p.get("maxDays", 10))
        if res:
            out.append({"pair": D.pair, "dir": "buy" if up else "sell", "t": engine.iso(datetime.fromtimestamp(ts_sig, UTC)),
                        "R": round(res[0], 3), "closeTs": res[1], "reason": res[2]})
            busy_until = res[1]
    return out


# ---------------- méthode B : retour dans la tendance (D1 en tendance, entrée H4) ----------------
def method_pullback(D, p, allow=None):
    out, busy_until = [], 0
    D.h4t = [r["ts"] for r in D.h4]
    for i in range(60, len(D.h4)):
        r = D.h4[i]
        ts_sig = r["ts"] + 4 * 3600
        if ts_sig < START.timestamp() or ts_sig < busy_until:
            continue
        k = D.d1_closed_index(ts_sig)
        if k < 120:
            continue
        c1 = D.d1[k]["c"]
        up = c1 > D.d1_ema50[k] > D.d1_ema100[k]
        dn = c1 < D.d1_ema50[k] < D.d1_ema100[k]
        if not (up or dn):
            continue
        if allow is not None:
            a = allow(D.pair, ts_sig)
            if a is None or (a == "buy") != up:
                continue
        e20, a4 = D.h4_ema20[i], D.h4_atr[i]
        # le prix revient sur la moyenne 20 H4 puis une bougie H4 clôture dans le sens de la tendance
        if up and not (r["l"] <= e20 and r["c"] > e20 and r["c"] > r["o"] and r["c"] > D.h4_ema50[i]):
            continue
        if dn and not (r["h"] >= e20 and r["c"] < e20 and r["c"] < r["o"] and r["c"] < D.h4_ema50[i]):
            continue
        swing = min(x["l"] for x in D.h4[i - 5:i + 1]) - 0.2 * a4 if up else max(x["h"] for x in D.h4[i - 5:i + 1]) + 0.2 * a4
        j = h1_index_at(D, ts_sig)
        if j >= len(D.h1):
            break
        e = D.h1[j]["o"]
        risk = (e - swing) if up else (swing - e)
        if risk < 0.5 * a4:
            swing = e - 0.5 * a4 if up else e + 0.5 * a4
        if risk > 3 * a4:
            continue
        res = simulate(D, j, up, swing, p["exit"], max_days=p.get("maxDays", 20))
        if res:
            out.append({"pair": D.pair, "dir": "buy" if up else "sell", "t": engine.iso(datetime.fromtimestamp(ts_sig, UTC)),
                        "R": round(res[0], 3), "closeTs": res[1], "reason": res[2]})
            busy_until = res[1]
    return out


# ---------------- force des devises (momentum) ----------------
class Strength:
    def __init__(self, datas):
        self.d = {p: D for p, D in datas.items() if not p.startswith("XAU")}

    def scores(self, ts, L):
        acc = {c: [] for c in CCY}
        for p, D in self.d.items():
            k = D.d1_closed_index(ts)
            if k < L:
                continue
            ret = math.log(D.d1[k]["c"] / D.d1[k - L]["c"]) / max(D.d1_atr[k] / D.d1[k]["c"], 1e-6) / math.sqrt(L)
            acc[p[:3]].append(ret); acc[p[3:]].append(-ret)
        return {c: sum(v) / len(v) for c, v in acc.items() if v}

    def ranking(self, ts, L):
        s = self.scores(ts, L)
        rows = []
        for p in self.d:
            if p[:3] in s and p[3:] in s:
                diff = s[p[:3]] - s[p[3:]]
                rows.append((abs(diff), p, "buy" if diff > 0 else "sell"))
        rows.sort(reverse=True)
        return rows


def method_momentum(datas, p):
    """Chaque lundi : on prend les K paires où l'écart de force est le plus grand, dans le sens de la devise forte, pour une semaine."""
    S = Strength(datas)
    out = []
    any_d = datas["EURUSD"]
    t = START
    while t.weekday() != 0:
        t += timedelta(days=1)
    end = datetime.fromtimestamp(any_d.h1t[-1], UTC)
    while t + timedelta(days=7) <= end:
        ts = int(t.timestamp())
        for _, pair, d in S.ranking(ts, p["L"])[:p["K"]]:
            D = datas[pair]
            j = h1_index_at(D, ts)
            k = D.d1_closed_index(ts)
            if j >= len(D.h1) or k < 20:
                continue
            up = d == "buy"
            e = D.h1[j]["o"]
            stop = e - p["k"] * D.d1_atr[k] if up else e + p["k"] * D.d1_atr[k]
            res = simulate(D, j, up, stop, {"until": ts + 4 * 86400 + 20 * 3600, **p.get("exit", {})}, max_days=8)  # sortie vendredi 20 h UTC
            if res:
                out.append({"pair": pair, "dir": d, "t": engine.iso(t), "R": round(res[0], 3), "closeTs": res[1], "reason": res[2]})
        t += timedelta(days=p.get("hold", 7))
    return out


# ---------------- statistiques ----------------
def stats(tr):
    if not tr:
        return {"trades": 0}
    tr = sorted(tr, key=lambda x: x["closeTs"])
    R = [x["R"] for x in tr]
    w = [r for r in R if r > 0]; l = [r for r in R if r <= 0]
    eq = pk = dd = 0; s = ms = 0
    for r in R:
        eq += r; pk = max(pk, eq); dd = min(dd, eq - pk)
        s = s + 1 if r <= 0 else 0; ms = max(ms, s)
    t0 = engine.parse_t(tr[0]["t"]); t1 = datetime.fromtimestamp(tr[-1]["closeTs"], UTC)
    years = max((t1 - t0).days / 365.25, 0.25)
    return {"trades": len(R), "perYear": round(len(R) / years), "winRate": round(len(w) / len(R), 3),
            "avgWinR": round(sum(w) / len(w), 2) if w else 0, "avgLossR": round(sum(l) / len(l), 2) if l else 0,
            "avgR": round(sum(R) / len(R), 3), "totalR": round(eq, 1), "pctPerYear": round(eq / years, 1),
            "maxDD": round(dd, 1), "maxLosingStreak": ms,
            "profitFactor": round(sum(w) / -sum(l), 2) if l and sum(l) else None}


def split(tr):
    return {"all": stats(tr), "train": stats([x for x in tr if x["t"] < SPLIT]), "test": stats([x for x in tr if x["t"] >= SPLIT])}


def _load(args):
    hist, pair = args
    return pair, Data(hist, pair)


def run_one(args):
    name, kind, p = args
    datas = G["datas"]
    if kind == "breakout":
        tr = [x for D in datas.values() for x in method_breakout(D, p)]
    elif kind == "fade":
        tr = [x for D in datas.values() for x in method_fade(D, p)]
    elif kind == "pullback":
        tr = [x for D in datas.values() for x in method_pullback(D, p)]
    elif kind == "momentum":
        tr = method_momentum(datas, p)
    elif kind == "mixed":
        S = Strength(datas)
        cache = {}
        def allow(pair, ts, L=p["L"], K=p["K"]):
            wk = ts - (ts % (6 * 3600))
            key = (wk,)
            if key not in cache:
                cache[key] = {pp: d for _, pp, d in S.ranking(wk, L)[:K]}
            return cache[key].get(pair)
        tr = [x for D in datas.values() if not D.pair.startswith("XAU") for x in method_pullback(D, p, allow)]
    elif kind == "breakout_mom":
        S = Strength(datas)
        tr = []
        for D in datas.values():
            if D.pair.startswith("XAU"):
                continue
            for x in method_breakout(D, p):
                ts = int(engine.parse_t(x["t"]).timestamp())
                top = {pp: d for _, pp, d in S.ranking(ts, p["L"])[:p["K"]]}
                if top.get(D.pair) == x["dir"]:
                    tr.append(x)
    return name, kind, p, split(tr), tr


G = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default="hist")
    ap.add_argument("--out", default="results_methods.json")
    ap.add_argument("--only")
    a = ap.parse_args()
    pairs = sorted({f.split("_")[0] for f in os.listdir(a.hist) if f.endswith("_1h.json.gz")})
    with Pool(os.cpu_count() or 2) as pool:
        G["datas"] = dict(pool.map(_load, [(a.hist, p) for p in pairs]))
    for D in G["datas"].values():
        D.h4t = [r["ts"] for r in D.h4]
    grid = []
    for N in (20, 55):
        for k in (2, 3):
            for ex in ("trail", "don"):
                for filt in (None, "ema"):
                    exit_rule = {"trail": k} if ex == "trail" else {"don": max(10, N // 2)}
                    grid.append((f"A cassure {N} j · stop {k} ATR · {'stop suiveur' if ex == 'trail' else 'sortie ' + str(max(10, N // 2)) + ' j'}{' · filtre EMA100' if filt else ''}",
                                 "breakout", {"N": N, "k": k, "exit": exit_rule, "filt": filt}))
    for ex_name, ex in (("RR 2", {"rr": 2}), ("RR 3", {"rr": 3}), ("stop suiveur 3 ATR H4", {"trail4": 3}), ("stop suiveur 2 ATR D1", {"trail": 2})):
        grid.append((f"B retour dans la tendance H4 · {ex_name}", "pullback", {"exit": ex}))
    for L in (20, 60):
        for K in (3, 6):
            for k in (2, 3):
                grid.append((f"C momentum {L} j · {K} paires · stop {k} ATR · 1 semaine", "momentum", {"L": L, "K": K, "k": k}))
    for L in (20, 60):
        for K in (6, 10):
            for ex_name, ex in (("RR 2", {"rr": 2}), ("RR 3", {"rr": 3}), ("stop suiveur 3 ATR H4", {"trail4": 3})):
                grid.append((f"D mixte : force {L} j (top {K}) + retour H4 · {ex_name}", "mixed", {"L": L, "K": K, "exit": ex}))
    for N in (20, 55):
        for L in (20, 60):
            grid.append((f"E cassure {N} j + force {L} j (top 10) · stop 3 ATR · stop suiveur", "breakout_mom",
                         {"N": N, "k": 3, "exit": {"trail": 3}, "L": L, "K": 10}))
    for N in (10, 20):
        for k in (1.5, 2, 3):
            for ex_name, ex in (("objectif moyenne 20 j", {"mean": True}), ("RR 1", {"rr": 1}), ("RR 1,5", {"rr": 1.5})):
                for nt in (False, True):
                    grid.append((f"F retour à la moyenne · contre la cassure {N} j · stop {k} ATR · {ex_name}{' · sans tendance de fond' if nt else ''}",
                                 "fade", {"N": N, "k": k, "exit": ex, "noTrend": nt, "maxDays": 10}))
    if a.only:
        grid = [g for g in grid if g[0].startswith(tuple(a.only.split(",")))]
    res = []
    with Pool(os.cpu_count() or 2) as pool:
        for name, kind, p, st, tr in pool.imap_unordered(run_one, grid):
            print(name, "|", json.dumps(st["train"]), "|", json.dumps(st["test"]), flush=True)
            res.append({"name": name, "kind": kind, "params": p, "stats": st, "trades": tr})
    json.dump(res, open(a.out, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main()
