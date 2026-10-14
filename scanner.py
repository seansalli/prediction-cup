"""
Price Scanner

List of markets from platform api

Finds pairs of markets about the same race; por ejemplo
"Will the Democratic Party win the Texas Senate?"
"Will the Republican Party win the Texas Senate?"

Adds the two YES prices together. In a two-party race they should add up
to about 100. If they don't, one or both markets may be mispriced.

Prints a report + saves it
"""


# IMPORTS
import csv          # read/write CSV (spreadsheet) files -- built into Python
import json         # turn the API's text responses into Python data -- built in
import os           # read settings like your API key from the environment -- built in
import time         # lets us pause between requests -- built in
import re           # "regular expressions": pattern-matching on text -- built in
from datetime import datetime  # timestamps for our output files -- built in

import requests                 # makes web requests (installed with pip)
from dotenv import load_dotenv  # reads secrets from a .env file (installed with pip)



# SETTINGS

# if "sample" uses sample_markets.csv
# if "live" uses call the real API
DATA_SOURCE = "live"

# Flag a pair if its prices add up to more than 100 + THRESHOLD
# or less than 100 - THRESHOLD
THRESHOLD = 2

SAMPLE_FILE = "sample_markets.csv"

# LIVE API SETTINGS
BASE_URL = "https://sig.thesuper.market/api/v1"
MARKETS_ENDPOINT = "/markets"
AUTH_HEADER_NAME = "Authorization"
AUTH_HEADER_FORMAT = "Bearer {key}"

SHOW_RAW_EXAMPLE = True



# Get markets

def load_sample_markets(path):
    """Read markets from a CSV file."""
    markets = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            markets.append({
                "market_id": row["market_id"],
                "title": row["title"],
                "yes_price": float(row["yes_price"]),
            })
    return markets


def find_market_list(data):
    if isinstance(data, list):
        return data
    for key in ("markets", "data", "results", "items"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data[key]
    raise ValueError("Couldn't find a list of markets in the API response. "
                     "Look at the printed response and update find_market_list().")


def pick_first(d, candidate_keys):
    """Return the value of the first key in candidate_keys that exists in d."""
    for key in candidate_keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def get_yes_price(m):
    for exchange in m.get("exchanges", []):
        if exchange.get("option") == "YES" and exchange.get("latestPrice") is not None:
            return float(exchange["latestPrice"]) * 100
    return None


def is_midterm_market(m):
    for context in m.get("contexts", []):
        tournament = context.get("tournament") or {}
        if tournament.get("slug") == "midterm-elections":
            return True
    return False


def find_next_cursor(data):
    if not isinstance(data, dict):
        return None
    places = [data, data.get("pagination") or {}, data.get("meta") or {}]
    for place in places:
        for key in ("nextCursor", "next_cursor", "cursor", "next"):
            value = place.get(key) if isinstance(place, dict) else None
            if value and isinstance(value, (str, int)):
                return value
    return None


def fetch_all_raw_markets(url, headers):
    all_markets = []
    seen_ids = set()
    params = {}
    page = 1

    while page <= 50:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if page == 1:
            print(f"Status code: {response.status_code}") #200
        response.raise_for_status()
        data = response.json()
        if page == 1 and isinstance(data, dict):
            print(f"Top-level keys in the reply: {list(data.keys())}")

        items = find_market_list(data)
        new_items = [m for m in items if m.get("id") not in seen_ids]
        if not new_items:
            break
        for m in new_items:
            seen_ids.add(m.get("id"))
        all_markets.extend(new_items)

        cursor = find_next_cursor(data)
        page += 1
        if cursor:
            params = {"cursor": cursor}
        else:
            params = {"page": page, "offset": len(all_markets)}

        time.sleep(0.5)

    print(f"Fetched {len(all_markets)} markets over {page - 1} page(s).")
    if len(all_markets) <= 20:
        print("NOTE: only one page came back.")
    return all_markets


def fetch_live_markets():
    load_dotenv()
    api_key = os.environ.get("SUSQ_API_KEY")

    headers = {}
    if api_key:
        headers[AUTH_HEADER_NAME] = AUTH_HEADER_FORMAT.format(key=api_key)
    else:
        print("Note: no SUSQ_API_KEY found in .env -- trying without one.")

    url = BASE_URL + MARKETS_ENDPOINT
    print(f"Requesting {url} ...")
    raw_markets = fetch_all_raw_markets(url, headers)

    if SHOW_RAW_EXAMPLE and raw_markets:
        print(f"Field names in a market: {list(raw_markets[0].keys())}")

    markets = []
    skipped = 0
    for m in raw_markets:
        if not is_midterm_market(m) or m.get("status") != "open":
            skipped += 1
            continue
        price = get_yes_price(m)
        if m.get("title") is None or price is None:
            skipped += 1
            continue
        markets.append({
            "market_id": str(m.get("id")),
            "title": m["title"],
            "yes_price": price,
        })

    print(f"Kept {len(markets)} open midterm markets (skipped {skipped}).")
    return markets



# match pairs
TITLE_PATTERN = re.compile(r"Will the (Democratic|Republican) Party win the (.+)\?")


def group_into_pairs(markets):
    races = {}
    for m in markets:
        match = TITLE_PATTERN.search(m["title"])
        if not match:
            continue
        party, race = match.group(1), match.group(2)
        races.setdefault(race, {})[party] = m
    return races



# Check pairs
def check_pairs(races, threshold):
    results = []
    for race, sides in races.items():
        if "Democratic" not in sides or "Republican" not in sides:
            continue
        dem = sides["Democratic"]["yes_price"]
        rep = sides["Republican"]["yes_price"]
        total = dem + rep
        gap = total - 100

        if gap > threshold:
            signal = "OVERPRICED: both YES sides look expensive (consider NO)"
        elif gap < -threshold:
            signal = "UNDERPRICED: both YES sides look cheap (consider YES)"
        else:
            signal = "ok"

        results.append({
            "race": race,
            "dem_yes": dem,
            "rep_yes": rep,
            "total": round(total, 1),
            "gap": round(gap, 1),
            "signal": signal,
        })
    results.sort(key=lambda r: abs(r["gap"]), reverse=True)
    return results



# report
def print_report(results, unpaired_count):
    print(f"{'RACE':<28}{'DEM':>6}{'REP':>6}{'SUM':>7}{'GAP':>7}   SIGNAL")
    print("-" * 95)
    for r in results:
        print(f"{r['race']:<28}{r['dem_yes']:>6.0f}{r['rep_yes']:>6.0f}"
              f"{r['total']:>7.0f}{r['gap']:>+7.0f}   {r['signal']}")
    flagged = sum(1 for r in results if r["signal"] != "ok")
    print("-" * 95)
    print(f"{len(results)} pairs checked, {flagged} flagged. "
          f"({unpaired_count} races have only one side listed.)")


def save_report(results):
    os.makedirs("output", exist_ok=True)
    filename = os.path.join("output", f"scan_{datetime.now():%Y-%m-%d_%H%M}.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["race", "dem_yes", "rep_yes",
                                               "total", "gap", "signal"])
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved report to {filename}")



# MAIN HERE

def main():
    if DATA_SOURCE == "live":
        markets = fetch_live_markets()
    else:
        markets = load_sample_markets(SAMPLE_FILE)
    print(f"Loaded {len(markets)} markets ({DATA_SOURCE} mode).\n")

    races = group_into_pairs(markets)
    results = check_pairs(races, THRESHOLD)
    unpaired = sum(1 for sides in races.values() if len(sides) < 2)

    print_report(results, unpaired)
    save_report(results)


if __name__ == "__main__":
    main()
