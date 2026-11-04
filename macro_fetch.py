#!/usr/bin/env python3
"""Mon Desk : récupération des données macro historiques (lancé sur GitHub, qui a accès à internet).

Écrit macro/<NOM>.json = [[date ISO, valeur], ...] et macro/report.json (ce qui a marché ou non).
- Rendements à 2 ans : US (FRED DGS2), zone euro (BCE, courbe AAA 2 ans), Royaume-Uni (FRED / BoE), Canada (Banque du Canada),
  Australie (RBA F2), Nouvelle-Zélande (RBNZ B2), Suisse (BNS), Japon (ministère des Finances).
- Taux à 3 mois mensuels de l'OCDE (FRED, IR3TIB01xxM156N) en secours pour chaque pays.
- VIX (FRED VIXCLS).
- COT CFTC « Traders in Financial Futures » (futures), 2019 à aujourd'hui.
"""
import csv, io, json, os, re, sys, urllib.request, zipfile
from datetime import datetime

OUT = "macro"
UA = {"User-Agent": "Mozilla/5.0 (mondesk-backtest)"}
report = {}


def get(url, binary=False, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def save(name, rows, src):
    rows = sorted({d: v for d, v in rows if v is not None}.items())
    json.dump(rows, open(os.path.join(OUT, name + ".json"), "w"))
    report[name] = {"ok": bool(rows), "n": len(rows), "from": rows[0][0] if rows else None, "to": rows[-1][0] if rows else None, "src": src}
    print(name, report[name], flush=True)


def fnum(s):
    try:
        s = str(s).strip().replace(",", ".")
        return float(s) if s not in ("", ".", "NA", "ND", "-") else None
    except ValueError:
        return None


def fred(series):
    t = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")
    rows = []
    for r in list(csv.reader(io.StringIO(t)))[1:]:
        if len(r) >= 2 and r[0] >= "2018-01-01":
            rows.append((r[0][:10], fnum(r[1])))
    return rows


def attempt(name, fn, src):
    try:
        rows = fn()
        if not rows:
            raise RuntimeError("aucune ligne")
        save(name, rows, src)
        return True
    except Exception as e:
        report.setdefault(name + "_errors", []).append(f"{src}: {e}")
        print(name, "échec", src, e, file=sys.stderr, flush=True)
        return False


def ecb_2y():
    t = get("https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y?format=csvdata&startPeriod=2018-01-01")
    rd = csv.DictReader(io.StringIO(t))
    return [(r["TIME_PERIOD"], fnum(r["OBS_VALUE"])) for r in rd]


def boc_2y():
    t = get("https://www.bankofcanada.ca/valet/observations/BD.CDN.2YR.DQ.YLD/json?start_date=2018-01-01")
    js = json.loads(t)
    return [(o["d"], fnum(o.get("BD.CDN.2YR.DQ.YLD", {}).get("v"))) for o in js["observations"]]


def rba_2y():
    t = get("https://www.rba.gov.au/statistics/tables/csv/f2-data.csv")
    rows = list(csv.reader(io.StringIO(t)))
    head = next(r for r in rows if r and r[0].strip().lower() == "title")
    col = next(i for i, h in enumerate(head) if "2 year" in h.lower() and "commonwealth" in h.lower())
    out = []
    for r in rows:
        if r and re.match(r"\d{2}-[A-Za-z]{3}-\d{4}", r[0].strip()):
            d = datetime.strptime(r[0].strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
            if d >= "2018-01-01" and col < len(r):
                out.append((d, fnum(r[col])))
    return out


def rbnz_2y():
    import openpyxl
    b = get("https://www.rbnz.govt.nz/-/media/project/sites/rbnz/files/statistics/series/b/b2/hb2-daily.xlsx", binary=True)
    wb = openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head_i = next(i for i, r in enumerate(rows) if r and any(isinstance(c, str) and "2 year" in c.lower() for c in r if c))
    col = next(j for j, c in enumerate(rows[head_i]) if isinstance(c, str) and "2 year" in c.lower() and "swap" not in c.lower())
    out = []
    for r in rows[head_i + 1:]:
        if r and isinstance(r[0], datetime) and r[0].year >= 2018:
            out.append((r[0].strftime("%Y-%m-%d"), fnum(r[col])))
    return out


def snb_2y():
    t = get("https://data.snb.ch/api/cube/rendoblid/data/csv/en")
    out = []
    for r in csv.reader(io.StringIO(t), delimiter=";"):
        if len(r) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}", r[0]) and r[1].strip() in ("2J", "2Y") and r[0] >= "2018-01-01":
            out.append((r[0], fnum(r[2])))
    return out


def mof_2y():
    out = []
    for url in ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv",
                "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"):
        t = get(url)
        rows = list(csv.reader(io.StringIO(t)))
        hi = next(i for i, r in enumerate(rows) if r and r[0].strip().lower() == "date")
        col = rows[hi].index("2Y")
        for r in rows[hi + 1:]:
            if r and re.match(r"\d{4}/\d{1,2}/\d{1,2}", r[0]):
                d = datetime.strptime(r[0], "%Y/%m/%d").strftime("%Y-%m-%d")
                if d >= "2018-01-01":
                    out.append((d, fnum(r[col])))
    return out


def boe_2y():
    url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/2018"
           "&Dateto=now&SeriesCodes=IUDSNZC&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    t = get(url)
    out = []
    for r in csv.reader(io.StringIO(t)):
        if len(r) >= 2 and re.match(r"\d{2} [A-Za-z]{3} \d{4}", r[0]):
            out.append((datetime.strptime(r[0], "%d %b %Y").strftime("%Y-%m-%d"), fnum(r[1])))
    return out


def cot():
    rows = []
    for y in range(2019, datetime.utcnow().year + 1):
        try:
            b = get(f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{y}.zip", binary=True, timeout=120)
        except Exception as e:
            report.setdefault("COT_errors", []).append(f"{y}: {e}"); continue
        z = zipfile.ZipFile(io.BytesIO(b))
        t = z.read(z.namelist()[0]).decode("latin-1")
        for r in csv.DictReader(io.StringIO(t)):
            name = r.get("Market_and_Exchange_Names", "")
            code = None
            for k, c in (("EURO FX - ", "EUR"), ("BRITISH POUND", "GBP"), ("JAPANESE YEN", "JPY"), ("SWISS FRANC", "CHF"),
                         ("CANADIAN DOLLAR", "CAD"), ("AUSTRALIAN DOLLAR", "AUD"), ("NZ DOLLAR", "NZD"), ("NEW ZEALAND DOLLAR", "NZD"),
                         ("USD INDEX", "USD"), ("U.S. DOLLAR INDEX", "USD")):
                if name.upper().startswith(k):
                    code = c; break
            if not code or "CROSS" in name.upper() or "E-MINI" in name.upper():
                continue
            d = r.get("Report_Date_as_YYYY-MM-DD") or r.get("As_of_Date_In_Form_YYMMDD")
            try:
                lev = int(r["Lev_Money_Positions_Long_All"]) - int(r["Lev_Money_Positions_Short_All"])
                am = int(r["Asset_Mgr_Positions_Long_All"]) - int(r["Asset_Mgr_Positions_Short_All"])
                oi = int(r["Open_Interest_All"])
            except (KeyError, ValueError):
                continue
            rows.append({"date": d[:10], "ccy": code, "levNet": lev, "amNet": am, "oi": oi, "market": name})
    json.dump(rows, open(os.path.join(OUT, "COT.json"), "w"))
    report["COT"] = {"ok": bool(rows), "n": len(rows), "ccys": sorted({r["ccy"] for r in rows}),
                     "from": min((r["date"] for r in rows), default=None), "to": max((r["date"] for r in rows), default=None)}
    print("COT", report["COT"], flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    attempt("Y2_USD", lambda: fred("DGS2"), "FRED DGS2")
    attempt("Y2_EUR", ecb_2y, "BCE courbe AAA 2 ans") or attempt("Y2_EUR", lambda: fred("IR3TIB01EZM156N"), "OCDE 3 mois (mensuel)")
    attempt("Y2_GBP", boe_2y, "BoE IUDSNZC") or attempt("Y2_GBP", lambda: fred("IR3TIB01GBM156N"), "OCDE 3 mois (mensuel)")
    attempt("Y2_CAD", boc_2y, "Banque du Canada 2 ans") or attempt("Y2_CAD", lambda: fred("IR3TIB01CAM156N"), "OCDE 3 mois (mensuel)")
    attempt("Y2_AUD", rba_2y, "RBA F2") or attempt("Y2_AUD", lambda: fred("IR3TIB01AUM156N"), "OCDE 3 mois (mensuel)")
    attempt("Y2_NZD", rbnz_2y, "RBNZ B2") or attempt("Y2_NZD", lambda: fred("IR3TIB01NZM156N"), "OCDE 3 mois (mensuel)")
    attempt("Y2_CHF", snb_2y, "BNS rendoblid") or attempt("Y2_CHF", lambda: fred("IR3TIB01CHM156N"), "OCDE 3 mois (mensuel)")
    attempt("Y2_JPY", mof_2y, "Ministère des Finances japonais") or attempt("Y2_JPY", lambda: fred("IR3TIB01JPM156N"), "OCDE 3 mois (mensuel)")
    attempt("VIX", lambda: fred("VIXCLS"), "FRED VIXCLS")
    try:
        cot()
    except Exception as e:
        report["COT"] = {"ok": False, "error": str(e)}
    json.dump(report, open(os.path.join(OUT, "report.json"), "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
