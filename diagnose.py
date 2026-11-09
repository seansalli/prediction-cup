import csv
import os
import re

from scanner import TOURNAMENT_SLUG, api_get, make_session

TITLE = re.compile(r"Will the (Democratic|Republican) Party win the (.+)\?")

session = make_session()
data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions")
pnl = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}/portfolio/pnl", {"period": "all"})
if "data" in pnl and isinstance(pnl["data"], dict):
    pnl = pnl["data"]

races = {}
for p in data.get("positions", []):
    if p.get("settled") or not p.get("quantity"):
        continue
    m = TITLE.search(p.get("marketTitle", ""))
    race, party = (m.group(2), m.group(1)) if m else (p.get("marketTitle", "?"), "?")
    races.setdefault(race, []).append({
        "party": party, "qty": float(p["quantity"]), "cost": float(p["costBasis"]),
        "value": float(p["marketValue"]), "upnl": float(p["unrealizedPnl"]),
    })

pair_rows, bet_rows = [], []
for race, legs in races.items():
    qty = {l["party"]: l["qty"] for l in legs}
    is_pair = (len(legs) == 2 and qty.get("Democratic", 0) < 0 and qty.get("Republican", 0) < 0)
    shares = min(-qty.get("Democratic", 0), -qty.get("Republican", 0)) if is_pair else 0
    row = {"race": race, "cost": sum(l["cost"] for l in legs),
           "value": sum(l["value"] for l in legs), "upnl": sum(l["upnl"] for l in legs),
           "guaranteed": shares}
    (pair_rows if is_pair else bet_rows).append(row)

def show(title, rows, pairs):
    print(f"\n{title}")
    print(f"{'RACE':<26}{'COST':>11}{'VALUE NOW':>11}{'PAPER P&L':>11}" + (f"{'AT SETTLE':>11}" if pairs else ""))
    for r in sorted(rows, key=lambda r: r["upnl"]):
        extra = f"{r['guaranteed'] - r['cost']:>+11,.0f}" if pairs else ""
        print(f"{r['race']:<26}{r['cost']:>11,.0f}{r['value']:>11,.0f}{r['upnl']:>+11,.0f}{extra}")
    total = sum(r["upnl"] for r in rows)
    line = f"{'TOTAL':<26}{sum(r['cost'] for r in rows):>11,.0f}{sum(r['value'] for r in rows):>11,.0f}{total:>+11,.0f}"
    if pairs:
        line += f"{sum(r['guaranteed'] - r['cost'] for r in rows):>+11,.0f}"
    print(line)

show("ARBITRAGE PAIRS (guaranteed at settlement)", pair_rows, True)
show("DIRECTIONAL BETS / PARTIAL PAIRS", bet_rows, False)

realized = 0.0
log_file = os.path.join("output", "recycler_log.csv")
if os.path.exists(log_file):
    with open(log_file, newline="", encoding="utf-8") as f:
        realized = sum(float(r["profit"] or 0) for r in csv.DictReader(f)
                       if r["action"] == "close" and r["result"] == "filled")

print(f"\nAccount value: {pnl.get('totalAccountValue', 0):,.0f}   (started at 100,000)")
print(f"Bot profit already locked in by closes: {realized:+,.0f}")
