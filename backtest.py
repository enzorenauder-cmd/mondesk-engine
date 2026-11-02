#!/usr/bin/env python3
"""Mon Desk : backtest de la méthode technique (engine.py) sur l'historique.

Rejoue le moteur à chaque passage du Radar (lun-ven, 8 h 15 à 20 h 15 heure de Paris), pour chaque variante de méthode, sur les 29 paires,
avec seulement les bougies déjà CLÔTURÉES à ce moment-là (aucune information du futur), puis suit chaque
signal bougie par bougie (entrée touchée ou non, TP, SL, expiration) avec la même fonction que le Radar.

Limites : pas de fondamental, pas de filtre d'annonces, spread fixe estimé, prix Twelve Data (pas ceux du courtier).

Usage : python3 backtest.py --hist hist --out results [--pairs EURUSD,GBPUSD] [--from 2023-01-15]
"""
import argparse, bisect, gzip, json, os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import engine

UTC = timezone.utc
PARIS = ZoneInfo("Europe/Paris")
VALID_H = 6          # validité d'un signal (validityH du Radar)
VALIDITIES = (6, 12, 24)  # durées de validité comparées
MAX_HOLD_D = 10      # au-delà, un trade encore ouvert est clôturé au prix du moment
# variantes comparées (rrMin 1 : on garde tout, les seuils se testent après coup)
VARIANTS = {
    "base": {"rrMin": 1.0},                                    # méthode actuelle
    "tp2": {"rrMin": 1.0, "tpMode": "second"},                 # TP sur la deuxième cible
    "tprr": {"rrMin": 1.5, "tpMode": "rrmin"},                 # TP sur la première cible qui donne RR >= 1,5
    "pullback": {"rrMin": 1.0, "allowH4Pullback": True},       # D1 en tendance + H4 en correction autorisé
    "sltight": {"rrMin": 1.0, "slBufferATR": 0.03},            # stop plus serré (marge 3 % de l'ATR D1 au lieu de 10 %)
    "sltight_tprr": {"rrMin": 1.5, "slBufferATR": 0.03, "tpMode": "rrmin"},  # stop serré + TP sur la première cible à RR >= 1,5
}
CFG = VARIANTS["base"]


def load(hist, pair, interval):
    p = os.path.join(hist, f"{pair}_{interval}.json")
    if os.path.exists(p + ".gz"):
        rows = json.load(gzip.open(p + ".gz"))
    elif os.path.exists(p):
        rows = json.load(open(p))
    else:
        return [], []
    rows.sort(key=lambda r: r["t"])
    ts = [int(engine.parse_t(r["t"]).timestamp()) for r in rows]
    return rows, ts


def slots(start, end):
    d = start.astimezone(PARIS).date()
    while d <= end.astimezone(PARIS).date():
        if d.weekday() < 5:
            for h in range(8, 21):
                t = datetime(d.year, d.month, d.day, h, 15, tzinfo=PARIS).astimezone(UTC)
                if start <= t <= end:
                    yield t
        d += timedelta(days=1)


def session_of(t):
    h = t.astimezone(PARIS).hour
    return "Londres (8-12 h)" if h < 13 else ("New York (13-16 h)" if h < 17 else ("Fin de journée (17-18 h)" if h < 19 else "Soir (19-20 h)"))


def run_pair(pair, hist, start_from=None):
    d1, d1t = load(hist, pair, "1day")
    h4, h4t = load(hist, pair, "4h")
    m15, m15t = load(hist, pair, "15min")
    if len(m15) < 300 or len(d1) < 60 or len(h4) < 100:
        return [], {"pair": pair, "error": "historique insuffisant"}
    start = engine.parse_t(m15[250]["t"])
    if start_from:
        start = max(start, start_from)
    end = engine.parse_t(m15[-1]["t"])
    out, active_until = [], {}
    analyses = 0
    for T in slots(start, end):
        ts = int(T.timestamp())
        # bougies clôturées à T : M15 ouverte au plus tard à T-15 min, H4 à T-4 h, D1 la veille (UTC)
        i15 = bisect.bisect_right(m15t, ts - 900)
        i4 = bisect.bisect_right(h4t, ts - 4 * 3600)
        day0 = int(datetime(T.year, T.month, T.day, tzinfo=UTC).timestamp())
        i1 = bisect.bisect_left(d1t, day0)
        if i15 < 200 or i4 < 100 or i1 < 60:
            continue
        # pas de données récentes (week-end, trou) : on saute
        if ts - m15t[i15 - 1] > 3 * 3600:
            continue
        analyses += 1
        j_end = bisect.bisect_right(m15t, ts + MAX_HOLD_D * 86400)
        fut = m15[i15:j_end]
        if not fut:
            continue
        fut_end = engine.parse_t(fut[-1]["t"])
        sl_d1, sl_h4, sl_15 = d1[max(0, i1 - 120):i1], h4[max(0, i4 - 150):i4], m15[i15 - 200:i15]
        for vname, cfg in VARIANTS.items():
            r = engine.analyse_pair(pair, sl_d1, sl_h4, sl_15, cfg, T)
            for c in r.get("cands", []):
                key = (vname, pair, c["dir"])
                if active_until.get(key, 0) > ts:   # un signal déjà actif sur cette paire et ce sens
                    continue
                risk = abs(float(c["entry"]) - float(c["sl"]))
                cost = engine.spread(pair) / risk if risk else 0
                res = {}
                for vh in VALIDITIES:
                    item = {"pair": pair, "dir": c["dir"], "entry": c["entry"], "sl": c["sl"], "tp": c["tp"],
                            "invalidation": c["invalidation"], "createdAt": engine.iso(T),
                            "validUntil": engine.iso(T + timedelta(hours=vh))}
                    ev = engine.evaluate(item, fut, fut_end)
                    st = ev.get("status")
                    if st in ("tp", "sl"):
                        R, closed = ev["resultR"] - cost, ev["closedAt"]
                    elif st == "in_zone":   # encore ouvert au bout de 10 jours : clôture au prix du moment
                        st, R, closed = "timeout", (ev.get("currentR") or 0) - cost, fut[-1]["t"]
                    else:                    # jamais déclenché (expiré / invalidé) : 0 R, pas un trade
                        R, closed = 0.0, None
                    res[vh] = (st, round(R, 3), closed, bool(ev.get("missedRight")))
                st, R, closed, missed = res[VALID_H]
                traded = st in ("tp", "sl", "timeout")
                out.append({"variant": vname, "pair": pair, "dir": c["dir"], "setup": c["setup"][:2],
                            "order": "limite" if c["orderType"].endswith("Limit") else "marché",
                            "t": engine.iso(T), "hourParis": T.astimezone(PARIS).hour, "session": session_of(T),
                            "rr": c["rr"], "tech": c["techScore"], "status": st, "traded": traded,
                            "R": R if traded else 0.0, "missedRight": missed, "closedAt": closed,
                            "byValidity": {str(vh): {"status": v[0], "traded": v[0] in ("tp", "sl", "timeout"),
                                                     "R": v[1] if v[0] in ("tp", "sl", "timeout") else 0.0}
                                           for vh, v in res.items()}})
                if traded and closed:
                    active_until[key] = int(engine.parse_t(closed).timestamp())
                else:
                    active_until[key] = ts + VALID_H * 3600
    return out, {"pair": pair, "analyses": analyses, "signals": len(out), "from": engine.iso(start), "to": engine.iso(end)}


def _job(args):
    return run_pair(*args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default="hist")
    ap.add_argument("--out", default="results")
    ap.add_argument("--pairs")
    ap.add_argument("--from", dest="frm")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    pairs = a.pairs.split(",") if a.pairs else [f.split("_")[0] for f in os.listdir(a.hist) if f.endswith("_15min.json.gz") or f.endswith("_15min.json")]
    frm = datetime.strptime(a.frm, "%Y-%m-%d").replace(tzinfo=UTC) if a.frm else None
    allrows, meta = [], []
    from multiprocessing import Pool
    with Pool(os.cpu_count() or 2) as pool:
        for rows, m in pool.imap_unordered(_job, [(p, a.hist, frm) for p in sorted(set(pairs))]):
            allrows += rows; meta.append(m)
            print(json.dumps(m, ensure_ascii=False), flush=True)
    json.dump(allrows, open(os.path.join(a.out, "trades.json"), "w"), separators=(",", ":"))
    json.dump(meta, open(os.path.join(a.out, "meta.json"), "w"), indent=1, ensure_ascii=False)
    print(f"{len(allrows)} signaux, {sum(r['traded'] for r in allrows)} trades déclenchés")


if __name__ == "__main__":
    main()
