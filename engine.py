#!/usr/bin/env python3
"""Mon Desk - moteur technique du Radar (bibliothèque standard uniquement).

Calcule, toujours de la même façon, la partie technique de la méthode :
ATR, swings (2 bougies de chaque côté), structure D1/H4/M15 (BOS validé par
une CLÔTURE), FVG, order blocks, liquidité (veille, semaine, Asie, equal
highs/lows), setups M15 S1-S4, entrée/SL/TP/RR après coûts, score technique
/100, lots par compte, paires à surveiller avec scénario graphique.

Usage :
  python3 engine.py scan  --key KEY --out DIR [--inputs inputs.json]
  python3 engine.py track --key KEY --out DIR --items items.json
  (--data-dir DIR remplace l'API par des fichiers <PAIRE>_<interval>.json)

inputs.json (facultatif) : {"settings": {...desk/settings...},
  "accounts": [...], "calendar": [{time, currency, impact}],
  "strength": {"EUR": -10, ...}, "force": ["AUDNZD"], "top": 8,
  "only": ["AUDNZD"]  (suivi : analyse seulement ces paires)}
items.json : [{"id", "kind": "signal"|"test"|"trade", "pair", "dir", "entry",
  "sl", "slInit", "tp", "invalidation", "createdAt"|"openAt", "validUntil",
  "status", "triggered", "beMoved"}]
"""
import json, math, os, sys, time, argparse, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
PARIS = ZoneInfo("Europe/Paris")

CURS = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
PAIRS28 = [CURS[i] + CURS[j] for i in range(8) for j in range(i + 1, 8)]
ALL_PAIRS = PAIRS28 + ["XAUUSD"]
MAJORS = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"}
UTC = timezone.utc
_last_call = [0.0]


# ---------------------------------------------------------------- utilitaires
def pip(pair):
    return 0.1 if pair.startswith("XAU") else (0.01 if pair.endswith("JPY") else 0.0001)


def digits(pair):
    return 2 if pair.startswith("XAU") else (3 if pair.endswith("JPY") else 5)


def rnd(pair, v):
    return None if v is None else round(v, digits(pair))


def spread(pair):
    if pair.startswith("XAU"):
        return 0.30
    return (1.0 if pair in MAJORS else 2.2) * pip(pair)


def lot_size(pair):
    return 100 if pair.startswith("XAU") else 100000


def parse_t(s):
    s = s.replace("Z", "").replace("T", " ")
    fmt = "%Y-%m-%d %H:%M:%S" if len(s) > 10 else "%Y-%m-%d"
    return datetime.strptime(s[:19], fmt).replace(tzinfo=UTC)


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- données
def fetch(pair, interval, n=200, key=None, data_dir=None, start=None):
    """Bougies ASC [{t, o, h, l, c}] en UTC."""
    if data_dir:
        p = os.path.join(data_dir, f"{pair}_{interval}.json")
        if not os.path.exists(p):
            return []
        rows = json.load(open(p))
    else:
        wait = 7.8 - (time.time() - _last_call[0])  # 8 requêtes / minute max
        if wait > 0:
            time.sleep(wait)
        sym = pair[:3] + "/" + pair[3:]
        q = {"symbol": sym, "interval": interval, "outputsize": n, "timezone": "UTC",
             "order": "ASC", "apikey": key, "format": "JSON"}
        if start:
            q["start_date"] = start.strftime("%Y-%m-%d %H:%M:%S")
        url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(q)
        for attempt in range(3):
            _last_call[0] = time.time()
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    js = json.loads(r.read().decode())
            except Exception as e:  # réseau
                js = {"status": "error", "message": str(e)}
            if js.get("status") == "error" and "credits" in str(js.get("message", "")).lower():
                time.sleep(62)
                continue
            break
        if js.get("status") == "error":
            raise RuntimeError(f"{pair} {interval}: {js.get('message')}")
        rows = js.get("values", [])
    out = []
    for r in rows:
        t = r.get("t") or r.get("datetime")
        out.append({"t": iso(parse_t(t)), "o": float(r.get("o", r.get("open"))),
                    "h": float(r.get("h", r.get("high"))), "l": float(r.get("l", r.get("low"))),
                    "c": float(r.get("c", r.get("close")))})
    out.sort(key=lambda x: x["t"])
    return out


# ---------------------------------------------------------------- briques techniques
def atr(c, n=14):
    if len(c) < 2:
        return None
    trs = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
           for i in range(1, len(c))]
    if len(trs) < n:
        return sum(trs) / len(trs)
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def swings(c, k=2):
    """Swings confirmés : k bougies de chaque côté."""
    hi, lo = [], []
    for i in range(k, len(c) - k):
        h, l = c[i]["h"], c[i]["l"]
        if all(h > c[i - j]["h"] for j in range(1, k + 1)) and all(h >= c[i + j]["h"] for j in range(1, k + 1)):
            hi.append((i, h))
        if all(l < c[i - j]["l"] for j in range(1, k + 1)) and all(l <= c[i + j]["l"] for j in range(1, k + 1)):
            lo.append((i, l))
    return hi, lo


def structure(c, k=2):
    """BOS validés par une clôture. Renvoie état et liste d'événements."""
    hi, lo = swings(c, k)
    events, last_h, last_l, hi_p, lo_p = [], None, None, 0, 0
    used_h, used_l = set(), set()
    for i in range(len(c)):
        while hi_p < len(hi) and hi[hi_p][0] + k <= i - 1:
            last_h = hi[hi_p]; hi_p += 1
        while lo_p < len(lo) and lo[lo_p][0] + k <= i - 1:
            last_l = lo[lo_p]; lo_p += 1
        if last_h and last_h[0] not in used_h and c[i]["c"] > last_h[1]:
            events.append({"i": i, "dir": "up", "level": last_h[1], "swing_i": last_h[0]}); used_h.add(last_h[0])
        if last_l and last_l[0] not in used_l and c[i]["c"] < last_l[1]:
            events.append({"i": i, "dir": "down", "level": last_l[1], "swing_i": last_l[0]}); used_l.add(last_l[0])
    state = "indéterminé"
    if events:
        # la structure suit le dernier BOS / CHoCH ; sans cassure récente = range
        last = events[-1]
        if last["i"] < len(c) - 60:
            state = "range"
        else:
            state = "haussier" if last["dir"] == "up" else "baissier"
    return {"state": state, "events": events, "swings_hi": hi, "swings_lo": lo}


def fvgs(c, a, min_frac=0.1):
    """FVG non comblés : [{i, dir, low, high}]."""
    out = []
    for i in range(2, len(c)):
        if c[i]["l"] > c[i - 2]["h"] and c[i]["l"] - c[i - 2]["h"] >= min_frac * a:
            out.append({"i": i - 1, "dir": "up", "low": c[i - 2]["h"], "high": c[i]["l"]})
        if c[i]["h"] < c[i - 2]["l"] and c[i - 2]["l"] - c[i]["h"] >= min_frac * a:
            out.append({"i": i - 1, "dir": "down", "low": c[i]["h"], "high": c[i - 2]["l"]})
    res = []
    for g in out:
        after = c[g["i"] + 2:]
        filled = any(x["l"] <= g["low"] for x in after) if g["dir"] == "up" else any(x["h"] >= g["high"] for x in after)
        if not filled:
            g["touches"] = sum(1 for x in after if (x["l"] <= g["high"] if g["dir"] == "up" else x["h"] >= g["low"]))
            res.append(g)
    return res


def order_blocks(c, st, a):
    """Dernière bougie opposée avant un déplacement >= 1,5 ATR qui a fait un BOS."""
    obs = []
    for e in st["events"]:
        j = e["i"]
        for b in range(j - 1, max(j - 12, 0) - 1, -1):
            x = c[b]
            if e["dir"] == "up" and x["c"] < x["o"]:
                disp = max(y["h"] for y in c[b:j + 1]) - x["l"]
                if disp >= 1.5 * a:
                    obs.append({"i": b, "dir": "up", "low": x["l"], "high": x["h"], "disp": disp / a})
                break
            if e["dir"] == "down" and x["c"] > x["o"]:
                disp = x["h"] - min(y["l"] for y in c[b:j + 1])
                if disp >= 1.5 * a:
                    obs.append({"i": b, "dir": "down", "low": x["l"], "high": x["h"], "disp": disp / a})
                break
    res = []
    for ob in obs:
        after = c[ob["i"] + 1:]
        broken = any(y["c"] < ob["low"] for y in after) if ob["dir"] == "up" else any(y["c"] > ob["high"] for y in after)
        if not broken:
            ob["touches"] = sum(1 for y in after[3:] if (y["l"] <= ob["high"] if ob["dir"] == "up" else y["h"] >= ob["low"]))
            res.append(ob)
    return res


def liquidity(d1, m15, a15):
    lv = []
    now = parse_t(m15[-1]["t"]) if m15 else datetime.now(UTC)
    if len(d1) >= 2:
        pd_ = d1[-2] if parse_t(d1[-1]["t"]).date() == now.date() else d1[-1]
        lv += [("veille haut", pd_["h"], "high"), ("veille bas", pd_["l"], "low")]
        # semaine précédente
        wk = now.isocalendar()[1]
        prev = [x for x in d1 if parse_t(x["t"]).isocalendar()[1] == (wk - 1 if wk > 1 else 52)]
        if prev:
            lv += [("semaine haut", max(x["h"] for x in prev), "high"), ("semaine bas", min(x["l"] for x in prev), "low")]
    # session asiatique du jour (23 h - 7 h UTC)
    asia = [x for x in m15 if parse_t(x["t"]) >= now.replace(hour=0, minute=0, second=0) - timedelta(hours=1)
            and parse_t(x["t"]).hour < 7]
    if asia:
        lv += [("Asie haut", max(x["h"] for x in asia), "high"), ("Asie bas", min(x["l"] for x in asia), "low")]
    hi, lo = swings(m15[-96:]) if len(m15) >= 10 else ([], [])
    for arr, side, name in ((hi, "high", "equal highs"), (lo, "low", "equal lows")):
        for p in range(len(arr)):
            for q in range(p + 1, len(arr)):
                if abs(arr[p][1] - arr[q][1]) <= 0.1 * a15:
                    lv.append((name, (arr[p][1] + arr[q][1]) / 2, side))
    return lv


# ---------------------------------------------------------------- analyse d'une paire
def analyse_pair(pair, d1, h4, m15, cfg, now):
    rr_min = float(cfg.get("rrMin", 2))
    a_d1, a_h4, a_15 = atr(d1), atr(h4), atr(m15)
    if not (a_d1 and a_h4 and a_15) or len(m15) < 40:
        return {"pair": pair, "error": "données insuffisantes"}
    s_d1, s_h4, s_15 = structure(d1), structure(h4), structure(m15)
    price = m15[-1]["c"]
    bias = None
    if s_d1["state"] == "haussier" and s_h4["state"] == "haussier":
        bias = "buy"
    elif s_d1["state"] == "baissier" and s_h4["state"] == "baissier":
        bias = "sell"
    h4_zones = [dict(z, kind="OB H4") for z in order_blocks(h4, s_h4, a_h4)] + \
               [dict(z, kind="FVG H4") for z in fvgs(h4, a_h4, 0.15)]
    h4_zones = [z for z in h4_zones if z["i"] >= len(h4) - 120]
    lv = liquidity(d1, m15, a_15)
    m15_fvg = fvgs(m15, a_15, 0.1)
    m15_ob = order_blocks(m15, s_15, a_15)
    info = {"pair": pair, "price": rnd(pair, price), "atrD1": rnd(pair, a_d1), "atrM15": rnd(pair, a_15),
            "d1": s_d1["state"], "h4": s_h4["state"], "m15": s_15["state"], "bias": bias,
            "levels": [{"name": n, "price": rnd(pair, p)} for n, p, _ in lv][:12]}
    cands, rejected, watch = [], [], []
    if not bias:
        rejected.append({"pair": pair, "reason": f"D1 {s_d1['state']} / H4 {s_h4['state']} : pas d'alignement"})
        return {"info": info, "cands": cands, "rejected": rejected, "watch": watch}
    up = bias == "buy"
    sgn = 1 if up else -1
    zones = [z for z in h4_zones if z["dir"] == ("up" if up else "down")]
    zones.sort(key=lambda z: abs(((z["low"] + z["high"]) / 2) - price))
    last16 = m15[-16:]
    t_now = parse_t(m15[-1]["t"])

    def tp_for(entry, sl):
        targets = sorted({p for _, p, side in lv if (side == "high" if up else side == "low") and (p - entry) * sgn > 0.3 * abs(entry - sl)}
                         | {p for _, p in (s_h4["swings_hi"] if up else s_h4["swings_lo"])[-6:] if (p - entry) * sgn > 0.3 * abs(entry - sl)},
                         key=lambda p: (p - entry) * sgn)
        return targets[0] if targets else None

    def build(setup, entry, sl_point, zone, conf_i, conf_score, note):
        sl = sl_point - sgn * 0.1 * a_d1
        risk = abs(entry - sl)
        if risk <= 0:
            return
        if risk > 0.6 * a_d1:
            rejected.append({"pair": pair, "setup": setup, "reason": f"stop trop large ({risk / a_d1:.0%} de l'ATR D1)"}); return
        sp = spread(pair)
        if sp > 0.25 * risk:
            rejected.append({"pair": pair, "setup": setup, "reason": "spread > 25 % du stop"}); return
        tp = tp_for(entry, sl)
        if tp is None:
            rejected.append({"pair": pair, "setup": setup, "reason": "pas de cible logique"}); return
        rr = (abs(tp - entry) - sp) / (risk + sp)
        # placement : limite ou marché
        path_done = (price - entry) * sgn / abs(tp - entry)
        conf_age_h = (t_now - parse_t(m15[conf_i]["t"])).total_seconds() / 3600
        if (price - entry) * sgn <= 0:
            order = "Buy Limit" if up else "Sell Limit"
            if (price - sl) * sgn <= 0:
                rejected.append({"pair": pair, "setup": setup, "reason": "prix déjà sous l'invalidation"}); return
        elif path_done < 0.4 and conf_age_h < 2:
            order = "Achat au marché" if up else "Vente au marché"
            entry = price
            risk = abs(entry - sl); rr = (abs(tp - entry) - sp) / (risk + sp)
        else:
            rejected.append({"pair": pair, "setup": setup, "reason": f"entrée ratée ({path_done:.0%} du chemin déjà fait)"}); return
        align = 25 if s_d1["state"] == s_h4["state"] else 12
        zscore = 20 if zone and zone.get("touches", 0) <= 1 else (12 if zone else 8)
        conf = conf_score - (5 if conf_age_h > 2 else 0)
        space = max(0, min(15, rr / 3 * 15))
        execu = 15 if sp < 0.1 * risk else 8
        tech = round(align + zscore + conf + space + execu)
        z = zone or {"low": min(entry, sl_point), "high": max(entry, sl_point)}
        cands.append({
            "pair": pair, "dir": bias, "setup": setup, "orderType": order,
            "entry": rnd(pair, entry), "sl": rnd(pair, sl), "tp": rnd(pair, tp),
            "invalidation": rnd(pair, sl_point), "zone": {"low": rnd(pair, z["low"]), "high": rnd(pair, z["high"])},
            "rr": round(rr, 2), "techScore": tech, "confirmedAt": m15[conf_i]["t"], "note": note,
            "technical": {"setup": setup, "structure": f"D1 {s_d1['state']}, H4 {s_h4['state']}, M15 {s_15['state']}",
                          "zone": f"{z.get('kind', 'zone')} {rnd(pair, z['low'])}-{rnd(pair, z['high'])}",
                          "confirmation": note, "targets": f"TP {rnd(pair, tp)} (première cible logique)"},
            "trigger": {"price": rnd(pair, entry), "text": "confirmation M15 faite"},
            "path": [{"price": rnd(pair, entry), "label": "Retour sur l'entrée" if order.endswith("Limit") else "Entrée au marché"},
                     {"price": rnd(pair, tp), "label": "Objectif"}]})

    # --- S1 rejet de zone : touche de zone H4 puis clôture au-delà du swing interne
    for z in zones[:3]:
        touch = [i for i in range(len(m15) - 16, len(m15)) if (m15[i]["l"] <= z["high"] if up else m15[i]["h"] >= z["low"]) and
                 (m15[i]["h"] >= z["low"] if up else m15[i]["l"] <= z["high"])]
        if not touch:
            continue
        t0 = touch[0]
        before = m15[max(0, t0 - 12):t0 + 1]
        internal = max(x["h"] for x in before[-6:]) if up else min(x["l"] for x in before[-6:])
        conf = [i for i in range(t0 + 1, len(m15)) if (m15[i]["c"] > internal if up else m15[i]["c"] < internal)]
        if conf:
            ext = min(x["l"] for x in m15[t0:conf[0] + 1]) if up else max(x["h"] for x in m15[t0:conf[0] + 1])
            zone_half = (z["high"] - 0.25 * (z["high"] - z["low"])) if up else (z["low"] + 0.25 * (z["high"] - z["low"]))
            build("S1 · rejet de zone", zone_half, min(ext, z["low"]) if up else max(ext, z["high"]), z, conf[0], 20,
                  f"Rejet de la zone puis clôture M15 {'au-dessus de' if up else 'sous'} {rnd(pair, internal)}")
            break
    # --- S2 BOS M15 dans le sens du biais, entrée sur l'OB / FVG de la cassure
    bos = [e for e in s_15["events"] if e["dir"] == ("up" if up else "down") and e["i"] >= len(m15) - 12]
    if bos:
        e = bos[-1]
        src = [o for o in m15_ob if o["dir"] == e["dir"] and e["i"] - 12 <= o["i"] <= e["i"]] or \
              [g for g in m15_fvg if g["dir"] == e["dir"] and e["i"] - 6 <= g["i"] <= e["i"] + 2]
        if src:
            o = src[-1]
            entry = (o["high"] - 0.25 * (o["high"] - o["low"])) if up else (o["low"] + 0.25 * (o["high"] - o["low"]))
            sw = [p for i, p in (s_15["swings_lo"] if up else s_15["swings_hi"]) if i < e["i"]]
            slp = min(o["low"], sw[-1]) if (up and sw) else (max(o["high"], sw[-1]) if sw else (o["low"] if up else o["high"]))
            zone = next((z for z in zones if z["low"] - a_15 <= slp <= z["high"] + a_15), None)
            build("S2 · BOS M15 + retour", entry, slp, zone, e["i"], 22, f"BOS M15 par clôture {'au-dessus de' if up else 'sous'} {rnd(pair, e['level'])}")
    # --- S3 prise de liquidité puis déplacement + BOS
    for i in range(len(m15) - 16, len(m15) - 1):
        x = m15[i]
        for name, p, side in lv:
            swept = (side == "low" and x["l"] < p < x["c"]) if up else (side == "high" and x["h"] > p > x["c"])
            if not swept:
                continue
            seg = m15[i:i + 5]
            disp = (max(y["h"] for y in seg) - x["l"]) if up else (x["h"] - min(y["l"] for y in seg))
            b = [e for e in s_15["events"] if e["dir"] == ("up" if up else "down") and i < e["i"] <= i + 8]
            if disp >= 1.5 * a_15 and b:
                g = [g for g in m15_fvg if g["dir"] == ("up" if up else "down") and i <= g["i"] <= b[0]["i"] + 1]
                entry = ((g[0]["low"] + g[0]["high"]) / 2) if g else ((x["l"] + seg[-1]["c"]) / 2 if up else (x["h"] + seg[-1]["c"]) / 2)
                build("S3 · prise de liquidité", entry, x["l"] if up else x["h"], None, b[0]["i"], 25,
                      f"Prise de liquidité ({name} {rnd(pair, p)}) puis déplacement et BOS M15")
                break
        else:
            continue
        break
    # --- S4 retour en FVG M15 après une cassure
    if bos:
        g = [g for g in m15_fvg if g["dir"] == ("up" if up else "down") and g["i"] >= bos[-1]["i"] - 2]
        if g:
            g = g[-1]
            slp = m15[g["i"] - 1]["l"] if up else m15[g["i"] - 1]["h"]
            build("S4 · retour en FVG", (g["low"] + g["high"]) / 2, slp, None, bos[-1]["i"], 16, f"FVG M15 {rnd(pair, g['low'])}-{rnd(pair, g['high'])} non comblé")
    for c_ in cands:
        if c_["rr"] < rr_min:
            c_["belowRR"] = True
    # --- À surveiller : zone H4 proche sans confirmation
    if not [c_ for c_ in cands if not c_.get("belowRR")]:
        for z in zones[:2]:
            dist = (price - z["high"]) if up else (z["low"] - price)
            if -0.2 * a_d1 <= dist <= 0.5 * a_d1:
                internal = max(x["h"] for x in last16[-8:]) if up else min(x["l"] for x in last16[-8:])
                entry = (z["high"] - 0.25 * (z["high"] - z["low"])) if up else (z["low"] + 0.25 * (z["high"] - z["low"]))
                sl = (z["low"] if up else z["high"]) - sgn * 0.1 * a_d1
                tp = tp_for(entry, sl)
                if tp is None or (abs(tp - entry) - spread(pair)) / (abs(entry - sl) + spread(pair)) < rr_min:
                    continue
                trig = internal
                watch.append({
                    "pair": pair, "dir": bias, "setup": "S1 · rejet de zone",
                    "condition": f"Retour dans {rnd(pair, z['low'])}-{rnd(pair, z['high'])} ({z['kind']}) puis clôture M15 {'au-dessus de' if up else 'sous'} {rnd(pair, trig)}",
                    "zone": {"low": rnd(pair, z["low"]), "high": rnd(pair, z["high"])},
                    "trigger": {"price": rnd(pair, trig), "text": f"clôture M15 {'au-dessus' if up else 'en dessous'}"},
                    "entry": rnd(pair, entry), "sl": rnd(pair, sl), "tp": rnd(pair, tp), "invalidation": rnd(pair, z["low"] if up else z["high"]),
                    "path": [{"price": rnd(pair, (z["low"] + z["high"]) / 2), "label": "Le prix revient dans la zone"},
                             {"price": rnd(pair, trig), "label": f"Bougie M15 clôture {'au-dessus de' if up else 'sous'} {rnd(pair, trig)}"},
                             {"price": rnd(pair, entry), "label": f"Entrée au retour ({'Buy' if up else 'Sell'} Limit)"}] +
                            ([{"price": rnd(pair, tp), "label": "Objectif"}] if tp else []),
                    "candles": m15[-64:]})
                break
    return {"info": info, "cands": cands, "rejected": rejected, "watch": watch}


# ---------------------------------------------------------------- lots, annonces
def fx_table(closes):
    fx = {"EUR": 1.0}
    for cur in CURS:
        if cur == "EUR":
            continue
        p = closes.get("EUR" + cur)
        if p:
            fx[cur] = 1 / p
    return fx


def lots_for(pair, entry, sl, accounts, fx, risk_default=1.0):
    out = []
    quote = "USD" if pair.startswith("XAU") else pair[3:]
    for a in accounts:
        if a.get("status") in ("perdu", "clôturé"):
            continue
        cur = a.get("currency") or "EUR"
        v = (fx.get(quote, 0) / fx.get(cur, 1)) if fx.get(cur) else 0
        bal = float(a.get("balance") or a.get("capital") or 0)
        risk = float(a.get("riskMax") or risk_default)
        if a.get("consecutiveSL", 0) >= 3:
            risk *= float(a.get("riskCut", 0.6))
        dist = abs(entry - sl)
        if not (v and dist and bal):
            continue
        lots = math.floor(bal * risk / 100 / (dist * lot_size(pair) * v) * 100) / 100
        out.append({"accountId": a.get("id"), "account": a.get("name"), "max": lots, "half": math.floor(lots * 50) / 100})
    return out


def news_flags(pair, cand_time, valid_until, calendar):
    curs = {"USD" if pair.startswith("XAU") else pair[:3], pair[3:]}
    blocked, during = None, []
    for e in calendar or []:
        if (e.get("impact") or "").lower()[:1] != "h" or e.get("currency") not in curs or not e.get("time"):
            continue
        t = parse_t(e["time"])
        if abs((t - cand_time).total_seconds()) <= 15 * 60:
            blocked = e.get("title") or e.get("currency")
        if cand_time < t <= valid_until:
            during.append(e)
    return blocked, during


def mt5_text(c):
    p = c["pair"]
    lines = [f"{p} · {'ACHAT' if c['dir'] == 'buy' else 'VENTE'} · {c['orderType']}",
             f"Entrée {c['entry']:.{digits(p)}f}", f"SL {c['sl']:.{digits(p)}f}", f"TP {c['tp']:.{digits(p)}f}"]
    if c.get("lots"):
        lines.append("Lots " + " · ".join(f"{l['max']:.2f} ({l['account']})" for l in c["lots"]))
    else:
        lines.append("Lots : ajoute tes comptes dans Mon Desk")
    vu = parse_t(c["validUntil"]).astimezone(PARIS)
    lines.append(f"Jusqu'à {vu.hour} h {vu.minute:02d} · RR {c['rr']:.1f}")
    return "\n".join(lines)


# ---------------------------------------------------------------- commandes
def cmd_scan(args):
    inp = json.load(open(args.inputs)) if args.inputs else {}
    cfg = inp.get("settings", {})
    pairs = [p for p in (cfg.get("pairs") or ALL_PAIRS) if len(p) == 6]
    if inp.get("only"):  # suivi : seulement ces paires (paires à surveiller, trades ouverts)
        pairs = [p for p in inp["only"] if len(p) == 6]
    strength = inp.get("strength", {})
    now = datetime.now(UTC)
    os.makedirs(args.out, exist_ok=True)
    d1s, closes, prelim = {}, {}, []
    for p in pairs:
        try:
            d1 = fetch(p, "1day", 120, args.key, args.data_dir)
        except Exception as e:
            print("D1", p, e, file=sys.stderr); continue
        if len(d1) < 30:
            continue
        d1s[p] = d1; closes[p] = d1[-1]["c"]
        st = structure(d1)["state"]
        b, q = ("USD", p[3:]) if p.startswith("XAU") else (p[:3], p[3:])
        fund = (strength.get(b, 0) - strength.get(q, 0)) if not p.startswith("XAU") else -strength.get("USD", 0)
        trend = {"haussier": 1, "baissier": -1}.get(st, 0)
        agree = (fund > 0 and trend > 0) or (fund < 0 and trend < 0)
        prelim.append((abs(fund) + (40 if agree else 0) + (10 if trend else 0), p))
    prelim.sort(reverse=True)
    top = int(inp.get("top", 8))
    chosen = [p for _, p in prelim[:top]] if not inp.get("only") else list(d1s)
    for f in inp.get("force", []):
        if f in d1s and f not in chosen:
            chosen.append(f)
    fx = fx_table(closes)
    vh = float(cfg.get("validityH", 6))
    res = {"generatedAt": iso(now), "fx": {k: round(v, 6) for k, v in fx.items() if k != "EUR"},
           "ranking": [{"pair": p, "score": round(s)} for s, p in prelim], "pairs": {}, "candidates": [], "watch": [], "rejected": []}
    for p in chosen:
        try:
            h4 = fetch(p, "4h", 150, args.key, args.data_dir)
            m15 = fetch(p, "15min", 200, args.key, args.data_dir)
        except Exception as e:
            print("H4/M15", p, e, file=sys.stderr); continue
        r = analyse_pair(p, d1s[p], h4, m15, cfg, now)
        if "info" not in r:
            continue
        res["pairs"][p] = r["info"]
        for c in r["cands"]:
            c["validUntil"] = iso(now + timedelta(hours=vh))
            blocked, during = news_flags(p, now, now + timedelta(hours=vh), inp.get("calendar"))
            if blocked:
                c["newsBlocked"] = blocked
            if during:
                c["alerts"] = [f"⚠ {e.get('title', e.get('currency'))} {parse_t(e['time']).astimezone(PARIS).strftime('%H h %M')} : sors avant" for e in during[:1]]
            c["lots"] = lots_for(p, c["entry"], c["sl"], inp.get("accounts", []), fx, float(cfg.get("riskPct", 1)))
            c["mt5"] = mt5_text(c)
            c["candles"] = m15[-64:]
            res["candidates"].append(c)
        res["watch"] += r["watch"]
        res["rejected"] += r["rejected"]
    res["candidates"].sort(key=lambda c: -c["techScore"])
    # fichiers prêts pour la base (un par candidat et par paire à surveiller)
    for c in res["candidates"]:
        json.dump(c, open(os.path.join(args.out, f"cand_{c['pair']}_{c['setup'][:2]}.json"), "w"), ensure_ascii=False)
    summary = dict(res)
    summary["candidates"] = [{k: v for k, v in c.items() if k not in ("candles", "mt5")} for c in res["candidates"]]
    summary["watch"] = [{k: v for k, v in w.items() if k != "candles"} for w in res["watch"]]
    json.dump(res, open(os.path.join(args.out, "scan_full.json"), "w"), ensure_ascii=False)
    json.dump(summary, open(os.path.join(args.out, "scan.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({"pairs_scanned": len(d1s), "analysed": list(res["pairs"]), "candidates": len(res["candidates"]),
                      "watch": len(res["watch"]), "summary": os.path.join(args.out, "scan.json")}, ensure_ascii=False))


def evaluate(item, m15, now):
    """Statut d'un signal, test ou trade à partir des bougies M15 depuis sa création."""
    up = item["dir"] == "buy"
    e, sl, tp = float(item["entry"]), float(item["sl"]), float(item["tp"])
    sl0 = float(item.get("slInit") or sl)
    inv = item.get("invalidation")
    t0 = parse_t(item.get("openAt") or item.get("createdAt"))
    vu = parse_t(item["validUntil"]) if item.get("validUntil") else None
    bars = [b for b in m15 if parse_t(b["t"]) >= t0 - timedelta(minutes=14)]
    out = {"lastPrice": rnd(item["pair"], m15[-1]["c"]) if m15 else None, "lastPriceAt": m15[-1]["t"] if m15 else None}
    triggered = bool(item.get("triggered"))
    entered_i = None
    for i, b in enumerate(bars):
        if not triggered:
            if vu and parse_t(b["t"]) > vu:
                out["status"] = "expired"
                post = bars[:i]
                out["missedRight"] = any((x["h"] >= tp) if up else (x["l"] <= tp) for x in post)
                return out
            if inv is not None and ((b["c"] < float(inv)) if up else (b["c"] > float(inv))) and not ((b["l"] <= e) if up else (b["h"] >= e)):
                out["status"] = "invalidated"; return out
            if (b["l"] <= e) if up else (b["h"] >= e):
                triggered = True; entered_i = i; out["triggered"] = True; out["triggeredAt"] = b["t"]; out["status"] = "in_zone"
            else:
                continue
        hit_sl = (b["l"] <= sl) if up else (b["h"] >= sl)
        hit_tp = (b["h"] >= tp) if up else (b["l"] <= tp)
        if hit_sl:  # prudence : si les deux dans la même bougie, on compte le SL
            out["status"] = "be" if item.get("beMoved") and abs(sl - e) < 3 * pip(item["pair"]) else "sl"
            out["exit"] = rnd(item["pair"], sl); out["closedAt"] = b["t"]
            out["resultR"] = round((sl - e) / (e - sl0) if up else (e - sl) / (sl0 - e), 2)
            return out
        if hit_tp:
            out["status"] = "tp"; out["exit"] = rnd(item["pair"], tp); out["closedAt"] = b["t"]
            out["resultR"] = round((tp - e) / (e - sl0) if up else (e - tp) / (sl0 - e), 2); return out
    if not triggered:
        out["status"] = "pending"
        if vu and now > vu:
            out["status"] = "expired"
            out["missedRight"] = any((x["h"] >= tp) if up else (x["l"] <= tp) for x in bars)
        return out
    out["status"] = "in_zone"
    px = m15[-1]["c"]
    out["currentR"] = round(((px - e) / (e - sl0)) if up else ((e - px) / (sl0 - e)), 2)
    # nouveau creux (achat) / sommet (vente) M15 formé depuis l'entrée : règle break-even
    seg = bars[entered_i or 0:]
    hi, lo = swings(seg)
    out["newSwing"] = bool(lo if up else hi)
    return out


def cmd_track(args):
    items = json.load(open(args.items))
    now = datetime.now(UTC)
    os.makedirs(args.out, exist_ok=True)
    by_pair = {}
    for it in items:
        by_pair.setdefault(it["pair"], []).append(it)
    results = []
    for p, its in by_pair.items():
        t0 = min(parse_t(i.get("openAt") or i.get("createdAt")) for i in its)
        try:
            m15 = fetch(p, "15min", 500, args.key, args.data_dir, start=t0 - timedelta(minutes=30))
        except Exception as e:
            print("M15", p, e, file=sys.stderr); continue
        for it in its:
            upd = evaluate(it, m15, now)
            upd = {k: v for k, v in upd.items() if v is not None}
            results.append({"id": it["id"], "kind": it.get("kind"), "pair": p, "update": upd})
            json.dump(upd, open(os.path.join(args.out, f"upd_{it['id']}.json"), "w"), ensure_ascii=False)
    json.dump(results, open(os.path.join(args.out, "track.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "track"])
    ap.add_argument("--key"); ap.add_argument("--out", default="radar_out")
    ap.add_argument("--inputs"); ap.add_argument("--items"); ap.add_argument("--data-dir")
    a = ap.parse_args()
    if not a.key and not a.data_dir:
        sys.exit("--key ou --data-dir requis")
    (cmd_scan if a.cmd == "scan" else cmd_track)(a)
