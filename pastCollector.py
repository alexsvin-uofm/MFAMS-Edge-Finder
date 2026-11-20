import json
import time
import warnings
import requests
import pandas as pd

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

GAMMA_BASE_URL = "https://" + "".join(["gamma-api", ".", "polymarket", ".", "com"])
DATA_API_V2_URL = "https://" + "".join(["data-api", ".", "polymarket", ".", "com"]) + "/v2/trades"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

# 2025-2026 College Basketball Season window
SEASON_START = "2025-11-01"
SEASON_END = "2026-04-15"

EXCLUDED_TERMS = [
    "michigan state",
    "central michigan",
    "eastern michigan",
    "western michigan",
    "northern michigan",
    "women's",
    "womens",
    "(w)",
    "cfb",
    "football",
    "hockey",
    "baseball",
    "softball",
    "soccer"
]

NON_MONEYLINE_TERMS = [
    "spread",
    "o/u",
    "over/under",
    "total",
    "1h",
    "2h",
    "first half",
    "second half",
    "points",
    "rebounds",
    "assists",
    "threes",
    "by >",
    "by more than"
]


def safe_get_json(url, params=None):
    """Makes a GET request and safely parses JSON."""
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    if not resp.text or not resp.text.strip():
        raise ValueError(f"Empty response from {resp.url} (Status {resp.status_code})")
    return resp.json()


def is_michigan_25_26_mens_bball(event):
    """Verifies the event is 2025-2026 Michigan Men's Basketball."""
    title = event.get("title", "").lower()
    slug = event.get("slug", "").lower()
    end_date = event.get("endDate", "")[:10]
    tags = [t.get("slug", "").lower() for t in event.get("tags", [])]
    combined = f"{title} {slug} {' '.join(tags)}"

    if "michigan" not in combined:
        return False
    if any(bad in combined for bad in EXCLUDED_TERMS):
        return False
    if end_date and not (SEASON_START <= end_date <= SEASON_END):
        return False

    bball_signals = ["wolverines", "basketball", "ncaab", "cbb", "march madness", "big ten"]
    return any(sig in combined for sig in bball_signals)


def is_single_game_moneyline(market):
    """Returns True only for full-game win/loss markets."""
    question = market.get("question", "")
    q_lower = question.lower()

    if any(bad in q_lower for bad in EXCLUDED_TERMS):
        return False
    if any(prop in q_lower for prop in NON_MONEYLINE_TERMS):
        return False

    if " vs. " not in q_lower and " v. " not in q_lower and " vs " not in q_lower:
        return False
    if "michigan" not in q_lower:
        return False

    raw_outcomes = market.get("outcomes", "[]")
    try:
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
    except Exception:
        outcomes = []

    if not isinstance(outcomes, list) or len(outcomes) != 2:
        return False

    lower_outcomes = [str(o).strip().lower() for o in outcomes]
    if set(lower_outcomes) & {"yes", "no", "over", "under"}:
        return False

    return any("michigan" in o for o in lower_outcomes)


def fetch_michigan_25_26_games():
    """Fetches only the 37 single-game win/loss markets for Michigan's 2025-2026 season."""
    matched_markets = []
    seen_conditions = set()

    print(f"Querying {GAMMA_BASE_URL} for 2025-2026 Michigan Men's Basketball single-game markets...")

    for query in ["Michigan", "Michigan Wolverines"]:
        page = 1
        while page <= 20:
            params = {
                "q": query,
                "events_status": "closed",
                "page": page
            }
            data = safe_get_json(f"{GAMMA_BASE_URL}/public-search", params=params)
            events = data.get("events", []) if isinstance(data, dict) else []
            if not events:
                break

            for event in events:
                if is_michigan_25_26_mens_bball(event):
                    title = event.get("title", "")
                    for market in event.get("markets", []):
                        if not is_single_game_moneyline(market):
                            continue
                        cid = market.get("conditionId")
                        if cid and cid not in seen_conditions:
                            seen_conditions.add(cid)
                            matched_markets.append({
                                "event_title": title,
                                "market_question": market.get("question"),
                                "end_date": event.get("endDate"),
                                "conditionId": cid
                            })
            page += 1
            time.sleep(0.15)

    matched_markets.sort(key=lambda x: x.get("end_date") or "")
    return matched_markets


def fetch_trades_for_market(condition_id):
    """Paginates through Data API v2 /v2/trades using cursor pagination."""
    all_trades = []
    cursor = None

    while True:
        params = {
            "condition": condition_id,
            "limit": 500
        }
        if cursor:
            params["cursor"] = cursor

        payload = safe_get_json(DATA_API_V2_URL, params=params)
        trades = payload.get("data") or []
        all_trades.extend(trades)

        pagination = payload.get("pagination") or {}
        cursor = pagination.get("next_cursor")
        if not cursor or not pagination.get("has_more"):
            break

        time.sleep(0.15)

    return all_trades


def main():
    markets = fetch_michigan_25_26_games()
    print(f"Found {len(markets)} Michigan 2025-2026 single-game win/loss markets.\n")

    if not markets:
        print("No markets found.")
        return

    all_records = []
    for idx, m in enumerate(markets, start=1):
        game_date = (m["end_date"] or "")[:10]
        print(f"[{idx}/{len(markets)}] ({game_date}) {m['market_question']}")
        try:
            trades = fetch_trades_for_market(m["conditionId"])
            print(f"  -> Retrieved {len(trades)} trades")
            for t in trades:
                t["event_title"] = m["event_title"]
                t["market_question"] = m["market_question"]
                t["event_end_date"] = m["end_date"]
                t["conditionId"] = m["conditionId"]
            all_records.extend(trades)
        except Exception as e:
            print(f"  -> Error fetching {m['conditionId']}: {e}")

    if all_records:
        df = pd.DataFrame(all_records)
        if "timestamp" in df.columns:
            df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")

        output_file = "michigan_2025_2026_moneyline_trades.csv"
        df.to_csv(output_file, index=False)
        print(f"\nDone! Saved {len(df)} total trades across {len(markets)} games to {output_file}")
    else:
        print("\nNo trades returned for the matched markets.")


if __name__ == "__main__":
    main()
