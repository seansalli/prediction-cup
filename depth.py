import csv
import os
import time
from datetime import datetime

from scanner import (api_get, fetch_all_markets, fetch_quotes, get_tournament_id,
                     group_into_pairs, make_session, yes_exchange_and_last_price,
                     THIRD_CANDIDATE_RACES)


TOP_N = 15
MAX_SPEND_PER_PAIR = 10000
MIN_EDGE_PER_SHARE = 0.0
BOOK_DEPTH = 200


def fetch_bids(session, exchange_id, tournament_id):
    data = api_get(session, f"/exchanges/{exchange_id}/orderbook",
                   {"depth": BOOK_DEPTH, "tournamentId": tournament_id})
    bids = [(float(level["price"]), float(level["quantity"])) for level in data.get("bids", [])]
    bids.sort(key=lambda level: level[0], reverse=True)
    return bids


def walk_books(dem_bids, rep_bids, budget):
    i = j = 0
    dem_left = dem_bids[0][1] if dem_bids else 0
    rep_left = rep_bids[0][1] if rep_bids else 0
    shares = cost = 0.0
    worst_dem = worst_rep = None

    while i < len(dem_bids) and j < len(rep_bids):
        dem_price, rep_price = dem_bids[i][0], rep_bids[j][0]
        pair_cost = 2 - dem_price - rep_price
        if 1 - pair_cost <= MIN_EDGE_PER_SHARE + 1e-9:
            break

        qty = min(dem_left, rep_left, (budget - cost) / pair_cost)
        if qty <= 1e-9:
            break

        shares += qty
        cost += qty * pair_cost
        worst_dem, worst_rep = dem_price, rep_price
        dem_left -= qty
        rep_left -= qty

        if dem_left <= 1e-9:
            i += 1
            dem_left = dem_bids[i][1] if i < len(dem_bids) else 0
        if rep_left <= 1e-9:
            j += 1
            rep_left = rep_bids[j][1] if j < len(rep_bids) else 0

    return {"shares": int(shares), "cost": cost * int(shares) / shares if shares else 0.0,
            "worst_dem_bid": worst_dem, "worst_rep_bid": worst_rep}


def find_candidates(session, tournament_id):
    raw = fetch_all_markets(session, tournament_id)
    markets = []
    for m in raw:
        exchange_id, last = yes_exchange_and_last_price(m, tournament_id)
        if exchange_id:
            markets.append({"title": m["title"], "exchange_id": exchange_id, "last": last})

    quotes = fetch_quotes(session, [m["exchange_id"] for m in markets], tournament_id)
    for m in markets:
        q = quotes.get(m["exchange_id"], {})
        m["bid"] = q.get("bid")

    candidates = []
    for race, sides in group_into_pairs(markets).items():
        if "Democratic" not in sides or "Republican" not in sides:
            continue
        d, r = sides["Democratic"], sides["Republican"]
        if d["bid"] is None or r["bid"] is None:
            continue
        edge = d["bid"] + r["bid"] - 100
        if edge > 0:
            candidates.append({"race": race, "edge_top": edge,
                               "dem_exchange": d["exchange_id"], "rep_exchange": r["exchange_id"]})

    candidates.sort(key=lambda c: c["edge_top"], reverse=True)
    return candidates[:TOP_N]


def analyse_depth(session, tournament_id, candidates):
    rows = []
    for c in candidates:
        dem_bids = fetch_bids(session, c["dem_exchange"], tournament_id)
        rep_bids = fetch_bids(session, c["rep_exchange"], tournament_id)
        time.sleep(0.2)

        result = walk_books(dem_bids, rep_bids, MAX_SPEND_PER_PAIR)
        shares, cost = result["shares"], result["cost"]
        if shares == 0:
            continue
        profit = shares - cost
        rows.append({
            "race": c["race"],
            "shares_each_side": shares,
            "total_cost": round(cost, 2),
            "guaranteed_payout": shares,
            "guaranteed_profit": round(profit, 2),
            "return_pct": round(100 * profit / cost, 2),
            "dem_no_limit": round(1 - result["worst_dem_bid"], 3),
            "rep_no_limit": round(1 - result["worst_rep_bid"], 3),
            "dem_exchange_id": c["dem_exchange"],
            "rep_exchange_id": c["rep_exchange"],
            "note": "third candidate (still safe for NO pair)" if c["race"] in THIRD_CANDIDATE_RACES else "",
        })
    rows.sort(key=lambda r: r["guaranteed_profit"], reverse=True)
    return rows


def print_report(rows):
    print()
    print(f"{'RACE':<26}{'SHARES':>9}{'COST':>11}{'PROFIT':>9}{'RETURN':>8}"
          f"   {'BUY NO (Dem) <=':>16}{'BUY NO (Rep) <=':>17}")
    print("-" * 100)
    for r in rows:
        print(f"{r['race']:<26}{r['shares_each_side']:>9,}{r['total_cost']:>11,.2f}"
              f"{r['guaranteed_profit']:>9,.2f}{r['return_pct']:>7.2f}%"
              f"   {r['dem_no_limit']:>16.3f}{r['rep_no_limit']:>17.3f}")
    print("-" * 100)
    total_cost = sum(r["total_cost"] for r in rows)
    total_profit = sum(r["guaranteed_profit"] for r in rows)
    print(f"{len(rows)} pairs | capital needed: {total_cost:,.2f} SUSQies | "
          f"guaranteed profit: {total_profit:,.2f} SUSQies")


def save_report(rows):
    os.makedirs("output", exist_ok=True)
    filename = os.path.join("output", f"depth_{datetime.now():%Y-%m-%d_%H%M}.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["race"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved report to {filename}")


def main():
    session = make_session()
    tournament_id = get_tournament_id(session)
    candidates = find_candidates(session, tournament_id)
    print(f"Checking order book depth for {len(candidates)} arb pairs...")
    rows = analyse_depth(session, tournament_id, candidates)
    print_report(rows)
    save_report(rows)


if __name__ == "__main__":
    main()
