import csv
import os
from datetime import datetime
from statistics import NormalDist

from scanner import (TOURNAMENT_SLUG, api_get, group_into_pairs, load_live_markets,
                     make_session)


PRACTICE = False
INPUT_FILE = "election_night_practice.csv" if PRACTICE else "election_night.csv"
PRIOR_FILE = "senate_races.csv"

REMAINING_SD = 12.0
EXPECTED_VOTE_SD = 1.5
MIN_EDGE = 0.05
KELLY_FRACTION = 0.25
MAX_STAKE_PER_RACE = 5000
THIRD_CANDIDATE_RACES = {"Nebraska Senate", "Alaska Senate"}

NORMAL = NormalDist()


def read_float(value):
    value = (value or "").strip().replace(",", "")
    return float(value) if value else None


def load_rows():
    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_priors():
    priors = {}
    if os.path.exists(PRIOR_FILE):
        with open(PRIOR_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                margin = read_float(r.get("poll_margin"))
                if margin is not None:
                    priors[f"{r['state']} Senate"] = margin
    return priors


def mid(side):
    if not side:
        return None
    if side["bid"] is not None and side["ask"] is not None:
        return (side["bid"] + side["ask"]) / 200
    return None if side["last"] is None else side["last"] / 100


def market_prob_d(race, sides):
    d, r = mid(sides.get("Democratic")), mid(sides.get("Republican"))
    if d is None or r is None:
        return d
    if race in THIRD_CANDIDATE_RACES:
        return d
    return d / (d + r)


def project(row, prior):
    d, r, pct = read_float(row["d_votes"]), read_float(row["r_votes"]), read_float(row["pct_reported"])
    if d is None or r is None or pct is None or d + r == 0:
        return None
    f = min(max(pct / 100, 0.0), 1.0)
    counted = 100 * (d - r) / (d + r)
    lean = read_float(row["remaining_lean"])
    if lean is not None:
        remaining = counted + lean
        basis = f"counted {lean:+.1f}"
    elif prior is not None:
        remaining = prior
        basis = "pre-election polls"
    else:
        remaining = counted
        basis = "same as counted"
    projected = f * counted + (1 - f) * remaining
    sd = (((1 - f) * REMAINING_SD) ** 2 + EXPECTED_VOTE_SD ** 2) ** 0.5
    return {"counted": counted, "projected": projected, "p_d": NORMAL.cdf(projected / sd),
            "pct": pct, "basis": basis}


def best_trade(p_d, sides, third):
    d, r = sides.get("Democratic"), sides.get("Republican")
    options = []
    if d and d["ask"] is not None and not third:
        options.append(("BUY YES on Dem", d["ask"] / 100, p_d))
    if r and r["bid"] is not None:
        options.append(("BUY NO on Rep", 1 - r["bid"] / 100, p_d))
    if r and r["ask"] is not None:
        options.append(("BUY YES on Rep", r["ask"] / 100, 1 - p_d))
    if d and d["bid"] is not None and not third:
        options.append(("BUY NO on Dem", 1 - d["bid"] / 100, 1 - p_d))
    if not options:
        return None
    action, price, p_win = max(options, key=lambda o: o[2] - o[1])
    return {"action": action, "price": price, "edge": p_win - price, "p_win": p_win}


def get_holdings(session, pairs):
    data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions")
    qty = {str(p["exchangeId"]): float(p["quantity"]) for p in data.get("positions", [])
           if not p.get("settled")}
    exposure = {}
    for race, sides in pairs.items():
        d_ex = sides.get("Democratic", {}).get("exchange_id")
        r_ex = sides.get("Republican", {}).get("exchange_id")
        d_q, r_q = qty.get(str(d_ex), 0), qty.get(str(r_ex), 0)
        exposure[race] = max(d_q, 0) + max(-r_q, 0) - max(-d_q, 0) - max(r_q, 0)
    return exposure


def get_cash(session):
    data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}")
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    return float(data.get("myBalance") or 0)


def main():
    markets = load_live_markets()
    pairs = group_into_pairs(markets)
    session = make_session()
    cash = get_cash(session)
    exposure = get_holdings(session, pairs)
    priors = load_priors()

    print(f"\n[{datetime.now():%H:%M:%S}] cash {cash:,.0f}")
    print(f"{'RACE':<24}{'IN %':>6}{'COUNTED':>9}{'PROJ':>7}{'P(D)':>7}{'MKT':>6}"
          f"   {'TRADE':<16}{'PRICE':>6}{'EDGE':>7}{'STAKE':>7}   YOU HOLD")
    print("-" * 115)
    for row in load_rows():
        race = row["race"].strip()
        sides = pairs.get(race)
        proj = project(row, priors.get(race))
        hold = exposure.get(race, 0)
        hold_txt = "" if abs(hold) < 1 else f"{abs(hold):,.0f} sh on {'Dem' if hold > 0 else 'Rep'}"
        if proj is None:
            continue
        market = market_prob_d(race, sides) if sides else None
        line = (f"{race:<24}{proj['pct']:>6.0f}{proj['counted']:>+9.1f}{proj['projected']:>+7.1f}"
                f"{100 * proj['p_d']:>7.1f}{'' if market is None else f'{100 * market:6.1f}':>6}")
        trade = best_trade(proj["p_d"], sides, race in THIRD_CANDIDATE_RACES) if sides else None
        if trade and trade["edge"] >= MIN_EDGE:
            kelly = (trade["p_win"] - trade["price"]) / (1 - trade["price"])
            stake = int(min(KELLY_FRACTION * kelly * cash, MAX_STAKE_PER_RACE, cash))
            limit_pct = round(round(trade["price"] * 200) / 2, 1)
            line += (f"   {trade['action']:<16}{trade['price']:>6.3f}{100 * trade['edge']:>+7.1f}"
                     f"{stake:>7,}   {hold_txt}")
            line += f"\n{'':<24}-> limit {limit_pct}, amount {stake:,} (~{int(stake / trade['price']):,} shares)"
        elif trade:
            line += f"   {'(no bet)':<16}{'':>6}{100 * trade['edge']:>+7.1f}{'':>7}   {hold_txt}"
        else:
            line += "   (no market)"
        print(line)
        if proj["basis"] != "pre-election polls" or proj["pct"] < 100:
            print(f"{'':<24}   remaining vote assumed: {proj['basis']}")
    print("-" * 115)
    print("PROJ = projected final margin (Dem minus Rep). Edit election_night.csv and rerun as counts update.")


if __name__ == "__main__":
    main()
