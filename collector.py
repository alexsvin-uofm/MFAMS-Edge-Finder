"""
Edge Finder - collector v4

Type a college basketball team -> find its next game on Kalshi and Polymarket
-> print the win percentages side by side, check for arbitrage after fees,
and save the quotes to SQLite.

Polymarket has TWO exchanges, and this reads both:
  * polymarket.com  (global; via gamma-api / clob APIs)
  * polymarket.us   (US exchange; via gateway.polymarket.us)

Run:  python3 collector.py            (asks for team names, one after another)
      python3 collector.py UConn      (one lookup, then exits)

Put summary27.csv (your team list) in the same folder as this file.
"""
import csv
import json
import math
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
US = "https://gateway.polymarket.us"

# ---- Settings -------------------------------------------------------------
KALSHI_SERIES = "KXNCAAMBGAME"             # men's college basketball games
TEAMS_CSV = Path(__file__).resolve().parent / "summary27.csv"
DB_PATH = Path(__file__).resolve().parent / "data" / "edge.db"
ORDER_SIZES = [1, 10, 100, 1000]           # contract counts for the arb check
SHOW_RULES = True                          # print rule texts for review
MATCH_THRESHOLD = 0.85                     # how alike two team names must be
POLY_FEE = {"rate": 0.03}                  # overwritten from the market's feeSchedule
# ---------------------------------------------------------------------------

# Teams whose names differ between sites. Matching also normalises "St." etc.
ALIAS_GROUPS = [
    ["Connecticut", "UConn"],
    ["Pittsburgh", "Pitt"],
    ["Mississippi", "Ole Miss"],
    ["Miami FL", "Miami (FL)", "Miami Florida", "Miami"],
    ["Miami OH", "Miami (OH)", "Miami Ohio"],
    ["N.C. State", "NC State"],
    ["Southern Miss", "Southern Mississippi"],
    ["Louisiana Monroe", "UL Monroe", "ULM"],
    ["Texas A&M Corpus Chris", "Texas A&M Corpus Christi", "Texas A&M-CC"],
    ["UMass", "Massachusetts"],
    ["USC", "Southern California"],
    ["UCF", "Central Florida"],
    ["VCU", "Virginia Commonwealth"],
    ["BYU", "Brigham Young"],
    ["LSU", "Louisiana State"],
    ["UNLV", "Nevada Las Vegas"],
    ["SMU", "Southern Methodist"],
    ["TCU", "Texas Christian"],
    ["Detroit Mercy", "Detroit"],
]
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


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
    yes_ask_size: Optional[float]   # contracts available at yes_ask (None = unknown)
    no_ask_size: Optional[float]    # contracts available at no_ask
    fetched_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(url, **params):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def as_list(x):
    # Polymarket sometimes returns lists as JSON-encoded strings
    return json.loads(x) if isinstance(x, str) else x


def best_high(levels):
    return max(levels) if levels else (None, None)


def best_low(levels):
    return min(levels) if levels else (None, None)


def ask_input(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_choice(prompt, options):
    """Print numbered options; return the chosen index or None."""
    for i, o in enumerate(options, 1):
        print(f"     {i}) {o}")
    ans = ask_input(prompt)
    if ans.isdigit() and 1 <= int(ans) <= len(options):
        return int(ans) - 1
    return None


# ------------------------------ team-name matching ---------------------------
def norm(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[.'\u2019]", "", s).strip()
    s = re.sub(r"\bst$", "state", s)       # "Penn St." -> "penn state"
    s = re.sub(r"^st\b", "saint", s)       # "St. John's" -> "saint johns"
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


ALIAS_ID = {norm(n): i for i, g in enumerate(ALIAS_GROUPS) for n in g}


def match_score(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ia, ib = ALIAS_ID.get(na), ALIAS_ID.get(nb)
    if ia is not None and ia == ib:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# Known school names (aliases now; the CSV team names are added in load_teams).
# Used to strip mascots: "Connecticut Huskies" -> "connecticut".
_VOCAB: List[str] = [norm(n) for g in ALIAS_GROUPS for n in g]


def school_part(name: str) -> str:
    """Longest known school name at the start of `name` (drops the mascot)."""
    n = norm(name)
    toks = n.split()
    best = ""
    for v in _VOCAB:
        vt = v.split()
        if len(vt) <= len(toks) and toks[:len(vt)] == vt and len(v) > len(best):
            best = v
    return best or n


def team_score(a: str, b: str) -> float:
    """Name similarity that ignores mascots ('Arizona Wildcats' ~ 'Arizona')."""
    return max(match_score(a, b), match_score(school_part(a), school_part(b)))


def team_variants(*names) -> List[str]:
    """The names plus every known alias of them."""
    out = []
    for n in names:
        out.append(n)
        i = ALIAS_ID.get(norm(n))
        if i is not None:
            out.extend(ALIAS_GROUPS[i])
    return list(dict.fromkeys(out))


def same_team(variants, name) -> bool:
    return max(team_score(v, name) for v in variants) >= MATCH_THRESHOLD


def mentions(text: str, variants) -> bool:
    """Does `text` (a title, say) contain any of these names as whole words?"""
    t = " " + norm(text) + " "
    return any(" " + norm(v) + " " in t for v in variants if norm(v))


def load_teams() -> List[str]:
    if not TEAMS_CSV.exists():
        print(f"(No team list at {TEAMS_CSV.name}; names will be used as typed.)")
        return []
    with open(TEAMS_CSV, newline="", encoding="utf-8-sig") as f:
        teams = [row["TeamName"] for row in csv.DictReader(f)]
    for t in teams:
        if norm(t) not in _VOCAB:
            _VOCAB.append(norm(t))
    return teams


def resolve_team(text: str, teams: List[str]) -> str:
    """Turn what the user typed into a team name from the CSV."""
    if not teams:
        return text
    exact = [t for t in teams if match_score(text, t) >= 1.0]
    tokens = norm(text).split()
    partial = [t for t in teams if tokens and all(k in norm(t) for k in tokens)]
    spelled_exactly = any(norm(t) == norm(text) for t in teams)
    if len(exact) == 1 and (spelled_exactly or len(partial) <= 1):
        return exact[0]          # e.g. "uconn", "pitt", "Arizona"
    if len(exact) == 1:
        exact = []               # e.g. "miami": several teams fit, so ask
    fuzzy = [t for t in sorted(teams, key=lambda t: -match_score(text, t))[:5]
             if match_score(text, t) >= 0.6]
    cands = exact or partial or fuzzy
    if not cands:
        print(f"  '{text}' isn't in the team list; searching for it as typed.")
        return text
    if len(cands) == 1:
        return cands[0]
    print(f"  '{text}' could be:")
    idx = ask_choice("  Pick a number (or Enter to cancel): ", cands[:10])
    return cands[idx] if idx is not None else ""


# --------------------------------- KALSHI -----------------------------------
def ticker_date(event_ticker: str) -> Optional[date]:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


_KALSHI_GAMES: Dict[str, dict] = {}


def kalshi_games() -> Dict[str, dict]:
    """All open games in the series, grouped by event ticker (cached)."""
    if _KALSHI_GAMES:
        return _KALSHI_GAMES
    print("  Loading open Kalshi games...")
    cursor = None
    while True:
        params = {"status": "open", "limit": 1000, "series_ticker": KALSHI_SERIES}
        if cursor:
            params["cursor"] = cursor
        data = get(f"{KALSHI}/markets", **params)
        for m in data["markets"]:
            et = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
            g = _KALSHI_GAMES.setdefault(
                et, {"date": ticker_date(et), "markets": []})
            g["markets"].append(m)
        cursor = data.get("cursor")
        if not cursor:
            return _KALSHI_GAMES


def find_kalshi_game(variants):
    """Earliest upcoming game with this team -> (date, event_ticker, team_name)."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=1)
    hits = []
    for et, g in kalshi_games().items():
        if g["date"] is None or g["date"] < cutoff:
            continue
        for m in g["markets"]:
            if same_team(variants, m.get("yes_sub_title", "")):
                hits.append((g["date"], et, m["yes_sub_title"]))
                break
    return min(hits) if hits else None


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
        # Kalshi shows bids only: to BUY yes you hit the best NO bid, so
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
        rules[m.get("yes_sub_title", "")] = m.get("rules_primary")
    return quotes, rules


# ----------------------- POLYMARKET GLOBAL (polymarket.com) -------------------
def poly_book(token_id):
    """Return (bids, asks) as lists of (price_dollars, size)."""
    book = get(f"{CLOB}/book", token_id=token_id)
    bids = [(float(b["price"]), float(b["size"])) for b in book["bids"]]
    asks = [(float(a["price"]), float(a["size"])) for a in book["asks"]]
    return bids, asks  # not assumed sorted; best_high/best_low handle it


def pick_moneyline(event):
    markets = event.get("markets") or []
    for m in markets:
        if m.get("sportsMarketType") == "moneyline":
            return m
    for m in markets:
        if m.get("question") == event.get("title"):
            return m
    return markets[0] if markets else None


def poly_event_date(ev) -> Optional[date]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})$", ev.get("slug") or "")
    text = m.group(0) if m else (ev.get("endDate") or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def poly_collect(team_k, opp_k, kdate):
    """Gather candidate events from polymarket.com using several strategies."""
    events, diag = {}, []

    def add(batch):
        n = 0
        for ev in batch or []:
            if isinstance(ev, dict) and ev.get("slug"):
                events.setdefault(ev["slug"], ev)
                n += 1
        return n

    t_names = team_variants(team_k)[:3]
    o_names = team_variants(opp_k)[:2]
    queries = [f"{t} {o}" for t in t_names for o in o_names] + [team_k]
    for q in queries:
        try:
            n = add(get(f"{GAMMA}/public-search", q=q).get("events"))
            diag.append(f"search '{q}': {n} events")
        except requests.RequestException as e:
            diag.append(f"search '{q}': failed ({e})")

    start = f"{kdate - timedelta(days=1)}T00:00:00Z"
    end = f"{kdate + timedelta(days=2)}T00:00:00Z"
    for tag in ("cbb", "college-basketball"):
        try:
            n = add(get(f"{GAMMA}/events", tag_slug=tag, closed="false",
                        end_date_min=start, end_date_max=end, limit=200))
            diag.append(f"tag '{tag}' on those dates: {n} events")
        except requests.RequestException as e:
            diag.append(f"tag '{tag}': failed ({e})")
    return events, diag


def find_poly_game(team_k, opp_k, kdate):
    """Search polymarket.com for team vs opponent -> (matches, near, diag)."""
    t_vars, o_vars = team_variants(team_k), team_variants(opp_k)
    events, diag = poly_collect(team_k, opp_k, kdate)
    matches, near = [], []
    for slug, ev in events.items():
        if ev.get("closed") or ev.get("active") is False:
            continue
        d = poly_event_date(ev)
        if d is not None and abs((d - kdate).days) > 1:
            continue
        market = pick_moneyline(ev)
        if not market or not market.get("outcomes"):
            continue
        outs = as_list(market["outcomes"])
        if len(outs) != 2:
            continue
        direct = same_team(t_vars, outs[0]) and same_team(o_vars, outs[1])
        cross = same_team(t_vars, outs[1]) and same_team(o_vars, outs[0])
        if direct or cross:
            matches.append(ev)
        elif any(same_team(t_vars, o) or same_team(o_vars, o) for o in outs):
            near.append(ev)
    return matches, near, diag


def polymarket_quotes(slug) -> Tuple[List[Quote], dict]:
    data = get(f"{GAMMA}/events", slug=slug)
    event = data[0] if isinstance(data, list) and data else data
    if not event:
        raise ValueError(f"No polymarket.com event found for slug {slug}")
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
        "title": event.get("title"),
        "description": market.get("description"),
        "feesEnabled": market.get("feesEnabled"),
        "feeSchedule": market.get("feeSchedule"),
    }
    return quotes, meta


# ------------------------- POLYMARKET US (polymarket.us) ----------------------
def us_get(path, tries=3):
    """GET from the US gateway, backing off when it rate-limits us (429)."""
    for i in range(tries):
        r = requests.get(US + path, timeout=15)
        if r.status_code == 429:
            time.sleep(1.5 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise requests.HTTPError("429 Too Many Requests (Polymarket US rate limit)")


def amount(x) -> Optional[float]:
    """Polymarket US prices look like {"value": "0.54", "currency": "USD"}."""
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def polymarket_us_quotes(slug, team_names) -> Tuple[List[Quote], dict]:
    """Read one event from the US gateway and the top of book of its markets."""
    data = us_get(f"/v1/events/slug/{slug}")
    ev = data.get("event", data) if isinstance(data, dict) else {}
    rows = []                                   # (title, market slug, bid, ask)
    for m in (ev.get("markets") or [])[:12]:
        ms = m.get("slug")
        if not ms:
            continue
        time.sleep(0.3)                         # the gateway rate-limits quickly
        try:
            bbo = us_get(f"/v1/markets/{ms}/bbo").get("marketData") or {}
            rows.append((m.get("title") or m.get("question") or "", ms,
                         amount(bbo.get("bestBid")), amount(bbo.get("bestAsk"))))
        except requests.RequestException as e:
            rows.append((m.get("title") or "", ms, None, None))
            print(f"    (bbo for {ms} failed: {e})")

    quotes, note = [], None

    # Case A: ONE market for the whole game, titled "A vs B" (what polymarket.us
    # shows for college games). Its bid/ask is for the FIRST-named team A; the
    # other team's prices are the complement (1 - price).
    for title, ms, bid, ask in rows:
        pv = parse_vs(title)
        if not pv or re.search(r"\d", title):          # skip spreads/totals
            continue
        a_name = next((n for n in team_names if team_score(n, pv[0]) >= MATCH_THRESHOLD), None)
        b_name = next((n for n in team_names if team_score(n, pv[1]) >= MATCH_THRESHOLD), None)
        if not a_name or not b_name or a_name == b_name:
            continue
        inv = lambda x: None if x is None else round(1 - x, 4)
        quotes = [
            Quote("polymarket_us", ms, a_name, bid, ask, inv(ask), inv(bid),
                  None, None, now_iso()),
            Quote("polymarket_us", ms, b_name, inv(ask), inv(bid), bid, ask,
                  None, None, now_iso()),
        ]
        note = (f"Polymarket US has one market for this game ('{title}'). I read "
                f"its bid/ask as {a_name}'s price and use 1 - price for "
                f"{b_name}. Check this against the polymarket.us page.")
        break

    # Case B: one market per team, each team's YES price is its own market.
    for name in ([] if quotes else team_names):
        cands = [r for r in rows if r[0] and team_score(name, r[0]) >= MATCH_THRESHOLD]
        if len(cands) != 1:
            break
        title, ms, bid, ask = cands[0]
        quotes.append(Quote(
            "polymarket_us", ms, name, bid, ask,
            round(1 - ask, 4) if ask is not None else None,
            round(1 - bid, 4) if bid is not None else None,
            None, None, now_iso()))
    if len(quotes) != len(team_names):
        quotes = []
    meta = {
        "note": note,
        "title": ev.get("title"),
        "text": " ".join([ev.get("title") or ""] + [r[0] for r in rows]),
        "description": ev.get("description"),
        "rows": rows,
    }
    return quotes, meta


def parse_vs(title: str):
    """'UConn vs Arizona' -> ('UConn', 'Arizona'); None if it isn't that shape."""
    m = re.match(r"^\s*(.+?)\s+(?:vs\.?|at|@)\s+(.+?)\s*$", title or "", re.I)
    return (m.group(1), m.group(2)) if m else None


def slug_from_link(text: str) -> str:
    return text.strip().split("?")[0].rstrip("/").split("/")[-1]


# ----------------------------------- FEES -----------------------------------
def kalshi_fee(contracts, price):
    """Kalshi taker fee for one order: round up 0.07 * C * P * (1-P) to a cent."""
    cents = 0.07 * contracts * price * (1 - price) * 100
    return math.ceil(cents - 1e-9) / 100


def polymarket_fee(contracts, price):
    """Polymarket taker fee: C * feeRate * p * (1 - p). Makers pay nothing.
    (Global formula per Polymarket's docs. Polymarket US fees are NOT verified;
    the same formula is used there as a stand-in.)"""
    return round(contracts * POLY_FEE["rate"] * price * (1 - price), 4)


# --------------------------------- ANALYSIS ---------------------------------
def arb_check(label, yes_q, yes_fee, no_q, no_fee):
    """Buy YES on one platform + NO on the other. Top-of-book prices only."""
    print(f"\n  {label}")
    a1, s1 = yes_q.yes_ask, yes_q.yes_ask_size
    a2, s2 = no_q.no_ask, no_q.no_ask_size
    if a1 is None or a2 is None:
        print("    missing price, skipping")
        return
    known = [x for x in (s1, s2) if x is not None]
    max_n = int(min(known)) if known else ORDER_SIZES[-1]
    note = "" if len(known) == 2 else "   (depth unknown on one side)"
    print(f"    YES on {yes_q.platform} @ {a1:.3f}  +  "
          f"NO on {no_q.platform} @ {a2:.3f}  = {a1 + a2:.3f} before fees{note}")
    sizes = [n for n in ORDER_SIZES if n <= max_n] or [max_n]
    for n in sizes:
        if n < 1:
            continue
        fees = yes_fee(n, a1) + no_fee(n, a2)
        cost = n * (a1 + a2) + fees
        profit = n - cost            # one side pays $1 per pair
        verdict = "PROFIT" if profit > 0 else "no"
        print(f"    n={n:>5}: cost ${cost:9.2f}  fees ${fees:6.2f}  "
              f"locked profit ${profit:8.2f}  -> {verdict}")


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


# --------------------------------- DISPLAY ----------------------------------
def mid(q: Optional[Quote]) -> Optional[float]:
    if q is None:
        return None
    if q.yes_bid is not None and q.yes_ask is not None:
        return (q.yes_bid + q.yes_ask) / 2
    return q.yes_ask if q.yes_ask is not None else q.yes_bid


def cents(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:g}c"


def cell(q: Optional[Quote]) -> str:
    p = mid(q)
    if p is None:
        return "n/a"
    wide = (q.yes_bid is not None and q.yes_ask is not None
            and q.yes_ask - q.yes_bid > 0.101)
    return (f"{p * 100:5.1f}%{'!' if wide else ' '} "
            f"({cents(q.yes_bid)}/{cents(q.yes_ask)})")


def pair_names(k_names, p_names) -> Optional[Dict[str, str]]:
    """Map the two Kalshi team names to the two Polymarket outcome names."""
    if len(k_names) != 2 or len(p_names) != 2:
        return None
    s = team_score
    direct = (s(k_names[0], p_names[0]), s(k_names[1], p_names[1]))
    cross = (s(k_names[0], p_names[1]), s(k_names[1], p_names[0]))
    pairing = 0 if sum(direct) >= sum(cross) else 1
    scores = direct if pairing == 0 else cross
    if max(scores) >= MATCH_THRESHOLD:         # one clear match pins down both
        order = p_names if pairing == 0 else p_names[::-1]
        return {k_names[0]: order[0], k_names[1]: order[1]}
    print("  Couldn't match team names automatically.")
    print(f"  Which Polymarket outcome is '{k_names[0]}'?")
    idx = ask_choice("  Pick a number: ", list(p_names))
    if idx is None:
        return None
    return {k_names[0]: p_names[idx], k_names[1]: p_names[1 - idx]}


def choose_poly_event(matches, near, diag, team_k, opp_k):
    """Return a Polymarket slug (or None) after auto-match / user help."""
    if len(matches) == 1:
        return matches[0]["slug"]
    options = (matches or near)[:8]
    if options:
        print("  Polymarket: " + ("several matches" if matches
                                  else "no exact match; closest events"))
        for i, e in enumerate(options, 1):
            print(f"     {i}) {e.get('title')}   [{e.get('slug')}]")
        ans = ask_input("  Pick a number, paste a Polymarket link, or Enter "
                        "to skip: ")
    else:
        print(f"  Polymarket: couldn't find {team_k} vs {opp_k} automatically.")
        for line in diag:
            print(f"     - {line}")
        ans = ask_input("  Paste the Polymarket link for this game, or Enter "
                        "to skip: ")
    if ans.isdigit() and options and 1 <= int(ans) <= len(options):
        return options[int(ans) - 1]["slug"]
    if ans:
        return slug_from_link(ans)
    return None


# ----------------------------------- MAIN -----------------------------------
def lookup(text: str, teams: List[str]):
    team = resolve_team(text, teams)
    if not team:
        return
    variants = team_variants(text, team)
    print(f"\n  Team: {team}")

    # ---- Kalshi: find the team's next game -------------------------------
    hit = find_kalshi_game(variants)
    if hit is None:
        print("  Kalshi: no open game found for this team.")
        ans = ask_input("  Paste the Kalshi link for the game, or Enter to "
                        "stop: ")
        if not ans:
            return
        event_ticker = slug_from_link(ans).upper()
        kdate = ticker_date(event_ticker) or datetime.now(timezone.utc).date()
    else:
        kdate, event_ticker, _ = hit

    k_quotes, k_rules = kalshi_quotes(event_ticker)
    k_names = [q.team for q in k_quotes]
    k_team = next((n for n in k_names if same_team(variants, n)),
                  k_names[0] if k_names else team)
    k_opp = next((n for n in k_names if n != k_team), "")
    pair_vars = [team_variants(n) for n in k_names]

    # ---- Polymarket: find the same game ------------------------------------
    matches, near, diag = find_poly_game(k_team, k_opp, kdate)
    slug = choose_poly_event(matches, near, diag, k_team, k_opp)

    g_quotes, g_meta, g_pair = [], {}, None     # polymarket.com
    u_quotes, u_meta = [], {}                   # polymarket.us
    if slug:
        try:
            g_quotes, g_meta = polymarket_quotes(slug)
            if not any(same_team(v, q.team) for v in pair_vars for q in g_quotes):
                print(f"  '{slug}' ({g_meta.get('title')}) isn't {k_team} vs "
                      f"{k_opp}; ignoring it. Check the link you pasted.")
                g_quotes, g_meta = [], {}
            else:
                g_pair = pair_names(k_names, [q.team for q in g_quotes])
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  polymarket.com lookup failed for '{slug}': {e}")
        try:
            u_quotes, u_meta = polymarket_us_quotes(slug, k_names)
            if u_meta and not all(mentions(u_meta["text"], v) for v in pair_vars):
                print(f"  polymarket.us '{slug}' ({u_meta.get('title')}) isn't "
                      f"{k_team} vs {k_opp}; ignoring it.")
                u_quotes, u_meta = [], {}
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  polymarket.us lookup failed for '{slug}': {e}")

    # ---- Show everything -------------------------------------------------
    print("\n" + "=" * 78)
    print(f"  {k_team} vs {k_opp}   -   {kdate:%a %b %d}")
    print(f"  Kalshi:     {event_ticker}")
    found = slug if (g_quotes or u_quotes) else None
    print(f"  Polymarket: {found or 'not found'}")
    print("=" * 78)
    print("  Chance to win = midpoint of best bid and ask; (bid/ask) shown\n")
    print(f"  {'':<13}{'KALSHI':<26}{'POLYMARKET .com':<26}POLYMARKET .us")
    for kn in sorted(k_names, key=lambda n: n != k_team):    # your team first
        kq = next(q for q in k_quotes if q.team == kn)
        gq = None
        if g_pair:
            gq = next((q for q in g_quotes if q.team == g_pair.get(kn)), None)
        uq = next((q for q in u_quotes if q.team == kn), None)
        print(f"  {kn:<13}{cell(kq):<26}{cell(gq):<26}{cell(uq)}")
    if any("!" in cell(q) for q in k_quotes + g_quotes + u_quotes):
        print("\n  ! = bid and ask are more than 10c apart, so the midpoint "
              "is a weak estimate.")
    if u_quotes and u_meta.get("note"):
        print(f"\n  Note: {u_meta['note']}")
    if slug and u_meta and not u_quotes:
        print("\n  Polymarket US markets I found (couldn't auto-assign teams):")
        for title, ms, bid, ask in u_meta["rows"]:
            print(f"     {title[:40]:<40} [{ms}]  bid {cents(bid)} / ask {cents(ask)}")

    save_quotes(k_quotes + g_quotes + u_quotes)

    if SHOW_RULES:
        print("\n  RULES CHECK (same question on both?)")
        print(f"   Kalshi:        {k_rules.get(k_team)}")
        if g_meta:
            print(f"   Polymarket:    {(g_meta.get('description') or '')[:400]}")
        elif u_meta:
            print(f"   Polymarket US: {(u_meta.get('description') or '')[:400]}")

    # ---- Arbitrage: against polymarket.us if we have it, else .com ---------
    p_quotes = u_quotes or (g_quotes if g_pair else [])
    if p_quotes:
        if u_quotes:
            names = {kn: kn for kn in k_names}
            venue = "Polymarket US"
        else:
            names = g_pair
            venue = "Polymarket .com"
        sched = (g_meta.get("feeSchedule") or {}) if g_meta else {}
        if g_meta and g_meta.get("feesEnabled") and sched.get("rate") is not None:
            POLY_FEE["rate"] = float(sched["rate"])
        k_a = next(q for q in k_quotes if q.team == k_team)
        p_a = next(q for q in p_quotes if q.team == names[k_team])
        print(f"\n  ARBITRAGE CHECK vs {venue}  (top-of-book prices; fee rate "
              f"{POLY_FEE['rate']}{', US fees unverified' if u_quotes else ''})")
        arb_check(f"Kalshi YES {k_team} + {venue} NO {k_team}",
                  k_a, kalshi_fee, p_a, polymarket_fee)
        arb_check(f"{venue} YES {k_team} + Kalshi NO {k_team}",
                  p_a, polymarket_fee, k_a, kalshi_fee)
    print()


def main():
    print("Edge Finder: next-game odds on Kalshi and Polymarket (.com and .us)")
    teams = load_teams()
    if len(sys.argv) > 1:
        lookup(" ".join(sys.argv[1:]), teams)
        return
    while True:
        text = ask_input("\nTeam name (Enter to quit): ")
        if not text:
            break
        try:
            lookup(text, teams)
        except requests.RequestException as e:
            print(f"  Network/API error: {e}")


if __name__ == "__main__":
    main()