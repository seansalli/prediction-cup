from scanner import api_get, make_session, TOURNAMENT_SLUG

session = make_session()
data = api_get(session, f"/tournaments/{TOURNAMENT_SLUG}/leaderboard",
               {"period": "all", "limit": 25, "sort": "pnl"})

print(f"{'RANK':<5}{'USER':<22}{'PNL':>12}{'ROI %':>9}{'TRADES':>9}{'VOLUME':>14}"
      f"{'VOL/TRADE':>11}{'WIN %':>8}")
print("-" * 90)
for row in data.get("leaderboard", []):
    trades = row.get("tradesCount") or 0
    volume = row.get("volume") or 0
    per_trade = volume / trades if trades else 0
    roi = row.get("roi")
    win = row.get("winRate")
    print(f"{row.get('rank'):<5}{str(row.get('username'))[:21]:<22}{row.get('pnl', 0):>12,.0f}"
          f"{(roi if roi is not None else 0):>9.1f}{trades:>9,}{volume:>14,.0f}"
          f"{per_trade:>11,.1f}{(win if win is not None else 0):>8.1f}")
print("-" * 90)
print(f"Your rank: {data.get('myRank')} of {data.get('total')}")
