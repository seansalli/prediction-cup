from scanner import api_get, get_tournament_id, make_session

session = make_session()
tournament_id = get_tournament_id(session)

account = api_get(session, "/account", {"tournamentId": tournament_id})
if "data" in account and isinstance(account["data"], dict):
    account = account["data"]

print("\nACCOUNT")
for key in ("balance", "availableAdvance", "buyingPower"):
    print(f"  {key:<18} {account.get(key)}")

relationships = api_get(session, "/relationships", {"tournamentId": tournament_id})
items = relationships.get("data", relationships) if isinstance(relationships, dict) else relationships
print(f"\nRELATIONSHIPS (linked markets): {len(items) if isinstance(items, list) else '?'}")
if isinstance(items, list):
    for r in items[:5]:
        print(" ", {k: r.get(k) for k in list(r.keys())[:8]})
