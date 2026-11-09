from scanner import api_get, make_session, TOURNAMENT_SLUG

session = make_session()
rows = []
for offset in (0, 100, 200, 300, 400, 500):
    data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}/leaderboard",
                   {"period": "all", "limit": 100, "offset": offset, "sort": "pnl"})
    page = data.get("leaderboard", [])
    rows.extend(page)
    my_rank, total = data.get("myRank"), data.get("total")
    if len(page) < 100:
        break

print(f"Players: {total}   |   Your rank: {my_rank}\n")
print(f"{'RANK':>6}{'P&L':>14}{'TRADES':>9}{'VOLUME':>14}")
for target in (1, 3, 10, 25, 50, 75, 100, 150, 200, 300, 400, 500):
    if target <= len(rows):
        r = rows[target - 1]
        print(f"{target:>6}{r.get('pnl', 0):>+14,.0f}{r.get('tradesCount') or 0:>9,}{r.get('volume') or 0:>14,.0f}")

bots = [r for r in rows[:100] if (r.get("tradesCount") or 0) >= 1000]
print(f"\nIn the top 100: {len(bots)} accounts have 1,000+ trades (likely bots), "
      f"{100 - len(bots)} have fewer.")
