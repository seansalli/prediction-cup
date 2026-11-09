from datetime import datetime, timedelta, timezone

from scanner import fetch_all_markets, fetch_quotes, get_tournament_id, make_session

DAYS_BACK = 10

session = make_session()
tournament_id = get_tournament_id(session)
markets = fetch_all_markets(session, tournament_id)
cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)

rows = []
for m in markets:
    yes = next((e for e in m.get("exchanges", []) if e.get("option") == "YES"), None)
    if not yes:
        continue
    created = datetime.fromisoformat(m["createdAt"].replace("Z", "+00:00"))
    last = yes.get("latestPrice")
    initial = yes.get("initialPrice")
    never_moved = last is not None and initial is not None and abs(last - initial) < 0.001
    if created >= cutoff or never_moved or last is None:
        rows.append({"title": m["title"], "created": created, "initial": initial,
                     "last": last, "exchange_id": str(yes["id"]), "never_moved": never_moved})

quotes = fetch_quotes(session, [r["exchange_id"] for r in rows], tournament_id) if rows else {}

print(f"\nMarkets created in the last {DAYS_BACK} days, never traded, or with no price: {len(rows)}\n")
print(f"{'CREATED':<12}{'START':>7}{'LAST':>7}{'BID':>7}{'ASK':>7}   MARKET")
for r in sorted(rows, key=lambda r: r["created"], reverse=True):
    q = quotes.get(r["exchange_id"], {})
    fmt = lambda x: "    -" if x is None else f"{x:5.1f}"
    print(f"{r['created']:%Y-%m-%d}  {fmt(None if r['initial'] is None else r['initial'] * 100):>6}"
          f"{fmt(None if r['last'] is None else r['last'] * 100):>7}{fmt(q.get('bid')):>7}{fmt(q.get('ask')):>7}"
          f"   {r['title']}{'   <-- never traded' if r['never_moved'] else ''}")
