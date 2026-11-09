import csv
import os
import time
import uuid
from datetime import datetime

import requests
from dotenv import load_dotenv

from scanner import (BASE_URL, TOURNAMENT_SLUG, api_get, fetch_all_markets, fetch_quotes,
                     get_tournament_id, group_into_pairs, yes_exchange_and_last_price)


DRY_RUN = False
LOOP = True
CYCLE_SECONDS = 90

CHUNK_SHARES = 1000
MIN_CHUNK_SHARES = 50
OPEN_MIN_EDGE = 0.005
CLOSE_MIN_PROFIT = 0.0
CLOSE_MAX_ASK_SUM = 1.0
CASH_RESERVE = 20000
MAX_ORDERS_PER_CYCLE = 20
SECONDS_BETWEEN_ORDERS = 2.5

COOLDOWN_SECONDS = 900

LOG_FILE = os.path.join("output", "recycler_log.csv")
cooldown_until = {}


def make_trade_session():
    load_dotenv()
    key = os.environ.get("SUSQ_TRADE_API_KEY")
    if not key:
        raise SystemExit("No SUSQ_TRADE_API_KEY in .env. Create a key with read + trade scopes.")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {key}"
    return session


def tick(price):
    return round(round(price / 0.005) * 0.005, 3)


def get_cash(session, tournament_id):
    data = api_get(session, "/account", {"tournamentId": tournament_id})
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    return float(data.get("buyingPower") or data.get("balance") or 0)


def get_positions(session):
    data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions")
    positions = {}
    for p in data.get("positions", []):
        if not p.get("settled"):
            positions[str(p["exchangeId"])] = {"qty": float(p["quantity"]), "avg": float(p["avgCost"])}
    return positions


def load_races(session, tournament_id):
    raw = fetch_all_markets(session, tournament_id)
    markets = []
    for m in raw:
        exchange_id, last = yes_exchange_and_last_price(m, tournament_id)
        if exchange_id:
            markets.append({"title": m["title"], "exchange_id": str(exchange_id), "last": last})
    quotes = fetch_quotes(session, [m["exchange_id"] for m in markets], tournament_id)
    for m in markets:
        q = quotes.get(m["exchange_id"], {})
        m["bid"] = None if q.get("bid") is None else q["bid"] / 100
        m["ask"] = None if q.get("ask") is None else q["ask"] / 100
    races = []
    for name, sides in group_into_pairs(markets).items():
        if "Democratic" in sides and "Republican" in sides:
            races.append({"race": name, "d": sides["Democratic"], "r": sides["Republican"]})
    return races


def place_pair(session, tournament_id, legs, dry_run):
    body = {
        "legs": [{"exchangeId": ex, "side": side, "action": "buy", "quantity": qty,
                  "price": price, "tournamentId": tournament_id} for ex, side, qty, price in legs],
        "executionMode": "allOrNone",
        "idempotencyKey": f"recycler-{uuid.uuid4()}",
    }
    if dry_run:
        return "dry-run"
    for attempt in range(2):
        response = session.post(BASE_URL + "/orders/multi-leg", json=body, timeout=20)
        if response.status_code == 429 and attempt == 0:
            time.sleep(int(response.headers.get("Retry-After", 60)))
            continue
        if response.status_code == 200:
            return "filled"
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        if error.get("details", {}).get("rejectionReasonCode") == "AllOrNoneNotFilled":
            return "not-enough-shares"
        if response.status_code in (401, 403):
            raise SystemExit(f"Auth/scope problem: {response.text[:300]}")
        return f"error {response.status_code}: {error.get('code')} {error.get('message', '')[:120]}"
    return "rate-limited"


def log(row):
    os.makedirs("output", exist_ok=True)
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            writer.writeheader()
        writer.writerow(row)


def on_cooldown(key):
    return time.time() < cooldown_until.get(key, 0)


def note_result(key, result):
    if result == "not-enough-shares":
        cooldown_until[key] = time.time() + COOLDOWN_SECONDS


def try_sizes(session, tournament_id, make_legs, max_qty, dry_run):
    qty = min(CHUNK_SHARES, int(max_qty))
    floor = min(MIN_CHUNK_SHARES, qty)
    while qty >= max(floor, 1):
        result = place_pair(session, tournament_id, make_legs(qty), dry_run)
        if result != "not-enough-shares":
            return qty, result
        time.sleep(SECONDS_BETWEEN_ORDERS)
        if qty == floor:
            break
        qty = max(qty // 4, floor)
    return 0, "not-enough-shares"


def plan_and_trade(session, tournament_id, dry_run):
    races = load_races(session, tournament_id)
    positions = get_positions(session)
    cash = get_cash(session, tournament_id)
    print(f"\n[{datetime.now():%H:%M:%S}] cash {cash:,.0f} | reserve {CASH_RESERVE:,} | "
          f"{len(races)} race pairs | {'DRY RUN' if dry_run else 'LIVE'}")

    closes, opens, skipped, waiting = [], [], [], []
    for race in races:
        d, r = race["d"], race["r"]
        pd = positions.get(d["exchange_id"], {"qty": 0, "avg": 0})
        pr = positions.get(r["exchange_id"], {"qty": 0, "avg": 0})

        if pd["qty"] < 0 and pr["qty"] < 0:
            pairs = min(-pd["qty"], -pr["qty"])
            pair_cost = pd["avg"] + pr["avg"]
            if not 0.5 < pair_cost < 1.05:
                skipped.append(f"{race['race']} (odd avg cost {pair_cost:.3f})")
                continue
            if d["ask"] is not None and r["ask"] is not None:
                ask_sum = d["ask"] + r["ask"]
                profit = 2 - pair_cost - ask_sum
                beats_holding = ask_sum <= CLOSE_MAX_ASK_SUM + 1e-9
                if beats_holding and profit >= CLOSE_MIN_PROFIT and not on_cooldown(("close", race["race"])):
                    closes.append((profit, race, pairs, pair_cost))
                else:
                    waiting.append(f"{race['race']} ({int(pairs):,} pairs, YES asks {ask_sum:.3f})")
            continue

        if pd["qty"] != 0 or pr["qty"] != 0:
            skipped.append(race["race"])
            continue

        if d["bid"] is not None and r["bid"] is not None:
            edge = d["bid"] + r["bid"] - 1
            if edge >= OPEN_MIN_EDGE and not on_cooldown(("open", race["race"])):
                opens.append((edge, race))

    if skipped:
        print(f"  Not touching (your directional bets / partial pairs): {', '.join(skipped)}")
    if waiting:
        print(f"  Holding, waiting for a profitable close: {'; '.join(waiting)}")

    orders = 0
    for profit, race, pairs, pair_cost in sorted(closes, key=lambda c: c[0], reverse=True):
        if orders >= MAX_ORDERS_PER_CYCLE:
            break
        d, r = race["d"], race["r"]
        make = lambda q, d=d, r=r: [(d["exchange_id"], "yes", q, tick(d["ask"])),
                                     (r["exchange_id"], "yes", q, tick(r["ask"]))]
        qty, result = try_sizes(session, tournament_id, make, pairs, dry_run)
        note_result(("close", race["race"]), result)
        orders += 1
        gain = qty * profit
        print(f"  CLOSE {race['race']:<24} {qty:>6,} pairs  YES {d['ask']:.3f}+{r['ask']:.3f}  "
              f"profit {gain:+,.0f}  frees ~{qty * (2 - d['ask'] - r['ask']):,.0f}  -> {result}")
        log({"time": datetime.now().isoformat(timespec="seconds"), "action": "close",
             "race": race["race"], "pairs": qty, "price_d": d["ask"], "price_r": r["ask"],
             "profit": round(gain, 2), "result": result})
        if result in ("filled", "dry-run"):
            cash += qty * (2 - d["ask"] - r["ask"])
        time.sleep(SECONDS_BETWEEN_ORDERS)

    for edge, race in sorted(opens, key=lambda o: o[0], reverse=True):
        if orders >= MAX_ORDERS_PER_CYCLE:
            break
        d, r = race["d"], race["r"]
        pair_cost = 2 - d["bid"] - r["bid"]
        affordable = (cash - CASH_RESERVE) / pair_cost
        if affordable < MIN_CHUNK_SHARES:
            print(f"  Stopping opens: cash would drop below reserve ({CASH_RESERVE:,}).")
            break
        make = lambda q, d=d, r=r: [(d["exchange_id"], "no", q, tick(1 - d["bid"])),
                                     (r["exchange_id"], "no", q, tick(1 - r["bid"]))]
        qty, result = try_sizes(session, tournament_id, make, affordable, dry_run)
        note_result(("open", race["race"]), result)
        orders += 1
        print(f"  OPEN  {race['race']:<24} {qty:>6,} pairs  NO {1 - d['bid']:.3f}+{1 - r['bid']:.3f}  "
              f"edge {edge:+.3f}  cost {qty * pair_cost:,.0f}  -> {result}")
        log({"time": datetime.now().isoformat(timespec="seconds"), "action": "open",
             "race": race["race"], "pairs": qty, "price_d": 1 - d["bid"], "price_r": 1 - r["bid"],
             "profit": round(qty * edge, 2), "result": result})
        if result in ("filled", "dry-run"):
            cash -= qty * pair_cost
        time.sleep(SECONDS_BETWEEN_ORDERS)

    if not closes and not opens:
        print("  Nothing worth trading this cycle.")
    return orders


def main():
    dry_run = DRY_RUN
    if not dry_run:
        answer = input("LIVE mode places real orders. Type yes to continue: ")
        if answer.strip().lower() != "yes":
            raise SystemExit("Cancelled.")
    session = make_trade_session()
    tournament_id = get_tournament_id(session)
    while True:
        plan_and_trade(session, tournament_id, dry_run)
        if not LOOP:
            break
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
