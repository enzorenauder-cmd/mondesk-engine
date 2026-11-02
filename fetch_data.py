#!/usr/bin/env python3
"""Mon Desk : récupère les bougies Twelve Data des 29 paires et les écrit dans data/.
Lancé par GitHub Actions chaque heure (jours ouvrés). La clé vient du secret TWELVE_DATA_KEY.
- M15 (1000 bougies) à chaque passage
- H4 (150) au premier passage du jour puis à 8 h, 12 h et 16 h UTC
- D1 (120) au premier passage du jour
Les fichiers existants sont gardés quand une unité n'est pas rafraîchie.
"""
import json, os, sys, time
from datetime import datetime, timezone
import engine

PAIRS = ["EURGBP","EURAUD","EURNZD","EURUSD","EURCAD","EURCHF","EURJPY","GBPAUD","GBPNZD","GBPUSD",
         "GBPCAD","GBPCHF","GBPJPY","AUDNZD","AUDUSD","AUDCAD","AUDCHF","AUDJPY","NZDUSD","NZDCAD",
         "NZDCHF","NZDJPY","USDCAD","USDCHF","USDJPY","CADCHF","CADJPY","CHFJPY","XAUUSD"]
OUT = "data"

def main():
    key = os.environ.get("TWELVE_DATA_KEY")
    if not key:
        sys.exit("secret TWELVE_DATA_KEY manquant")
    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(timezone.utc)
    meta_p = os.path.join(OUT, "meta.json")
    meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
    today = now.strftime("%Y-%m-%d")
    first_today = meta.get("d1Date") != today
    h4_due = first_today or now.hour in (8, 12, 16)
    plan = [("15min", 1000)]
    if h4_due:
        plan.append(("4h", 150))
    if first_today:
        plan.append(("1day", 120))
    errors = []
    for interval, n in plan:
        for p in PAIRS:
            try:
                rows = engine.fetch(p, interval, n, key)
                if rows:
                    json.dump(rows, open(os.path.join(OUT, f"{p}_{interval}.json"), "w"), separators=(",", ":"))
            except Exception as e:
                errors.append(f"{p} {interval}: {e}")
    meta["fetchedAt"] = engine.iso(datetime.now(timezone.utc))
    meta["m15At"] = meta["fetchedAt"]
    if h4_due:
        meta["h4At"] = meta["fetchedAt"]
    if first_today:
        meta["d1Date"] = today
    meta["errors"] = errors[-20:]
    meta["pairs"] = PAIRS
    json.dump(meta, open(meta_p, "w"), indent=1)
    print(json.dumps(meta, indent=1))

if __name__ == "__main__":
    main()
