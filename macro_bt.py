#!/usr/bin/env python3
"""Mon Desk : backtest des briques fondamentales chiffrées (rendements à 2 ans, COT, VIX), en paniers hebdomadaires.

Chaque lundi : score de chaque devise, on prend les K paires au plus grand écart de score (devise forte contre faible),
gardées jusqu'au vendredi 20 h UTC, stop 3 ATR D1, swap compté (écart de taux directeurs − 1 %/an).
Rien du futur : rendements connus la veille, COT publié le vendredi pour le mardi (utilisé à partir du lundi suivant).

Usage : BT_START=2020-03-02 python3 macro_bt.py --hist hist --macro macro --out results_macro.json
"""
import argparse, bisect, json, math, os, statistics as st
from datetime import datetime, timedelta, timezone

import engine
import methods_bt as M
import rates
from fund_bt import swap_R

UTC = timezone.utc
CCY = M.CCY


def ts_of(d):
    return int(datetime.strptime(d[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


class Series:
    def __init__(self, rows):
        self.t = [ts_of(d) for d, _ in rows]
        self.v = [v for _, v in rows]

    def at(self, ts, lag=86400):
        i = bisect.bisect_right(self.t, ts - lag) - 1
        return (self.v[i], self.t[i]) if i >= 0 else (None, None)


class Macro:
    def __init__(self, path):
        self.y2 = {}
        for c in CCY:
            p = os.path.join(path, f"Y2_{c}.json")
            self.y2[c] = Series(json.load(open(p)))
        self.vix = Series(json.load(open(os.path.join(path, "VIX.json"))))
        cot = {}
        for r in json.load(open(os.path.join(path, "COT.json"))):
            cot.setdefault(r["ccy"], {})[r["date"]] = r   # une ligne par date (les marchés renommés se recouvrent)
        self.cot = {c: sorted(v.values(), key=lambda r: r["date"]) for c, v in cot.items()}
        self.cot_t = {c: [ts_of(r["date"]) + 4 * 86400 for r in v] for c, v in self.cot.items()}   # mardi -> publié vendredi, utilisé lundi

    def y2_at(self, c, ts):
        """rendement à 2 ans (ou taux 3 mois mensuel) ; après la fin de la série, on prolonge avec les variations du taux directeur"""
        v, t0 = self.y2[c].at(ts)
        if v is None:
            return None
        if ts - t0 > 40 * 86400:
            v += rates.rate(c, ts) - rates.rate(c, t0)
        return v

    def cot_z(self, c, ts, kind="levNet", weeks=52):
        if c not in self.cot:
            return None
        i = bisect.bisect_right(self.cot_t[c], ts) - 1
        if i < weeks:
            return None
        hist = [r[kind] / r["oi"] for r in self.cot[c][i - weeks + 1:i + 1]]
        mu, sd = st.mean(hist), st.pstdev(hist)
        return (hist[-1] - mu) / sd if sd else 0.0

    def cot_change(self, c, ts, kind="levNet", weeks=4):
        if c not in self.cot:
            return None
        i = bisect.bisect_right(self.cot_t[c], ts) - 1
        if i < weeks:
            return None
        a, b = self.cot[c][i], self.cot[c][i - weeks]
        return (a[kind] - b[kind]) / a["oi"]


def ranks(d):
    """ramène un score par devise à son rang entre −1 et +1"""
    ks = [k for k, v in d.items() if v is not None]
    if len(ks) < 6:
        return {}
    srt = sorted(ks, key=lambda k: d[k])
    return {k: (i / (len(srt) - 1) - 0.5) * 2 for i, k in enumerate(srt)}


def make_pick(fx, score_fn):
    def pick(ts):
        sc = score_fn(ts)
        if not sc:
            return []
        rows = []
        for p in fx:
            if p[:3] in sc and p[3:] in sc:
                dlt = sc[p[:3]] - sc[p[3:]]
                rows.append((abs(dlt), p, "buy" if dlt > 0 else "sell"))
        rows.sort(reverse=True)
        out, used = [], {}
        for _, p, d in rows:   # pas plus de 2 paires sur la même devise
            if used.get(p[:3], 0) >= 2 or used.get(p[3:], 0) >= 2:
                continue
            out.append((p, d)); used[p[:3]] = used.get(p[:3], 0) + 1; used[p[3:]] = used.get(p[3:], 0) + 1
        return out
    return pick


def periods(tr):
    P = {"2020-2022": [x for x in tr if x["t"] < "2023-01-01"],
         "2023-mi2025": [x for x in tr if "2023-01-01" <= x["t"] < "2025-07-01"],
         "mi2025-2026": [x for x in tr if x["t"] >= "2025-07-01"]}
    out = {k: M.stats(v) for k, v in P.items()}
    out["all"] = M.stats(tr)
    yrs = {}
    for x in tr:
        yrs[x["t"][:4]] = round(yrs.get(x["t"][:4], 0) + x["R"], 1)
    out["years"] = dict(sorted(yrs.items()))
    return out


def main():
    from fund_bt import weekly
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", required=True)
    ap.add_argument("--macro", required=True)
    ap.add_argument("--out", default="results_macro.json")
    a = ap.parse_args()
    MC = Macro(a.macro)
    pairs = sorted({f.split("_")[0] for f in os.listdir(a.hist) if f.endswith("_1h.json.gz")})
    datas = {p: M.Data(a.hist, p) for p in pairs}
    for D in datas.values():
        D.h4t = [r["ts"] for r in D.h4]
    fx = [p for p in pairs if not p.startswith("XAU")]
    S = M.Strength(datas)

    def y2_level(ts):
        return {c: MC.y2_at(c, ts) for c in CCY}

    def y2_change(days):
        def f(ts):
            out = {}
            for c in CCY:
                a_, b_ = MC.y2_at(c, ts), MC.y2_at(c, ts - days * 86400)
                out[c] = None if a_ is None or b_ is None else a_ - b_
            return out
        return f

    def cot_level(kind, contrarian=False):
        def f(ts):
            d = {c: MC.cot_z(c, ts, kind) for c in CCY}
            return {c: (-v if contrarian else v) if v is not None else None for c, v in d.items()}
        return f

    def cot_flow(kind):
        return lambda ts: {c: MC.cot_change(c, ts, kind) for c in CCY}

    def risk(ts):
        """climat de marché : si le VIX monte (sur 5 jours) ou dépasse 25, on favorise les refuges (JPY, CHF, USD)"""
        v, _ = MC.vix.at(ts); v5, _ = MC.vix.at(ts - 7 * 86400)
        if v is None or v5 is None:
            return {}
        off = v > 25 or (v - v5) > 3
        if not off:
            return {}
        base = {"JPY": 1, "CHF": 0.8, "USD": 0.6, "EUR": 0, "GBP": -0.3, "CAD": -0.5, "NZD": -0.8, "AUD": -1}
        return base

    def combo(parts):
        def f(ts):
            tot = {c: 0.0 for c in CCY}; n = 0
            for fn in parts:
                r = ranks(fn(ts))
                if not r:
                    continue
                n += 1
                for c in CCY:
                    tot[c] += r.get(c, 0)
            return tot if n else {}
        return f

    mom60 = lambda ts: S.scores(ts, 60)

    def ranks_wrap(fn):
        return lambda ts: ranks(fn(ts))
    tests = [
        ("H1 anticipations de taux : variation des 2 ans sur 4 semaines", ranks_wrap(y2_change(28))),
        ("H1 anticipations de taux : variation des 2 ans sur 13 semaines", ranks_wrap(y2_change(91))),
        ("H2 niveau des taux à 2 ans (portage anticipé)", ranks_wrap(y2_level)),
        ("H3 COT fonds spéculatifs : suivre la position (z 52 sem.)", ranks_wrap(cot_level("levNet"))),
        ("H3 COT fonds spéculatifs : contre les extrêmes (z 52 sem.)", ranks_wrap(cot_level("levNet", True))),
        ("H3 COT gestionnaires d'actifs : suivre la position", ranks_wrap(cot_level("amNet"))),
        ("H3 COT fonds spéculatifs : suivre le flux sur 4 semaines", ranks_wrap(cot_flow("levNet"))),
        ("H4 climat de marché : refuges quand le VIX monte", risk),
        ("H5 combiné : anticipations 4 sem. + niveau 2 ans + momentum 60 j", combo([y2_change(28), y2_level, mom60])),
        ("H5 combiné : anticipations 13 sem. + niveau 2 ans + COT suivi", combo([y2_change(91), y2_level, cot_level("levNet")])),
        ("H5 combiné : anticipations 4 sem. + 13 sem. + niveau 2 ans", combo([y2_change(28), y2_change(91), y2_level])),
        ("H5 combiné complet : anticipations + niveau + COT + momentum", combo([y2_change(28), y2_change(91), y2_level, cot_level("levNet"), mom60])),
    ]
    res = []
    for name, fn in tests:
        for K in (2, 4):
            tr = weekly(datas, make_pick(fx, fn), K)
            pr = periods(tr)
            res.append({"name": f"{name} · {K} paires", "stats": pr, "trades": tr})
            p1, p2, p3 = pr["2020-2022"], pr["2023-mi2025"], pr["mi2025-2026"]
            print(f"{name[:70]:70} K={K} | 20-22 {p1.get('avgR',0):+.3f} ({p1.get('trades',0)}) | 23-25 {p2.get('avgR',0):+.3f} | 25-26 {p3.get('avgR',0):+.3f}"
                  f" | %/an {pr['all'].get('pctPerYear',0):+.1f} dd {pr['all'].get('maxDD',0)} | {pr['years']}", flush=True)
    json.dump(res, open(a.out, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main()
