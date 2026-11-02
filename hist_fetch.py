#!/usr/bin/env python3
"""Mon Desk : téléchargement de l'historique Twelve Data pour le backtest (reprise automatique).

Écrit hist/<PAIRE>_<15min|4h|1day>.json.gz et hist/progress.json.
Ne dépasse jamais --max-daily crédits utilisés sur la journée UTC (Radar compris),
pour laisser au Radar les crédits dont il a besoin.
"""
import argparse, gzip, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

import engine

UTC = timezone.utc
PAIRS = ["EURGBP","EURAUD","EURNZD","EURUSD","EURCAD","EURCHF","EURJPY","GBPAUD","GBPNZD","GBPUSD",
         "GBPCAD","GBPCHF","GBPJPY","AUDNZD","AUDUSD","AUDCAD","AUDCHF","AUDJPY","NZDUSD","NZDCAD",
         "NZDCHF","NZDJPY","USDCAD","USDCHF","USDJPY","CADCHF","CADJPY","CHFJPY","XAUUSD"]
# unité -> (début, taille de fenêtre en jours) ; échauffement avant 2023 pour D1 et H4
PLAN = {"1day": ("2022-06-01", 1600), "4h": ("2022-11-01", 700), "15min": ("2023-01-01", 72)}
OUT = "hist"
_last = [0.0]


def daily_usage(key):
    try:
        with urllib.request.urlopen("https://api.twelvedata.com/api_usage?apikey=" + key, timeout=30) as r:
            js = json.loads(r.read().decode())
        return int(js.get("daily_usage", 0)), int(js.get("plan_daily_limit", 800))
    except Exception as e:
        print("api_usage indisponible :", e, file=sys.stderr)
        return None, 800


def get(pair, interval, start, end, key):
    wait = 7.8 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    sym = pair[:3] + "/" + pair[3:]
    q = {"symbol": sym, "interval": interval, "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
         "end_date": end.strftime("%Y-%m-%d %H:%M:%S"), "outputsize": 5000, "timezone": "UTC",
         "order": "ASC", "apikey": key, "format": "JSON"}
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        _last[0] = time.time()
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                js = json.loads(r.read().decode())
        except Exception as e:
            js = {"status": "error", "message": str(e)}
        msg = str(js.get("message", "")).lower()
        if js.get("status") == "error" and "minute" in msg:
            time.sleep(62); continue
        break
    if js.get("status") == "error":
        if "no data" in str(js.get("message", "")).lower():
            return []
        raise RuntimeError(js.get("message"))
    return [{"t": engine.iso(engine.parse_t(r["datetime"])), "o": float(r["open"]), "h": float(r["high"]),
             "l": float(r["low"]), "c": float(r["close"])} for r in js.get("values", [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-daily", type=int, default=790, help="plafond de crédits utilisés sur la journée UTC")
    a = ap.parse_args()
    key = os.environ.get("TWELVE_DATA_KEY") or sys.exit("secret TWELVE_DATA_KEY manquant")
    os.makedirs(OUT, exist_ok=True)
    prog_p = os.path.join(OUT, "progress.json")
    prog = json.load(open(prog_p)) if os.path.exists(prog_p) else {}
    used0, _ = daily_usage(key)
    if used0 is None:
        sys.exit("impossible de lire la consommation du jour : arrêt par prudence")
    budget = a.max_daily - used0
    print(f"crédits utilisés aujourd'hui : {used0}, budget de ce passage : {max(budget, 0)}")
    end_all = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    spent = 0
    for interval, (start_s, win) in PLAN.items():
        for p in PAIRS:
            k = f"{p}_{interval}"
            done_until = prog.get(k)
            cur = engine.parse_t(done_until) if done_until else datetime.strptime(start_s, "%Y-%m-%d").replace(tzinfo=UTC)
            if (end_all - cur) < timedelta(days=3):
                continue
            path = os.path.join(OUT, k + ".json.gz")
            rows = json.load(gzip.open(path)) if os.path.exists(path) else []
            while cur < end_all:
                if spent >= budget:
                    break
                nxt = min(cur + timedelta(days=win), end_all)
                try:
                    part = get(p, interval, cur, nxt, key)
                except Exception as e:
                    print(k, cur, "erreur :", e, file=sys.stderr)
                    if "credits" in str(e).lower():
                        budget = spent  # plus de crédits : on s'arrête proprement
                    break
                spent += 1
                seen = {r["t"] for r in rows[-6000:]}
                rows += [r for r in part if r["t"] not in seen]
                cur = nxt
                prog[k] = engine.iso(cur)
            rows.sort(key=lambda r: r["t"])
            with gzip.open(path, "wt") as f:
                json.dump(rows, f, separators=(",", ":"))
            json.dump(prog, open(prog_p, "w"), indent=0)
            if spent >= budget:
                break
        if spent >= budget:
            break
    total = sum(1 for p in PAIRS for i in PLAN if (end_all - engine.parse_t(prog.get(f"{p}_{i}", "2000-01-01"))) < timedelta(days=3))
    prog["_complete"] = total == len(PAIRS) * len(PLAN)
    prog["_updatedAt"] = engine.iso(datetime.now(UTC))
    json.dump(prog, open(prog_p, "w"), indent=0)
    print(f"requêtes de ce passage : {spent} ; séries complètes : {total}/{len(PAIRS) * len(PLAN)}")


if __name__ == "__main__":
    main()
