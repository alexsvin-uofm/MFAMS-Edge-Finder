"""
Edge Finder - collector v2

Pulls one game from Kalshi and Polymarket, converts both into the SAME record
(Quote), saves them to SQLite, and runs a fee-aware arbitrage check.

Run:  python3 collector.py
"""
import json
import math
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# ---- Settings: change these to look at a different game -------------------
KALSHI_EVENT = "KXNFLGAME-26NOV09BUFMIN"   # from the Kalshi game URL
POLY_SLUG = "nfl-buf-min-2026-11-10"       # from the Polymarket search output

# YOU confirm the match by hand: same team, named differently on each site.
TEAM_A_KALSHI = "Buffalo"
TEAM_A_POLY = "Bills"

# Polymarket fee: PLACEHOLDER. Check Polymarket's fee docs and the fee fields
# printed below, then correct this. Here: fee = rate * price * contracts.
POLY_FEE_RATE_ASSUMED = 0.0075

# Optional: your own probability that team A wins (e.g. 0.60). None = skip.
MY_PROB_A = None

ORDER_SIZES = [1, 10, 100]                 # contract counts to test
DB_PATH = Path(__file__).resolve().parent / "data" / "edge.db"
# ---------------------------------------------------------------------------


# ------------------------------ shared record -------------------------------
@dataclass
class Quote:
    """One team's prices on one platform. All prices in dollars (0.00-1.00)."""
    platform: str
    market_id: str
    team: str
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    yes_ask_size: Optional[float]   # contracts available at yes_ask
    no_ask_size: Optional[float]    # contracts available at no_ask
    fetched_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(url, **params):
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def as_list(x):
    # Polymarket sometimes returns lists as JSON-encoded strings
    return json.loads(x) if isinstance(x, str) else x


def best_high(levels):
    """Highest-priced level -> (price, size), or (None, None)."""
    return max(levels) if levels else (None, None)


def best_low(levels):
    """Lowest-priced level -> (price, size), or (None, None)."""
    return min(levels) if levels else (None, None)


# --------------------------------- KALSHI -----------------------------------
def kalshi_book(ticker):
    """Return (yes_bids, no_bids) as lists of (price_dollars, size)."""
    data = get(f"{KALSHI}/markets/{ticker}/orderbook")
    if "orderbook_fp" in data:
        ob = data["orderbook_fp"]
        yes = [(float(p), float(q)) for p, q in (ob.get("yes_dollars") or [])]
        no = [(float(p), float(q)) for p, q in (ob.get("no_dollars") or [])]
    elif "orderbook" in data:
        ob = data["orderbook"]  # older format: cents
        yes = [(p / 100, q) for p, q in (ob.get("yes") or [])]
        no = [(p / 100, q) for p, q in (ob.get("no") or [])]
    else:
        print("Unexpected Kalshi response keys:", list(data.keys()))
        return [], []
    return yes, no


def kalshi_quotes(event_ticker) -> Tuple[List[Quote], dict]:
    markets = get(f"{KALSHI}/markets", event_ticker=event_ticker,
                  limit=100)["markets"]
    quotes, rules = [], {}
    for m in markets:
        yes_bids, no_bids = kalshi_book(m["ticker"])
        yb, yb_size = best_high(yes_bids)   # best price someone pays for YES
        nb, nb_size = best_high(no_bids)    # best price someone pays for NO
        # Kalshi shows bids only: to BUY yes you hit the best NO bid, and
        # YES ask = 1 - best NO bid (and vice versa).
        quotes.append(Quote(
            platform="kalshi",
            market_id=m["ticker"],
            team=m.get("yes_sub_title", ""),
            yes_bid=yb,
            yes_ask=round(1 - nb, 4) if nb is not None else None,
            no_bid=nb,
            no_ask=round(1 - yb, 4) if yb is not None else None,
            yes_ask_size=nb_size,
            no_ask_size=yb_size,
            fetched_at=now_iso(),
        ))
        rules[m.get("yes_sub_title", "")] = {
            "rules": m.get("rules_primary"),
            "close_time": m.get("close_time"),
        }
    return quotes, rules


# ------------------------------- POLYMARKET ---------------------------------
def poly_book(token_id):
    """Return (bids, asks) as lists of (price_dollars, size)."""
    book = get(f"{CLOB}/book", token_id=token_id)
    bids = [(float(b["price"]), float(b["size"])) for b in book["bids"]]
    asks = [(float(a["price"]), float(a["size"])) for a in book["asks"]]
    return bids, asks  # not assumed sorted; best_high/best_low handle it


def pick_moneyline(event):
    markets = event.get("markets", [])
    for m in markets:
        if m.get("sportsMarketType") == "moneyline":
            return m
    for m in markets:
        if m.get("question") == event.get("title"):
            return m
    return markets[0]


def polymarket_quotes(slug) -> Tuple[List[Quote], dict]:
    data = get(f"{GAMMA}/events", slug=slug)
    event = data[0] if isinstance(data, list) and data else data
    if not event:
        raise SystemExit(f"No Polymarket event found for slug {slug}")
    market = pick_moneyline(event)

    outcomes = as_list(market["outcomes"])
    tokens = as_list(market["clobTokenIds"])
    books = [poly_book(t) for t in tokens]
    fetched = now_iso()

    quotes = []
    for i, team in enumerate(outcomes):
        own_bids, own_asks = books[i]
        other_bids, other_asks = books[1 - i]
        yb, _ = best_high(own_bids)
        ya, ya_size = best_low(own_asks)
        # Buying NO on this team = buying the OTHER outcome's token.
        nb, _ = best_high(other_bids)
        na, na_size = best_low(other_asks)
        quotes.append(Quote("polymarket", tokens[i], team,
                            yb, ya, nb, na, ya_size, na_size, fetched))
    meta = {
        "question": market.get("question"),
        "end": event.get("endDate"),
        "description": market.get("description"),
        "feesEnabled": market.get("feesEnabled"),
        "feeType": market.get("feeType"),
        "feeSchedule": market.get("feeSchedule"),
        "takerBaseFee": market.get("takerBaseFee"),
    }
    return quotes, meta


# ----------------------------------- FEES -----------------------------------
def kalshi_fee(contracts, price):
    """Kalshi taker fee for one order: round up 0.07 * C * P * (1-P) to a cent."""
    cents = 0.07 * contracts * price * (1 - price) * 100
    return math.ceil(cents - 1e-9) / 100


def polymarket_fee(contracts, price):
    """PLACEHOLDER model - see POLY_FEE_RATE_ASSUMED at the top."""
    return round(POLY_FEE_RATE_ASSUMED * contracts * price, 4)


# --------------------------------- ANALYSIS ---------------------------------
def arb_check(label, yes_q, yes_fee, no_q, no_fee):
    """Buy YES on one platform + NO on the other. Top-of-book prices only."""
    print(f"\n  {label}")
    a1, s1 = yes_q.yes_ask, yes_q.yes_ask_size
    a2, s2 = no_q.no_ask, no_q.no_ask_size
    if None in (a1, a2, s1, s2):
        print("    missing price/size, skipping")
        return
    print(f"    YES on {yes_q.platform} @ {a1:.2f} (size {s1:g})  +  "
          f"NO on {no_q.platform} @ {a2:.2f} (size {s2:g})  "
          f"= {a1 + a2:.2f} before fees")
    max_n = int(min(s1, s2))
    sizes = sorted(set([n for n in ORDER_SIZES if n <= max_n] + [max_n]))
    for n in sizes:
        if n < 1:
            continue
        fees = yes_fee(n, a1) + no_fee(n, a2)
        cost = n * (a1 + a2) + fees
        profit = n - cost            # one side pays $1 per pair
        verdict = "PROFIT" if profit > 0 else "no"
        print(f"    n={n:>6}: cost ${cost:9.2f}  fees ${fees:6.2f}  "
              f"locked profit ${profit:9.2f}  -> {verdict}")


def edge_check(prob_a, k_a, p_a, n=100, kelly_mult=0.25):
    """Compare YOUR probability against all-in cost on each venue."""
    print(f"\n  Your P({TEAM_A_KALSHI} wins) = {prob_a:.3f}, size {n}")
    for q, fee in [(k_a, kalshi_fee), (p_a, polymarket_fee)]:
        for side, ask, p in [("YES", q.yes_ask, prob_a),
                             ("NO", q.no_ask, 1 - prob_a)]:
            if ask is None:
                continue
            cost = ask + fee(n, ask) / n          # all-in cost per contract
            edge = p - cost
            kelly = max(0.0, edge / (1 - cost)) * kelly_mult
            print(f"    {q.platform:<10} {side:<3} ask {ask:.2f}  "
                  f"all-in {cost:.4f}  net edge {edge:+.4f}  "
                  f"quarter-Kelly {kelly:.1%} of bankroll")


# ------------------------------------ DB ------------------------------------
def save_quotes(quotes):
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS quotes (
        platform TEXT, market_id TEXT, team TEXT,
        yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
        yes_ask_size REAL, no_ask_size REAL, fetched_at TEXT)""")
    conn.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [tuple(asdict(q).values()) for q in quotes])
    conn.commit()
    conn.close()


# ----------------------------------- MAIN -----------------------------------
def fmt(x):
    return "  -  " if x is None else f"{x:.2f}"


def print_quotes(quotes):
    print(f"  {'platform':<11}{'team':<12}{'YESbid':>7}{'YESask':>7}"
          f"{'NObid':>7}{'NOask':>7}{'size@YESask':>13}{'size@NOask':>12}")
    for q in quotes:
        print(f"  {q.platform:<11}{q.team:<12}{fmt(q.yes_bid):>7}"
              f"{fmt(q.yes_ask):>7}{fmt(q.no_bid):>7}{fmt(q.no_ask):>7}"
              f"{(q.yes_ask_size or 0):>13g}{(q.no_ask_size or 0):>12g}")


def main():
    print("=" * 70)
    print("FETCHING")
    print("=" * 70)
    k_quotes, k_rules = kalshi_quotes(KALSHI_EVENT)
    p_quotes, p_meta = polymarket_quotes(POLY_SLUG)
    print_quotes(k_quotes + p_quotes)

    gap = abs((datetime.fromisoformat(p_quotes[0].fetched_at)
               - datetime.fromisoformat(k_quotes[0].fetched_at)).total_seconds())
    print(f"\n  Kalshi and Polymarket prices were fetched {gap:.1f}s apart.")

    save_quotes(k_quotes + p_quotes)
    print(f"  Saved {len(k_quotes) + len(p_quotes)} quotes to {DB_PATH}")

    print("\n" + "=" * 70)
    print("MATCH REVIEW (read this: are these really the same question?)")
    print("=" * 70)
    k_info = k_rules.get(TEAM_A_KALSHI, {})
    print(f"  Kalshi rules:      {k_info.get('rules')}")
    print(f"  Kalshi close_time: {k_info.get('close_time')}")
    print(f"  Polymarket:        {p_meta['question']}  (ends {p_meta['end']})")
    print(f"  Polymarket rules:  {(p_meta['description'] or '')[:300]}")
    print(f"  Polymarket fee fields: feesEnabled={p_meta['feesEnabled']} "
          f"feeType={p_meta['feeType']} takerBaseFee={p_meta['takerBaseFee']}")
    print(f"                         feeSchedule={p_meta['feeSchedule']}")

    k_a = next((q for q in k_quotes if q.team == TEAM_A_KALSHI), None)
    p_a = next((q for q in p_quotes if q.team == TEAM_A_POLY), None)
    if not k_a or not p_a:
        print("\nCould not find team A on both platforms. Check TEAM_A_* "
              "against the team names in the table above.")
        return

    print("\n" + "=" * 70)
    print(f"ARBITRAGE CHECK  (team A = {TEAM_A_KALSHI}/{TEAM_A_POLY})")
    print("=" * 70)
    arb_check(f"Kalshi YES {TEAM_A_KALSHI} + Polymarket NO {TEAM_A_POLY}",
              k_a, kalshi_fee, p_a, polymarket_fee)
    arb_check(f"Polymarket YES {TEAM_A_POLY} + Kalshi NO {TEAM_A_KALSHI}",
              p_a, polymarket_fee, k_a, kalshi_fee)
    print("\n  (Polymarket fee is a placeholder. Prices are top-of-book only.)")

    if MY_PROB_A is not None:
        print("\n" + "=" * 70)
        print("EDGE CHECK (your estimate vs market)")
        print("=" * 70)
        edge_check(MY_PROB_A, k_a, p_a)

    print("\nDone.")


if __name__ == "__main__":
    main()