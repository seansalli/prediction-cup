import csv
import os
import re
import time
from datetime import datetime

import requests
from dotenv import load_dotenv


DATA_SOURCE = "live"
BASE_URL = "https://sig.thesuper.market/api/v1"
TOURNAMENT_SLUG = "midterm-elections"
LAST_PRICE_THRESHOLD = 2
THIRD_CANDIDATE_RACES = {"Nebraska Senate"}
SAMPLE_FILE = "sample_markets.csv"


def make_session():
    load_dotenv()
    api_key = os.environ.get("SUSQ_API_KEY")
    if not api_key:
        raise SystemExit("No SUSQ_API_KEY found. Put your key in a file named .env "
                         "(see .env.example). The API requires a key.")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"
    return session


def api_get(session, path, params=None):
    url = BASE_URL + path
    for attempt in range(2):
        response = session.get(url, params=params, timeout=20)
        if response.status_code == 429 and attempt == 0:
            wait = int(response.headers.get("Retry-After", 60))
            print(f"Rate limited -- waiting {wait} seconds...")
            time.sleep(wait)
            continue
        if response.status_code >= 400:
            raise SystemExit(f"API error {response.status_code} on {path}: {response.text[:500]}")
        return response.json()
    raise SystemExit(f"Still rate limited on {path} -- try again in a minute.")


def to_points(price):
    return None if price is None else float(price) * 100


def get_tournament_id(session):
    data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}")
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    print(f"Tournament: {data.get('name')}  (your balance: {data.get('myBalance')})")
    return data["id"]


def fetch_all_markets(session, tournament_id):
    markets = []
    params = {"limit": 100, "status": "open", "tournamentId": tournament_id}
    for _ in range(50):
        data = api_get(session, "/markets", params)
        markets.extend(data["data"])
        pagination = data.get("pagination", {})
        if not pagination.get("hasMore") or not pagination.get("nextCursor"):
            break
        params["cursor"] = pagination["nextCursor"]
    print(f"Downloaded {len(markets)} open markets.")
    return markets


def yes_exchange_and_last_price(market, tournament_id):
    yes = next((e for e in market.get("exchanges", []) if e.get("option") == "YES"), None)
    if yes is None:
        return None, None
    last = yes.get("latestPrice")
    for ctx in market.get("contexts", []):
        if (ctx.get("tournament") or {}).get("id") == tournament_id:
            for e in ctx.get("exchanges", []):
                if e.get("id") == yes["id"] and e.get("latestPrice") is not None:
                    last = e["latestPrice"]
    return yes["id"], to_points(last)


def fetch_quotes(session, exchange_ids, tournament_id):
    quotes = {}
    for start in range(0, len(exchange_ids), 100):
        chunk = exchange_ids[start:start + 100]
        data = api_get(session, "/exchanges/prices",
                       {"ids": ",".join(chunk), "tournamentId": tournament_id})
        for q in data["data"]:
            quotes[q["exchangeId"]] = {"bid": to_points(q.get("bestBid")),
                                       "ask": to_points(q.get("bestAsk"))}
        if data.get("missingIds"):
            print(f"Note: no quotes for {len(data['missingIds'])} exchanges.")
    return quotes


def load_live_markets():
    session = make_session()
    tournament_id = get_tournament_id(session)
    raw = fetch_all_markets(session, tournament_id)

    markets = []
    for m in raw:
        exchange_id, last = yes_exchange_and_last_price(m, tournament_id)
        if exchange_id is None:
            continue
        markets.append({"market_id": m["id"], "title": m["title"],
                        "exchange_id": exchange_id, "last": last})

    quotes = fetch_quotes(session, [m["exchange_id"] for m in markets], tournament_id)
    for m in markets:
        q = quotes.get(m["exchange_id"], {})
        m["bid"], m["ask"] = q.get("bid"), q.get("ask")
    return markets


def load_sample_markets():
    with open(SAMPLE_FILE, newline="", encoding="utf-8") as f:
        return [{"market_id": r["market_id"], "title": r["title"],
                 "last": float(r["yes_price"]), "bid": None, "ask": None}
                for r in csv.DictReader(f)]


TITLE_PATTERN = re.compile(r"Will the (Democratic|Republican) Party win the (.+)\?")


def group_into_pairs(markets):
    races = {}
    for m in markets:
        match = TITLE_PATTERN.search(m["title"])
        if match:
            races.setdefault(match.group(2), {})[match.group(1)] = m
    return races


def add(a, b):
    return None if a is None or b is None else a + b


def analyse(races):
    rows = []
    for race, sides in races.items():
        if "Democratic" not in sides or "Republican" not in sides:
            continue
        d, r = sides["Democratic"], sides["Republican"]

        last_sum = add(d["last"], r["last"])
        bid_sum = add(d["bid"], r["bid"])
        ask_sum = add(d["ask"], r["ask"])
        no_edge = None if bid_sum is None else bid_sum - 100
        yes_edge = None if ask_sum is None else 100 - ask_sum

        if no_edge is not None and no_edge > 0:
            signal = f"ARB: buy NO on both -> +{no_edge:.1f} guaranteed per pair"
        elif yes_edge is not None and yes_edge > 0:
            if race in THIRD_CANDIDATE_RACES:
                signal = "YES pair looks cheap, but a third candidate can win -- NOT safe"
            else:
                signal = f"YES pair: +{yes_edge:.1f} if a Dem or Rep wins (check for independents)"
        elif last_sum is not None and abs(last_sum - 100) > LAST_PRICE_THRESHOLD:
            signal = "last prices off, but no edge at current bid/ask"
        else:
            signal = "ok"
        if race in THIRD_CANDIDATE_RACES and signal == "ok":
            signal = "third candidate in race"

        rows.append({
            "race": race,
            "dem_last": d["last"], "rep_last": r["last"], "last_sum": last_sum,
            "dem_bid": d["bid"], "dem_ask": d["ask"],
            "rep_bid": r["bid"], "rep_ask": r["ask"],
            "no_edge": no_edge, "yes_edge": yes_edge, "signal": signal,
        })

    def best_edge(row):
        edges = [e for e in (row["no_edge"], row["yes_edge"]) if e is not None]
        return max(edges) if edges else -999

    rows.sort(key=best_edge, reverse=True)
    return rows


def fmt(x, width=6, plus=False):
    if x is None:
        return "-".rjust(width)
    return f"{x:+{width}.1f}" if plus else f"{x:{width}.1f}"


def print_report(rows):
    print()
    print(f"{'RACE':<26}{'LAST':>6} | {'D bid':>6}{'D ask':>6}{'R bid':>6}{'R ask':>6}"
          f" | {'NO edge':>7}{'YES edge':>9}   SIGNAL")
    print("-" * 120)
    for r in rows:
        print(f"{r['race']:<26}{fmt(r['last_sum'])} | "
              f"{fmt(r['dem_bid'])}{fmt(r['dem_ask'])}{fmt(r['rep_bid'])}{fmt(r['rep_ask'])} | "
              f"{fmt(r['no_edge'], 7, True)}{fmt(r['yes_edge'], 9, True)}   {r['signal']}")
    print("-" * 120)
    arbs = sum(1 for r in rows if r["signal"].startswith("ARB"))
    yes_pairs = sum(1 for r in rows if r["signal"].startswith("YES pair:"))
    print(f"{len(rows)} pairs checked: {arbs} guaranteed NO-pair arbs, "
          f"{yes_pairs} YES-pair opportunities.")


def save_report(rows):
    os.makedirs("output", exist_ok=True)
    filename = os.path.join("output", f"scan_{datetime.now():%Y-%m-%d_%H%M}.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["race"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved report to {filename}")


def main():
    markets = load_live_markets() if DATA_SOURCE == "live" else load_sample_markets()
    print(f"Loaded {len(markets)} markets ({DATA_SOURCE} mode).")
    rows = analyse(group_into_pairs(markets))
    print_report(rows)
    save_report(rows)


if __name__ == "__main__":
    main()
