import csv
import os
import random
from datetime import datetime
from statistics import NormalDist

from scanner import (api_get, group_into_pairs, load_live_markets, make_session,
                     TOURNAMENT_SLUG)


INPUT_FILE = "senate_races.csv"
SIMULATIONS = 20000
SEED = 42

NATIONAL_ERROR_SD = 3.0
STATE_ERROR_SD = 3.5
NATIONAL_POLL_BIAS = 0.0
POLL_WEIGHT = 0.5

D_SEATS_NOT_UP = 34
R_SEATS_NOT_UP = 31
SEATS_FOR_D_CONTROL = 51
THIRD_CANDIDATE_STATES = {"Nebraska"}
INDEPENDENT_CAUCUSES_WITH_D = False

KELLY_FRACTION = 0.25
MAX_STAKE_PCT = 0.10
MIN_EDGE = 0.03

TOTAL_SD = (NATIONAL_ERROR_SD ** 2 + STATE_ERROR_SD ** 2) ** 0.5
NORMAL = NormalDist()


def load_inputs():
    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["poll_margin"] = float(r["poll_margin"]) if r["poll_margin"].strip() else None
    return rows


def mid(side):
    if side is None:
        return None
    if side["bid"] is not None and side["ask"] is not None:
        return (side["bid"] + side["ask"]) / 2
    return side["last"]


def market_prob_not_r(state, sides):
    d_mid, r_mid = mid(sides.get("Democratic")), mid(sides.get("Republican"))
    if r_mid is None:
        return None
    if state in THIRD_CANDIDATE_STATES or d_mid is None:
        return 1 - r_mid / 100
    return d_mid / (d_mid + r_mid)


def prob_to_margin(p):
    p = min(max(p, 0.005), 0.995)
    return TOTAL_SD * NORMAL.inv_cdf(p)


def build_races(inputs, pairs):
    races = []
    for row in inputs:
        state = row["state"]
        sides = pairs.get(f"{state} Senate", {})
        p_market = market_prob_not_r(state, sides) if sides else None
        poll = row["poll_margin"]

        if poll is not None and p_market is not None:
            margin = POLL_WEIGHT * poll + (1 - POLL_WEIGHT) * prob_to_margin(p_market)
        elif poll is not None:
            margin = poll
        elif p_market is not None:
            margin = prob_to_margin(p_market)
        else:
            margin = prob_to_margin(0.05 if row["holder"] == "R" else 0.95)
            print(f"Warning: no poll or market for {state}; assuming safe {row['holder']}.")

        races.append({"state": state, "holder": row["holder"], "poll": poll,
                      "p_market": p_market, "margin": margin + NATIONAL_POLL_BIAS,
                      "sides": sides, "wins": 0})
    return races


def simulate(races):
    rng = random.Random(SEED)
    outcomes = []
    for _ in range(SIMULATIONS):
        national = rng.gauss(0, NATIONAL_ERROR_SD)
        d_seats = D_SEATS_NOT_UP
        results = []
        for race in races:
            not_r_wins = race["margin"] + national + rng.gauss(0, STATE_ERROR_SD) > 0
            results.append(not_r_wins)
            if not_r_wins:
                race["wins"] += 1
                if race["state"] not in THIRD_CANDIDATE_STATES or INDEPENDENT_CAUCUSES_WITH_D:
                    d_seats += 1
        outcomes.append((results, d_seats))
    for race in races:
        race["p_model"] = race["wins"] / SIMULATIONS
    p_control = sum(1 for _, s in outcomes if s >= SEATS_FOR_D_CONTROL) / SIMULATIONS
    seat_counts = sorted(s for _, s in outcomes)
    return p_control, seat_counts, outcomes


def bets_on_not_r(action):
    return action in ("BUY YES on Dem", "BUY NO on Rep")


def portfolio_pnl(races, control_trade, control_stake, outcomes):
    bets = [(i, r["trade"], r["stake"]) for i, r in enumerate(races) if r["stake"] > 0]
    pnls = []
    for results, d_seats in outcomes:
        pnl = 0.0
        for i, trade, stake in bets:
            won = results[i] if bets_on_not_r(trade["action"]) else not results[i]
            pnl += stake / trade["price"] - stake if won else -stake
        if control_stake > 0:
            d_control = d_seats >= SEATS_FOR_D_CONTROL
            won = d_control if bets_on_not_r(control_trade["action"]) else not d_control
            pnl += control_stake / control_trade["price"] - control_stake if won else -control_stake
        pnls.append(pnl)
    pnls.sort()
    return pnls


def best_trade(p_model, sides, third_candidate):
    d, r = sides.get("Democratic"), sides.get("Republican")
    options = []
    if d and d["ask"] is not None and not third_candidate:
        options.append(("BUY YES on Dem", d["ask"] / 100, p_model))
    if r and r["bid"] is not None:
        options.append(("BUY NO on Rep", 1 - r["bid"] / 100, p_model))
    if r and r["ask"] is not None:
        options.append(("BUY YES on Rep", r["ask"] / 100, 1 - p_model))
    if d and d["bid"] is not None and not third_candidate:
        options.append(("BUY NO on Dem", 1 - d["bid"] / 100, 1 - p_model))
    if not options:
        return None
    action, price, p_win = max(options, key=lambda o: o[2] - o[1])
    return {"action": action, "price": price, "p_win": p_win, "edge": p_win - price}


def stake_for(trade, bankroll):
    if trade is None or trade["edge"] < MIN_EDGE:
        return 0
    kelly = (trade["p_win"] - trade["price"]) / (1 - trade["price"])
    return round(min(KELLY_FRACTION * kelly, MAX_STAKE_PCT) * bankroll)


def get_bankroll(session):
    data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}")
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    return float(data.get("myBalance") or 0)


def pct(x):
    return "    -" if x is None else f"{100 * x:5.1f}"


def main():
    markets = load_live_markets()
    pairs = group_into_pairs(markets)
    bankroll = get_bankroll(make_session())

    races = build_races(load_inputs(), pairs)
    p_control, seats, outcomes = simulate(races)

    rows = []
    for race in races:
        third = race["state"] in THIRD_CANDIDATE_STATES
        trade = best_trade(race["p_model"], race["sides"], third) if race["sides"] else None
        stake = stake_for(trade, bankroll) if race["poll"] is not None else 0
        race.update({"trade": trade, "stake": stake})
        rows.append(race)
    rows.sort(key=lambda r: -(r["trade"]["edge"] if r["trade"] else -1))

    print()
    print(f"{'STATE':<16}{'HOLD':>5}{'POLL':>7}{'MARKET':>8}{'MODEL':>7}"
          f"   {'BEST TRADE':<16}{'PRICE':>7}{'EDGE':>7}{'STAKE':>9}")
    print("-" * 90)
    for r in rows:
        poll = "     -" if r["poll"] is None else f"{r['poll']:+6.1f}"
        t = r["trade"]
        if t and r["stake"] > 0:
            trade_txt = f"   {t['action']:<16}{t['price']:>7.3f}{100 * t['edge']:>+6.1f}{r['stake']:>9,}"
        elif t:
            trade_txt = f"   {'(no bet)':<16}{'':>7}{100 * t['edge']:>+6.1f}{'':>9}"
        else:
            trade_txt = "   (no market)"
        print(f"{r['state']:<16}{r['holder']:>5}{poll:>7}{pct(r['p_market']):>8}"
              f"{pct(r['p_model']):>7}{trade_txt}")
    print("-" * 90)

    def pctile(q):
        return seats[int(q * (len(seats) - 1))]

    control_sides = pairs.get("U.S. Senate", {})
    market_control = mid(control_sides.get("Democratic"))
    print(f"\nSENATE CONTROL (Dem needs {SEATS_FOR_D_CONTROL})")
    print(f"  Model:  Dem control {100 * p_control:.1f}%   |   "
          f"Market: {'-' if market_control is None else f'{market_control:.1f}%'}")
    print(f"  Dem seats: median {pctile(0.5)}, 80% range {pctile(0.1)}-{pctile(0.9)}")

    control_trade = best_trade(p_control, control_sides, False) if control_sides else None
    control_stake = stake_for(control_trade, bankroll)
    if control_trade and control_stake > 0:
        print(f"  Trade: {control_trade['action']} at {control_trade['price']:.3f} "
              f"(edge {100 * control_trade['edge']:+.1f}) -> stake {control_stake:,}")

    pnls = portfolio_pnl(races, control_trade, control_stake, outcomes)
    n = len(pnls)
    print("\nPORTFOLIO (all suggested bets together, same simulations)")
    print(f"  Expected profit: {sum(pnls) / n:+,.0f}   |   Chance of losing money: "
          f"{100 * sum(1 for p in pnls if p < 0) / n:.1f}%")
    print(f"  Bad case (5th pct): {pnls[int(0.05 * (n - 1))]:+,.0f}   |   Median: "
          f"{pnls[n // 2]:+,.0f}   |   Good case (95th pct): {pnls[int(0.95 * (n - 1))]:+,.0f}")

    total = sum(r["stake"] for r in rows) + control_stake
    print(f"\nBankroll {bankroll:,.0f} | suggested total stake {total:,} "
          f"(Kelly x{KELLY_FRACTION}, max {int(MAX_STAKE_PCT * 100)}% per race, min edge {int(MIN_EDGE * 100)} pts)")

    os.makedirs("output", exist_ok=True)
    filename = os.path.join("output", f"senate_{datetime.now():%Y-%m-%d_%H%M}.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["state", "holder", "poll_margin", "market_p", "model_p",
                         "action", "price", "edge", "stake"])
        for r in rows:
            t = r["trade"] or {}
            writer.writerow([r["state"], r["holder"], r["poll"], r["p_market"], r["p_model"],
                             t.get("action"), t.get("price"), t.get("edge"), r["stake"]])
    print(f"Saved report to {filename}")


if __name__ == "__main__":
    main()
