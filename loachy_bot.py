"""
╔══════════════════════════════════════════════════════════════╗
║           LOACHY BOY'S SPORTS BETTING ENGINE  v3            ║
║  Stats model → AI confirmation → Human-in-the-loop          ║
║  The loach sees value where bookies don't.                   ║
║  Part of Jacob's Money Farm.                                 ║
╚══════════════════════════════════════════════════════════════╝

FIXES vs v2:
  - Wallet history shows portfolio value (cash + staked), not raw cash
    so open bets no longer look like losses on the chart
  - streak_mgr / clv_pct resolved from local scope, not dir() hack
  - sport_key set correctly on fallback h2h candidates
  - settlement win_prob based on ai_confidence, not loose edge formula
  - same-game rescan protection across scans (persisted in state)
  - daily_loss_cap fixed — same $20 cap all day, not recalculated
  - open bets correctly excluded from wallet display (shows deployed capital)
  - _place_bet works in both paper and live mode (live just logs intent)
  - CLV uses last recorded odds from line tracker as closing proxy (not placed odds)
  - wallet_history writes portfolio_value, not raw wallet.cash
"""

import os, csv, json, logging, time, sys, random
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from dotenv import load_dotenv
load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
CONFIG = {
    "dry_run":      os.getenv("DRY_RUN", "true").lower() != "false",
    "odds_api_key": os.getenv("ODDS_API_KEY", ""),

    "sports": [
        "americanfootball_nfl",
        "americanfootball_ncaaf",
        "basketball_nba",
        "basketball_ncaab",
        "baseball_mlb",
        "icehockey_nhl",
        "soccer_epl",
        "soccer_usa_mls",
        "mma_mixed_martial_arts",
    ],
    "markets": ["h2h", "spreads", "totals"],

    # Confidence tiers
    "auto_bet_confidence":    0.65,
    "pending_min_confidence": 0.50,
    "longshot_threshold":    -104,   # odds > -104 = longshot → always pending
    "min_odds_american":     -280,
    "max_odds_american":     +200,
    "min_edge":               0.01,

    # Risk
    "paper_budget":    50.0,
    "max_bet_size":     8.0,
    "min_bet_size":     1.0,
    "max_open_bets":    10,
    "daily_loss_cap":  20.0,   # fixed — never shrinks
    "max_bets_per_day": 10,

    # Kelly
    "kelly_fraction": 0.25,
    "sport_kelly": {
        "basketball_nba":         0.30,
        "basketball_ncaab":       0.25,
        "icehockey_nhl":          0.22,
        "baseball_mlb":           0.25,
        "americanfootball_nfl":   0.20,
        "americanfootball_ncaaf": 0.18,
        "mma_mixed_martial_arts": 0.12,
        "soccer_epl":             0.20,
        "soccer_usa_mls":         0.15,
    },

    # Timing
    "scan_interval_sec":   3600,
    "pending_expiry_mins":   30,
    "restart_cooldown_mins": 10,
    "overnight_skip_start":   0,   # UTC hour
    "overnight_skip_end":     8,

    # Streak management
    "streak_reduce_after":  3,
    "streak_reduce_factor": 0.5,

    # Odds history
    "line_move_threshold": 8,

    # Files
    "log_file":          "loachy.log",
    "csv_file":          "loachy_trades.csv",
    "state_file":        "loachy_state.json",
    "dashboard_file":    "loachy_data.json",
    "pending_file":      "loachy_pending.json",
    "sports_config_file":"loachy_sports_config.json",
    "odds_history_file": "loachy_odds_history.json",
    "clv_log_file":      "loachy_clv.json",

    "ai_model": "claude-sonnet-4-20250514",
    "odds_regions": "us",
}

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CONFIG["log_file"], encoding="utf-8")]
)
log = logging.getLogger("loachy")
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(console)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ══════════════════════════════════════════════════════════════════════════════
# PERSONALITY
# ══════════════════════════════════════════════════════════════════════════════
QUOTES = {
    "hunting": [
        '"Vegas left money on the table. Loachy found it."',
        '"That line is cooked. We are ALL over this."',
        '"The public is wrong. The public is always wrong. Loachy knows."',
        '"This edge doesn\'t last. Loachy moves fast."',
    ],
    "watching": [
        '"I see something brewing in the spreads... almost..."',
        '"The fish doesn\'t rush. The fish waits. Then strikes."',
        '"Public hammering the favourite again. Predictable. Watching."',
        '"Line movement detected. Loachy is investigating."',
    ],
    "sleeping": [
        '"No edges above threshold. Bankroll preserved."',
        '"Bad lines everywhere today. Loachy doesn\'t chase."',
        '"Zero bets today = zero losses today. That\'s called discipline."',
    ],
    "pending": [
        '"Found something. Not sure enough to pull the trigger. Your call."',
        '"Borderline edge. Loachy won\'t bet it alone. You in or out?"',
        '"Could be value. Could be a trap. Loachy defers to management."',
    ],
}
ART = {
    "hunting":  ["  ><(((°>\n  !!!!!!", "  ><{{{°>\n  STRIKE"],
    "watching": ["  ><(((º>\n  ~ ~ ~ ~", "  >~~(((º>\n   hmm..."],
    "sleeping": ["  ><(((- )\n   z z z ", "  ><(((*  \n  zzz..."],
    "pending":  ["  ><(((º?\n   ? ? ? ?", "  ><(((·>\n   hmm???"],
}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: WALLET (single source of truth)
# ══════════════════════════════════════════════════════════════════════════════
class Wallet:
    """
    Clean accounting:
      place_bet(bet)       → cash -= bet_size
      settle_bet(id, won)  → cash += bet_size + pnl
      daily_pnl tracks REALIZED pnl only (settled bets)
      portfolio_value = cash + all staked amounts (open bets never look like losses)
    """

    def __init__(self):
        self.cash           = CONFIG["paper_budget"]
        self.total_pnl      = 0.0
        self.daily_pnl      = 0.0
        self.bets_today     = 0
        self.last_date      = datetime.now(ET).date().isoformat()
        self.open_bets      = {}
        self.bet_history    = []
        self.wallet_history = []
        self.seen_game_ids  = set()   # cross-scan dupe protection

    def reset_daily_if_needed(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_date:
            self.daily_pnl  = 0.0
            self.bets_today = 0
            self.last_date  = today
            log.info("[WALLET] Daily reset")

    def can_bet(self, game_id):
        self.reset_daily_if_needed()
        if self.daily_pnl        <= -CONFIG["daily_loss_cap"]:  return False, "Daily loss cap hit"
        if len(self.open_bets)   >= CONFIG["max_open_bets"]:    return False, "Max open bets"
        if self.bets_today       >= CONFIG["max_bets_per_day"]: return False, "Max bets today"
        if self.cash             < CONFIG["min_bet_size"]:      return False, "Wallet too low"
        if game_id in self.open_bets:                           return False, "Already bet on game"
        return True, "OK"

    def place(self, bet):
        self.cash       -= bet["bet_size"]
        self.bets_today += 1
        self.open_bets[bet["game_id"]] = bet
        self.seen_game_ids.add(bet["game_id"])

    def settle(self, game_id, won):
        bet = self.open_bets.pop(game_id, None)
        if not bet:
            return None
        size  = bet["bet_size"]
        price = bet["best_price"]
        # Payout = profit only (not stake). Stake returned separately.
        payout = size * price / 100 if price > 0 else size * 100 / abs(price)
        pnl    = round(payout if won else -size, 4)
        self.cash       += size + pnl   # return stake + pnl
        self.total_pnl  += pnl
        self.daily_pnl  += pnl
        entry = {**bet, "pnl": pnl, "won": won,
                 "settled_at": datetime.now(ET).isoformat()}
        self.bet_history.append(entry)
        return entry

    def portfolio_value(self):
        """Cash + staked capital. Open bets don't look like losses on chart."""
        staked = sum(b["bet_size"] for b in self.open_bets.values())
        return round(self.cash + staked, 2)

    def win_rate(self):
        if not self.bet_history: return 0.0
        return round(sum(1 for b in self.bet_history if b.get("won")) / len(self.bet_history) * 100, 1)

    def roi(self):
        staked = sum(b.get("bet_size", 0) for b in self.bet_history)
        return round(self.total_pnl / staked * 100, 2) if staked else 0.0

    def current_loss_streak(self):
        streak = 0
        for b in reversed(self.bet_history):
            if not b.get("won"):
                streak += 1
            else:
                break
        return streak

    def kelly_multiplier(self):
        streak = self.current_loss_streak()
        if streak >= CONFIG["streak_reduce_after"]:
            f = CONFIG["streak_reduce_factor"]
            log.info(f"  [STREAK] {streak} loss streak — Kelly x{f}")
            return f
        return 1.0

    def status(self):
        return (f"Cash: ${self.cash:.2f} | Portfolio: ${self.portfolio_value():.2f} | "
                f"P&L: ${self.total_pnl:+.2f} | Daily: ${self.daily_pnl:+.2f} | "
                f"Open: {len(self.open_bets)} | Win: {self.win_rate():.0f}%")

    def to_dict(self):
        return {
            "cash":           round(self.cash, 4),
            "total_pnl":      round(self.total_pnl, 4),
            "daily_pnl":      round(self.daily_pnl, 4),
            "bets_today":     self.bets_today,
            "last_date":      self.last_date,
            "open_bets":      self.open_bets,
            "bet_history":    self.bet_history[-500:],
            "wallet_history": self.wallet_history[-500:],
            "seen_game_ids":  list(self.seen_game_ids),
        }

    def load_dict(self, d):
        self.cash           = d.get("cash", CONFIG["paper_budget"])
        self.total_pnl      = d.get("total_pnl", 0.0)
        self.daily_pnl      = d.get("daily_pnl", 0.0)
        self.bets_today     = d.get("bets_today", 0)
        self.last_date      = d.get("last_date", datetime.now(ET).date().isoformat())
        self.open_bets      = d.get("open_bets", {})
        self.bet_history    = d.get("bet_history", [])
        self.wallet_history = d.get("wallet_history", [])
        self.seen_game_ids  = set(d.get("seen_game_ids", []))


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2: STATE
# ══════════════════════════════════════════════════════════════════════════════
class State:
    @staticmethod
    def save(wallet):
        try:
            d = wallet.to_dict()
            d["last_stop"] = datetime.now(ET).isoformat()
            with open(CONFIG["state_file"], "w") as f:
                json.dump(d, f, indent=2)
        except Exception as e:
            log.warning(f"State save failed: {e}")

    @staticmethod
    def load():
        try:
            with open(CONFIG["state_file"]) as f:
                return json.load(f)
        except:
            return None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: ODDS FETCHER
# ══════════════════════════════════════════════════════════════════════════════
class OddsFetcher:
    BASE_URL = "https://api.the-odds-api.com/v4/sports"
    _cache   = {}
    CACHE_SECS = 1500   # 25 min
    MAX_LIVE_CALLS = 1  # live API calls per scan

    PRIORITY = [
        "basketball_ncaab", "basketball_nba", "icehockey_nhl",
        "baseball_mlb", "americanfootball_nfl", "americanfootball_ncaaf",
        "mma_mixed_martial_arts", "soccer_epl", "soccer_usa_mls",
    ]

    def _api_remaining(self):
        return getattr(self, "_remaining", "?")

    def fetch_sport(self, sport_key):
        if sport_key in self._cache:
            ts, data = self._cache[sport_key]
            if time.time() - ts < self.CACHE_SECS:
                log.info(f"  [{sport_key}] cached ({int((time.time()-ts)/60)}min)")
                return data, True
        if not CONFIG["odds_api_key"]:
            return self._mock(sport_key), True
        try:
            r = requests.get(
                f"{self.BASE_URL}/{sport_key}/odds",
                params={"apiKey": CONFIG["odds_api_key"],
                        "regions": CONFIG["odds_regions"],
                        "markets": ",".join(CONFIG["markets"]),
                        "oddsFormat": "american", "dateFormat": "iso"},
                timeout=10)
            self._remaining = r.headers.get("x-requests-remaining", "?")
            log.info(f"  [{sport_key}] fetched | remaining: {self._remaining}")
            if r.status_code == 401: log.error("Bad ODDS_API_KEY"); return [], False
            if r.status_code == 422: return [], False
            if r.status_code == 429:
                log.warning("Rate limit — waiting 60s"); time.sleep(60); return [], False
            r.raise_for_status()
            data = r.json()
            self._cache[sport_key] = (time.time(), data)
            return data, False
        except Exception as e:
            log.debug(f"Odds fetch failed {sport_key}: {e}")
            return [], False

    def _load_enabled_sports(self):
        try:
            with open(CONFIG["sports_config_file"]) as f:
                d = json.load(f)
            enabled = [s for s, on in d.items() if on]
            if enabled:
                return [s for s in self.PRIORITY if s in enabled]
        except:
            pass
        return self.PRIORITY

    def is_overnight(self, force=False):
        if force:
            return False
        h = datetime.utcnow().hour
        return CONFIG["overnight_skip_start"] <= h < CONFIG["overnight_skip_end"]

    def fetch_all(self, force=False):
        if self.is_overnight(force):
            log.info(f"  [OVERNIGHT] {datetime.utcnow().hour:02d}:00 UTC — skipping scan")
            return [], self._api_remaining()

        active = self._load_enabled_sports()
        log.info(f"  Sports: {', '.join(active)}")

        all_games  = []
        live_calls = 0
        for sport in active:
            games, cached = self.fetch_sport(sport)
            if not cached:
                live_calls += 1
            for g in games:
                g["sport_key"] = sport
            all_games.extend(games)
            if not cached:
                time.sleep(0.3)
            if live_calls >= self.MAX_LIVE_CALLS:
                # Use cache only for remaining sports
                for s in active[active.index(sport)+1:]:
                    if s in self._cache:
                        c, _ = self.fetch_sport(s)
                        for g in c:
                            g["sport_key"] = s
                        all_games.extend(c)
                break

        log.info(f"  Total: {len(all_games)} games | Live calls: {live_calls} | Credits: {self._api_remaining()}")
        return all_games, self._api_remaining()

    def _mock(self, sport_key):
        if "ncaab" not in sport_key:
            return []
        return [{
            "id": "mock_ncaab_001", "sport_key": sport_key,
            "sport_title": "NCAAB",
            "commence_time": (datetime.now(ET) + timedelta(hours=4)).isoformat() + "Z",
            "home_team": "Duke Blue Devils", "away_team": "UNC Tar Heels",
            "bookmakers": [
                {"key": "draftkings", "title": "DraftKings", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Duke Blue Devils", "price": -130},
                        {"name": "UNC Tar Heels",   "price": +110},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Duke Blue Devils", "price": -110, "point": -2.5},
                        {"name": "UNC Tar Heels",   "price": -110, "point":  2.5},
                    ]},
                ]},
                {"key": "fanduel", "title": "FanDuel", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Duke Blue Devils", "price": -145},
                        {"name": "UNC Tar Heels",   "price": +122},
                    ]},
                ]},
            ]
        }]


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: STATISTICAL MODEL
# ══════════════════════════════════════════════════════════════════════════════
class StatModel:

    @staticmethod
    def to_prob(odds):
        return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)

    def analyse_game(self, game):
        bookmakers = game.get("bookmakers", [])
        if not bookmakers:
            return []

        home  = game.get("home_team", "Home")
        away  = game.get("away_team", "Away")
        sport = game.get("sport_title", game.get("sport_key", ""))
        sport_key = game.get("sport_key", "")
        commence  = game.get("commence_time", "")

        try:
            game_dt     = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            hours_until = (game_dt.replace(tzinfo=None) - datetime.utcnow()).total_seconds() / 3600
            if hours_until < 0.5 or hours_until > 48:
                return []
        except:
            hours_until = 12

        # Aggregate odds across bookmakers
        market_odds = {}
        for bookie in bookmakers:
            for market in bookie.get("markets", []):
                mk = market["key"]
                if mk not in market_odds:
                    market_odds[mk] = {}
                for outcome in market.get("outcomes", []):
                    name  = outcome["name"]
                    price = outcome["price"]
                    point = outcome.get("point")
                    key   = f"{name}_{point}" if point is not None else name
                    if key not in market_odds[mk]:
                        market_odds[mk][key] = {"prices": [], "name": name, "point": point}
                    market_odds[mk][key]["prices"].append(price)

        candidates = []
        for mk, outcomes in market_odds.items():
            for oc in outcomes.values():
                if not oc["prices"]:
                    continue
                best_price     = max(oc["prices"])
                consensus_prob = sum(self.to_prob(p) for p in oc["prices"]) / len(oc["prices"])
                best_prob      = self.to_prob(best_price)
                edge           = consensus_prob - best_prob

                if not (CONFIG["min_odds_american"] <= best_price <= CONFIG["max_odds_american"]):
                    continue
                if edge < CONFIG["min_edge"]:
                    continue

                dec  = best_price / 100 + 1 if best_price > 0 else 100 / abs(best_price) + 1
                b    = dec - 1
                p    = consensus_prob
                kf   = CONFIG["sport_kelly"].get(sport_key, CONFIG["kelly_fraction"])
                kelly = max(0, (b * p - (1-p)) / b * kf) if b > 0 else 0
                size  = round(min(max(kelly * CONFIG["paper_budget"],
                                     CONFIG["min_bet_size"]),
                                  CONFIG["max_bet_size"]), 2)

                candidates.append({
                    "game_id":        game["id"],
                    "sport":          sport,
                    "sport_key":      sport_key,
                    "home":           home,
                    "away":           away,
                    "market":         mk,
                    "outcome_name":   oc["name"],
                    "point":          oc["point"],
                    "best_price":     best_price,
                    "consensus_prob": round(consensus_prob, 4),
                    "best_prob":      round(best_prob, 4),
                    "edge":           round(edge, 4),
                    "bet_size":       size,
                    "hours_until":    round(hours_until, 1),
                    "commence_time":  commence,
                    "book_count":     len(oc["prices"]),
                })

        return sorted(candidates, key=lambda x: x["edge"], reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 5: AI CONFIRMER
# ══════════════════════════════════════════════════════════════════════════════
class AIConfirmer:
    def confirm(self, candidate, injury_ctx=None, weather_ctx=None, line_ctx=None):
        if not os.getenv("ANTHROPIC_API_KEY"):
            return True, 0.70, "No API key — using model signal"
        try:
            import anthropic
            client = anthropic.Anthropic()

            mk = candidate["market"]
            pt = candidate.get("point")
            if mk == "h2h":
                market_label = "moneyline — pick who wins"
            elif mk == "spreads" and pt is not None:
                market_label = f"spread: {candidate['outcome_name']} {pt:+.1f}"
            elif mk == "totals" and pt is not None:
                market_label = f"total: {candidate['outcome_name']} {pt}"
            else:
                market_label = mk

            is_longshot = candidate.get("best_price", 0) > CONFIG["longshot_threshold"]
            tier_note = (
                "LONGSHOT bet. Only flag if you see a genuine upset opportunity."
                if is_longshot else
                "FAVOURITE bet. Only agree if one team is clearly dominant."
            )

            extra = []
            if injury_ctx:  extra.append(f"INJURIES: {injury_ctx}")
            if weather_ctx: extra.append(f"WEATHER: {weather_ctx}")
            if line_ctx:    extra.append(f"LINE MOVEMENT: {line_ctx}")
            extra_str = ("\n\nContext:\n" + "\n".join(extra)) if extra else ""

            prompt = f"""You are Loachy Boy — a disciplined sports bettor who grinds favourites and only takes longshots when the edge is real.

Game: {candidate['away']} @ {candidate['home']}
Sport: {candidate['sport']}
Bet: {market_label}
Odds: {candidate['best_price']:+d}
Hours until game: {candidate['hours_until']:.1f}
Book implied probability: {candidate['consensus_prob']*100:.1f}%{extra_str}

{tier_note}

For FAVOURITES: agree when one team is clearly superior — form, depth, home advantage, no key injuries.
For LONGSHOTS: agree only on genuine upset spots — key injury on favourite, hostile environment, recent head-to-head edge.

Factor in injuries and weather if provided. Be honest — low confidence if genuinely unsure.

JSON only: {{"agree": true, "confidence": 0.80, "reasoning": "Max 2 sentences."}}"""

            msg = client.messages.create(
                model=CONFIG["ai_model"], max_tokens=250,
                messages=[{"role": "user", "content": prompt}])
            raw = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
            r   = json.loads(raw)
            return r.get("agree", False), r.get("confidence", 0.5), r.get("reasoning", "")
        except Exception as e:
            log.warning(f"AI confirm failed: {e}")
            return False, 0.0, f"AI error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 6: INJURY FEED
# ══════════════════════════════════════════════════════════════════════════════
class InjuryFeed:
    ESPN = {
        "basketball_nba":       "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
        "americanfootball_nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
        "baseball_mlb":         "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
        "icehockey_nhl":        "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries",
        "basketball_ncaab":     "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/injuries",
    }
    _cache     = {}
    CACHE_SECS = 3600

    def fetch(self, sport_key):
        url = self.ESPN.get(sport_key)
        if not url:
            return {}
        if sport_key in self._cache:
            ts, data = self._cache[sport_key]
            if time.time() - ts < self.CACHE_SECS:
                return data
        try:
            r = requests.get(url, timeout=6)
            r.raise_for_status()
            injuries = {}
            for item in r.json().get("injuries", []):
                team   = item.get("team", {}).get("displayName", "")
                player = item.get("athlete", {}).get("displayName", "Unknown")
                status = item.get("status", "Questionable")
                pos    = item.get("athlete", {}).get("position", {}).get("abbreviation", "")
                injuries.setdefault(team, []).append(
                    {"player": player, "status": status, "pos": pos})
            self._cache[sport_key] = (time.time(), injuries)
            return injuries
        except Exception as e:
            log.debug(f"Injury fetch failed {sport_key}: {e}")
            return {}

    def get_context(self, candidate):
        injuries = self.fetch(candidate.get("sport_key", ""))
        if not injuries:
            return None, False
        home_inj = injuries.get(candidate.get("home", ""), [])
        away_inj = injuries.get(candidate.get("away", ""), [])
        star_pos = {"QB","PG","SG","SF","PF","C","SP","ACE"}
        fav      = candidate.get("outcome_name", "")
        fav_inj  = home_inj if fav == candidate.get("home") else away_inj
        star_out = any(i["status"] in ("Out","Doubtful") and i["pos"].upper() in star_pos
                       for i in fav_inj)
        parts = []
        if home_inj:
            parts.append(candidate.get("home","") + ": " +
                         ", ".join(f"{i['player']} ({i['status']})" for i in home_inj[:3]))
        if away_inj:
            parts.append(candidate.get("away","") + ": " +
                         ", ".join(f"{i['player']} ({i['status']})" for i in away_inj[:3]))
        return (" | ".join(parts) if parts else None), star_out


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 7: LINE TRACKER
# ══════════════════════════════════════════════════════════════════════════════
class LineTracker:
    def __init__(self):
        self.history = self._load()

    def _load(self):
        try:
            with open(CONFIG["odds_history_file"]) as f:
                return json.load(f)
        except:
            return {}

    def _save(self):
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
            for gid in list(self.history.keys()):
                for oc in self.history[gid]:
                    self.history[gid][oc] = [
                        e for e in self.history[gid][oc] if e["ts"] > cutoff]
                if not any(self.history[gid].values()):
                    del self.history[gid]
            with open(CONFIG["odds_history_file"], "w") as f:
                json.dump(self.history, f)
        except Exception as e:
            log.debug(f"LineTracker save: {e}")

    def record(self, game_id, outcome, odds):
        self.history.setdefault(game_id, {}).setdefault(outcome, []).append(
            {"odds": odds, "ts": datetime.utcnow().isoformat()})
        self._save()

    def get_movement(self, game_id, outcome):
        entries = self.history.get(game_id, {}).get(outcome, [])
        if len(entries) < 2:
            return None, None, None, False
        opening = entries[0]["odds"]
        current = entries[-1]["odds"]
        move    = current - opening
        sharp   = abs(move) >= CONFIG["line_move_threshold"]
        return opening, current, "shortening" if move < 0 else "drifting", sharp

    def get_latest_odds(self, game_id, outcome):
        """Return most recently recorded odds — used as closing line proxy for CLV."""
        entries = self.history.get(game_id, {}).get(outcome, [])
        return entries[-1]["odds"] if entries else None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 8: WEATHER
# ══════════════════════════════════════════════════════════════════════════════
class WeatherChecker:
    OUTDOOR = {"americanfootball_nfl", "baseball_mlb", "soccer_usa_mls", "soccer_epl"}
    COORDS  = {"default": (39.8, -98.6)}

    def get_conditions(self, candidate):
        if candidate.get("sport_key", "") not in self.OUTDOOR:
            return None
        lat, lon = self.COORDS["default"]
        try:
            game_dt  = datetime.fromisoformat(
                candidate.get("commence_time", "").replace("Z", ""))
            date_str = game_dt.strftime("%Y-%m-%d")
            r = requests.get("https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon,
                        "daily": "precipitation_sum,windspeed_10m_max",
                        "timezone": "UTC",
                        "start_date": date_str, "end_date": date_str},
                timeout=6)
            r.raise_for_status()
            d      = r.json().get("daily", {})
            precip = (d.get("precipitation_sum") or [0])[0] or 0
            wind   = (d.get("windspeed_10m_max")  or [0])[0] or 0
            parts  = []
            if wind > 30:    parts.append(f"HIGH WIND {wind:.0f}km/h")
            elif wind > 20:  parts.append(f"moderate wind {wind:.0f}km/h")
            if precip > 5:   parts.append(f"HEAVY RAIN {precip:.0f}mm")
            elif precip > 1: parts.append(f"light rain {precip:.0f}mm")
            return " | ".join(parts) if parts else None
        except Exception as e:
            log.debug(f"Weather: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 9: PENDING MANAGER
# ══════════════════════════════════════════════════════════════════════════════
class PendingManager:

    def load(self):
        try:
            with open(CONFIG["pending_file"]) as f:
                return json.load(f)
        except:
            return {"pending": [], "approved": []}

    def save(self, data):
        try:
            with open(CONFIG["pending_file"], "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning(f"Pending save: {e}")

    def add(self, candidate, confidence, reasoning):
        data = self.load()
        if any(p["game_id"] == candidate["game_id"] for p in data["pending"]):
            return
        data["pending"].append({
            **candidate,
            "ai_confidence": round(confidence, 2),
            "ai_reasoning":  reasoning,
            "pending_since": datetime.now(ET).isoformat(),
            "expires_at":    (datetime.now(ET) + timedelta(
                              minutes=CONFIG["pending_expiry_mins"])).isoformat(),
            "status": "PENDING",
        })
        self.save(data)
        log.info(f"  [PENDING] {candidate['away']} @ {candidate['home']} | {confidence:.0%}")

    def get_approved(self):
        data     = self.load()
        approved = [p for p in data.get("pending",   []) if p.get("status") == "APPROVED"]
        approved += [p for p in data.get("approved", []) if p.get("status") == "APPROVED"]
        data["approved"] = []
        data["pending"]  = [p for p in data.get("pending", [])
                            if p.get("status") != "APPROVED"]
        self.save(data)
        if approved:
            log.info(f"  [PENDING] {len(approved)} approved")
        return approved

    def expire_old(self):
        data   = self.load()
        now    = datetime.now(ET)
        before = len(data["pending"])
        data["pending"] = [p for p in data["pending"]
                           if datetime.fromisoformat(p["expires_at"]) > now]
        removed = before - len(data["pending"])
        if removed:
            log.info(f"  [PENDING] {removed} expired")
            self.save(data)
        return data["pending"]


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 10: PARLAY BUILDER
# ══════════════════════════════════════════════════════════════════════════════
class ParlayBuilder:
    MIN_CONF    = 0.70
    MAX_PARLAYS = 2

    @staticmethod
    def _dec(odds):
        return odds / 100 + 1 if odds > 0 else 100 / abs(odds) + 1

    @staticmethod
    def _am(dec):
        return int((dec-1)*100) if dec >= 2 else int(-100/(dec-1))

    def build(self, confirmed):
        eligible = [c for c in confirmed
                    if c.get("ai_confirmed")
                    and c.get("ai_confidence", 0) >= self.MIN_CONF
                    and not c.get("longshot", False)
                    and c.get("market") in ("h2h", "spreads")]
        if len(eligible) < 2:
            return []
        eligible.sort(key=lambda x: x["ai_confidence"], reverse=True)
        parlays, used = [], set()
        for i in range(len(eligible)):
            for j in range(i+1, len(eligible)):
                if len(parlays) >= self.MAX_PARLAYS:
                    break
                l1, l2 = eligible[i], eligible[j]
                if l1["game_id"] == l2["game_id"]:
                    continue
                key = tuple(sorted([l1["game_id"], l2["game_id"]]))
                if key in used:
                    continue
                used.add(key)
                d1, d2   = self._dec(l1["best_price"]), self._dec(l2["best_price"])
                par_dec  = d1 * d2
                true_prob = l1["consensus_prob"] * l2["consensus_prob"]
                edge      = round(true_prob - 1/par_dec, 4)
                size      = max(round(min(l1["bet_size"], l2["bet_size"]) * 0.5, 2),
                                CONFIG["min_bet_size"])
                parlays.append({
                    "parlay_id":        f"par_{l1['game_id'][:8]}_{l2['game_id'][:8]}",
                    "type":             "2-leg parlay",
                    "legs": [
                        {"game_id": l1["game_id"], "sport": l1["sport"],
                         "matchup": f"{l1['away']} @ {l1['home']}",
                         "outcome": l1["outcome_name"], "odds": l1["best_price"],
                         "confidence": l1["ai_confidence"],
                         "reasoning": l1.get("ai_reasoning","")},
                        {"game_id": l2["game_id"], "sport": l2["sport"],
                         "matchup": f"{l2['away']} @ {l2['home']}",
                         "outcome": l2["outcome_name"], "odds": l2["best_price"],
                         "confidence": l2["ai_confidence"],
                         "reasoning": l2.get("ai_reasoning","")},
                    ],
                    "parlay_odds":      self._am(par_dec),
                    "parlay_decimal":   round(par_dec, 3),
                    "true_prob":        round(true_prob, 4),
                    "implied_prob":     round(1/par_dec, 4),
                    "edge":             edge,
                    "combined_conf":    round(l1["ai_confidence"] * l2["ai_confidence"], 3),
                    "bet_size":         size,
                    "potential_payout": round(size * (par_dec - 1), 2),
                    "status":           "PENDING",
                    "pending_since":    datetime.now(ET).isoformat(),
                    "expires_at":       (datetime.now(ET) + timedelta(
                                        minutes=CONFIG["pending_expiry_mins"])).isoformat(),
                })
        return parlays


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BOT
# ══════════════════════════════════════════════════════════════════════════════
class LoachyBot:

    def __init__(self):
        self.fetcher  = OddsFetcher()
        self.model    = StatModel()
        self.ai       = AIConfirmer()
        self.injuries = InjuryFeed()
        self.lines    = LineTracker()
        self.weather  = WeatherChecker()
        self.wallet   = Wallet()
        self.pending  = PendingManager()
        self.parlays  = ParlayBuilder()

        self.scan_count     = 0
        self.ai_calls       = 0
        self.next_scan_at   = None
        self._cur_parlays   = []

        self._load_state()

        try:
            with open(CONFIG["csv_file"], "x", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp","game_id","sport","matchup","market","outcome",
                    "odds","edge_pct","ai_confidence","bet_size","pnl","won","source","status"
                ])
        except FileExistsError:
            pass

    def _load_state(self):
        d = State.load()
        if not d:
            log.info("[INIT] Fresh start — $%.2f", CONFIG["paper_budget"])
            return
        self.wallet.load_dict(d)
        log.info("[INIT] Loaded | cash=$%.2f | portfolio=$%.2f | open=%d",
                 self.wallet.cash, self.wallet.portfolio_value(),
                 len(self.wallet.open_bets))

    def _log_csv(self, bet, pnl=None, won=None, source="AUTO"):
        try:
            matchup = f"{bet.get('away','')} @ {bet.get('home','')}"
            with open(CONFIG["csv_file"], "a", newline="") as f:
                csv.writer(f).writerow([
                    datetime.now(ET).isoformat(),
                    bet.get("game_id","")[:16],
                    bet.get("sport",""), matchup, bet.get("market",""),
                    bet.get("outcome_name",""), bet.get("best_price",""),
                    round(bet.get("edge",0)*100, 2),
                    round(bet.get("ai_confidence",0), 2),
                    bet.get("bet_size",0),
                    pnl if pnl is not None else "",
                    won if won is not None else "",
                    source,
                    "PAPER" if CONFIG["dry_run"] else "LIVE",
                ])
        except Exception as e:
            log.debug(f"CSV: {e}")

    def _place_bet(self, bet, source="AUTO"):
        ok, reason = self.wallet.can_bet(bet["game_id"])
        if not ok:
            log.info(f"  Blocked: {reason}")
            return False

        bet["placed_at"] = datetime.now(ET).isoformat()
        bet["source"]    = source
        bet["status"]    = "OPEN"

        self.wallet.place(bet)
        State.save(self.wallet)
        self._log_csv(bet, source=source)

        log.info("  [%s/%s] BET %s %s %+d | $%.2f | Edge %.1f%% | conf %.0f%%",
                 "PAPER" if CONFIG["dry_run"] else "LIVE", source,
                 bet.get("outcome_name",""), bet.get("market",""),
                 bet.get("best_price",0), bet.get("bet_size",0),
                 bet.get("edge",0)*100, bet.get("ai_confidence",0)*100)
        return True

    def _fetch_scores(self, sport_key):
        """Fetch completed scores from Odds API for a given sport."""
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/",
                params={
                    "apiKey":    CONFIG["odds_api_key"],
                    "daysFrom":  3,   # games completed in last 3 days
                    "dateFormat": "iso",
                },
                timeout=10
            )
            if r.status_code != 200:
                log.debug(f"[SCORES] {sport_key} HTTP {r.status_code}")
                return []
            return r.json()
        except Exception as e:
            log.debug(f"[SCORES] fetch error: {e}")
            return []

    def _determine_winner(self, bet, scores_by_id):
        """
        Given a bet and a dict of {game_id: score_object}, determine if bet won.
        Returns True (won), False (lost), or None (can't determine yet).

        Handles h2h, spreads, and totals markets.
        """
        game_id     = bet.get("game_id", "")
        market      = bet.get("market", "h2h")
        outcome     = bet.get("outcome_name", "")
        point       = bet.get("point", None)
        home        = bet.get("home", "")
        away        = bet.get("away", "")

        score_obj = scores_by_id.get(game_id)
        if not score_obj:
            return None  # game not in scores yet

        # Must be completed
        if not score_obj.get("completed"):
            return None

        scores = score_obj.get("scores") or []
        score_map = {s["name"]: int(s["score"]) for s in scores if s.get("score") is not None}

        if len(score_map) < 2:
            return None  # scores missing

        home_score = score_map.get(home)
        away_score = score_map.get(away)

        # Try fuzzy match if exact names don't match (Odds API sometimes abbreviates)
        if home_score is None or away_score is None:
            names = list(score_map.keys())
            if len(names) == 2:
                # Assign by position: first entry is usually away, second is home
                away_score = score_map[names[0]]
                home_score = score_map[names[1]]

        if home_score is None or away_score is None:
            log.debug(f"[SCORES] Can't match scores for {away} @ {home}: {score_map}")
            return None

        if market == "h2h":
            # outcome_name is the team name we bet on
            if outcome == home:
                return home_score > away_score
            elif outcome == away:
                return away_score > home_score
            else:
                # fuzzy: check if outcome contains home or away team name
                if home.lower() in outcome.lower():
                    return home_score > away_score
                elif away.lower() in outcome.lower():
                    return away_score > home_score
                return None

        elif market == "spreads":
            if point is None:
                return None
            # outcome_name is the team + spread, e.g. "Lakers -3.5"
            # determine which team we backed
            if home.lower() in outcome.lower():
                covered = (home_score + point) > away_score
                return covered
            elif away.lower() in outcome.lower():
                covered = (away_score + point) > home_score
                return covered
            return None

        elif market == "totals":
            total = home_score + away_score
            if point is None:
                return None
            if "over" in outcome.lower():
                return total > point
            elif "under" in outcome.lower():
                return total < point
            return None

        return None

    # Cache scores per scan to avoid re-fetching for every bet in same sport
    _scores_cache = {}

    def _settle_old_bets(self):
        """
        Settle bets where game started 3+ hours ago using REAL scores
        from The Odds API. Falls back to logging UNRESOLVED if scores
        aren't available yet (never uses fake dice rolls).
        """
        # Group bets by sport so we only fetch each sport's scores once
        bets_by_sport = {}
        for game_id, bet in list(self.wallet.open_bets.items()):
            try:
                game_time   = datetime.fromisoformat(
                    bet["commence_time"].replace("Z", "+00:00"))
                hours_since = (datetime.utcnow() - game_time.replace(tzinfo=None)
                               ).total_seconds() / 3600
                if hours_since < 3:
                    continue
            except:
                continue
            sport_key = bet.get("sport_key", "")
            if sport_key not in bets_by_sport:
                bets_by_sport[sport_key] = []
            bets_by_sport[sport_key].append((game_id, bet))

        if not bets_by_sport:
            return

        # Fetch scores for each sport needed
        for sport_key, bets in bets_by_sport.items():
            scores = self._fetch_scores(sport_key)
            scores_by_id = {s["id"]: s for s in scores}

            for game_id, bet in bets:
                won = self._determine_winner(bet, scores_by_id)

                if won is None:
                    # Game not completed yet or scores missing — skip, check next scan
                    log.info("  [SCORES] %s @ %s — result not available yet, waiting...",
                             bet.get("away",""), bet.get("home",""))
                    continue

                settled = self.wallet.settle(game_id, won)
                if settled:
                    State.save(self.wallet)
                    sign = "+" if settled["pnl"] >= 0 else ""
                    log.info("  [SETTLED/REAL] %s @ %s — %s | P&L: %s$%.2f | Cash: $%.2f",
                             bet.get("away",""), bet.get("home",""),
                             "WON ✓" if won else "LOST ✗",
                             sign, settled["pnl"], self.wallet.cash)
                    self._log_csv(bet, pnl=settled["pnl"], won=won)

                # CLV: compare placed odds vs latest recorded odds (closing proxy)
                latest = self.lines.get_latest_odds(game_id, bet.get("outcome_name",""))
                if latest and latest != bet.get("best_price"):
                    placed  = bet.get("best_price", 0)
                    clv_pts = placed - latest
                    beat    = clv_pts < 0  # we got shorter (better) odds
                    log.info("  [CLV] Placed %+d | Close ~%+d | CLV %+d pts %s",
                             placed, latest, clv_pts, "✓ BEAT" if beat else "✗ MISSED")
                    try:
                        try:
                            with open(CONFIG["clv_log_file"]) as f:
                                clv = json.load(f)
                        except:
                            clv = []
                        clv.append({
                            "game_id":      game_id,
                            "matchup":      f"{bet.get('away','')} @ {bet.get('home','')}",
                            "placed_odds":  placed,
                            "closing_odds": latest,
                            "clv_points":   clv_pts,
                            "clv_positive": beat,
                            "logged_at":    datetime.now(ET).isoformat(),
                        })
                        with open(CONFIG["clv_log_file"], "w") as f:
                            json.dump(clv[-200:], f, indent=2)
                    except Exception as e:
                        log.debug(f"CLV log: {e}")

    def _clv_summary(self):
        try:
            with open(CONFIG["clv_log_file"]) as f:
                data = json.load(f)
            if not data:
                return None
            return round(sum(1 for e in data if e.get("clv_positive")) / len(data) * 100, 1)
        except:
            return None

    def _write_dashboard(self, candidates, mood, pending_bets, api_remaining):
        try:
            # Portfolio value = cash + staked (open bets never look like losses)
            portfolio = self.wallet.portfolio_value()
            self.wallet.wallet_history.append({
                "t": datetime.now(ET).isoformat(),
                "v": portfolio,
            })
            if len(self.wallet.wallet_history) > 500:
                self.wallet.wallet_history = self.wallet.wallet_history[-500:]

            loss_streak = self.wallet.current_loss_streak()
            clv_pct     = self._clv_summary()

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "lastScan":       datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":           "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":           mood,
                    "quote":          random.choice(QUOTES.get(mood, QUOTES["watching"])),
                    "art":            random.choice(ART.get(mood, ART["watching"])),
                    "wallet":         round(self.wallet.cash, 2),
                    "portfolioValue": portfolio,
                    "stakedValue":    round(portfolio - self.wallet.cash, 2),
                    "startingBudget": CONFIG["paper_budget"],
                    "totalPnl":       round(self.wallet.total_pnl, 4),
                    "dailyPnl":       round(self.wallet.daily_pnl, 4),
                    "winRate":        self.wallet.win_rate(),
                    "roi":            self.wallet.roi(),
                    "betsTotal":      len(self.wallet.bet_history),
                    "openBets":       len(self.wallet.open_bets),
                    "scanCount":      self.scan_count,
                    "gamesScanned":   len(candidates),
                    "apiRemaining":   api_remaining,
                    "nextScanAt":     self.next_scan_at,
                    "aiCallsTotal":   self.ai_calls,
                    "aiCostEstimate": round(self.ai_calls * 0.003, 4),
                    "pendingCount":   len(pending_bets),
                    "lossStreak":     loss_streak,
                    "clvPct":         clv_pct,
                    "candidates":     candidates[:10],
                    "pendingBets":    pending_bets,
                    "openBetsList":   list(self.wallet.open_bets.values()),
                    "recentBets":     self.wallet.bet_history[-15:],
                    "suggestedParlays": self._cur_parlays,
                    "walletHistory":  self.wallet.wallet_history[-500:],
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Dashboard: {e}")

    def run_once(self, force=False):
        self.scan_count += 1
        self.wallet.reset_daily_if_needed()

        log.info("=" * 60)
        log.info("  LOACHY v3 #%d | %s | %s",
                 self.scan_count,
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"))
        log.info("  %s", self.wallet.status())
        log.info("=" * 60)

        # ── Step 1: Settle old bets ──
        self._settle_old_bets()

        # ── Step 2: Process approved pending ──
        for bet in self.pending.get_approved():
            log.info(f"  [HUMAN APPROVED] {bet.get('away','')} @ {bet.get('home','')}")
            self._place_bet(bet, source="HUMAN")

        # ── Step 3: Expire stale pending ──
        current_pending  = self.pending.expire_old()
        pending_game_ids = {p["game_id"] for p in current_pending}

        # ── Step 4: Fetch odds ──
        games, api_remaining = self.fetcher.fetch_all(force=force)

        # ── Step 5: Build candidates ──
        all_candidates = []
        for game in games:
            all_candidates.extend(self.model.analyse_game(game))

        # Best candidate per game (highest edge)
        best_per_game = {}
        for c in all_candidates:
            gid = c["game_id"]
            if gid not in best_per_game or c["edge"] > best_per_game[gid]["edge"]:
                best_per_game[gid] = c

        # Fallback: add basic h2h entry for games with no stat edge
        stat_ids = set(best_per_game.keys())
        for game in games:
            gid = game.get("id")
            if gid in stat_ids:
                continue
            sport_key = game.get("sport_key", "")
            for bookie in game.get("bookmakers", []):
                for market in bookie.get("markets", []):
                    if market["key"] != "h2h":
                        continue
                    for outcome in market.get("outcomes", []):
                        price = outcome.get("price", 0)
                        if not (CONFIG["min_odds_american"] <= price <= CONFIG["max_odds_american"]):
                            continue
                        try:
                            game_dt = datetime.fromisoformat(
                                game.get("commence_time","").replace("Z","+00:00"))
                            hrs = (game_dt.replace(tzinfo=None)-datetime.utcnow()).total_seconds()/3600
                            if hrs < 0.5 or hrs > 48:
                                continue
                        except:
                            hrs = 12
                        prob = StatModel.to_prob(price)
                        best_per_game[gid] = {
                            "game_id":        gid,
                            "sport":          game.get("sport_title", sport_key),
                            "sport_key":      sport_key,   # ← fixed: was missing
                            "home":           game.get("home_team",""),
                            "away":           game.get("away_team",""),
                            "market":         "h2h",
                            "outcome_name":   outcome["name"],
                            "point":          None,
                            "best_price":     price,
                            "consensus_prob": round(prob, 4),
                            "best_prob":      round(prob, 4),
                            "edge":           0.0,
                            "bet_size":       CONFIG["min_bet_size"],
                            "hours_until":    round(hrs, 1),
                            "commence_time":  game.get("commence_time",""),
                            "book_count":     1,
                        }
                        break
                    break

        top = sorted(best_per_game.values(), key=lambda x: x["edge"], reverse=True)[:8]
        log.info("  %d games queued for AI | %d had stat edge", len(top), len(all_candidates))

        # ── Step 6: AI evaluation ──
        kelly_mult        = self.wallet.kelly_multiplier()
        bets_placed       = 0
        pending_added     = 0
        confirmed_list    = []

        # Record odds for line movement
        for c in top:
            self.lines.record(c["game_id"], c["outcome_name"], c["best_price"])

        for c in top:
            ok, reason = self.wallet.can_bet(c["game_id"])
            if not ok:
                log.info(f"  Blocked: {reason}")
                continue

            # Skip games we've already bet on (cross-scan protection)
            if c["game_id"] in self.wallet.seen_game_ids:
                log.info(f"  Skip (already bet): {c['away']} @ {c['home']}")
                continue

            # Apply streak Kelly multiplier
            if kelly_mult < 1.0:
                c["bet_size"] = max(round(c["bet_size"] * kelly_mult, 2),
                                    CONFIG["min_bet_size"])

            # Gather context
            injury_ctx, star_out = self.injuries.get_context(c)
            weather_ctx          = self.weather.get_conditions(c)
            opening, current, direction, sharp = self.lines.get_movement(
                c["game_id"], c["outcome_name"])
            line_ctx = None
            if opening is not None:
                line_ctx = f"Line moved {opening:+d} → {current:+d} ({direction})"
                if sharp:
                    line_ctx += " — SHARP MONEY SIGNAL"

            if injury_ctx:  log.info(f"  [INJ]     {injury_ctx[:100]}")
            if weather_ctx: log.info(f"  [WEATHER] {weather_ctx}")
            if line_ctx:    log.info(f"  [LINE]    {line_ctx}")

            c.update({
                "injury_context":  injury_ctx,
                "weather_context": weather_ctx,
                "line_context":    line_ctx,
                "star_out":        star_out,
                "sharp_money":     sharp,
            })

            log.info("  AI: %s @ %s | %s %+d",
                     c["away"], c["home"], c["outcome_name"], c["best_price"])
            confirmed, confidence, reasoning = self.ai.confirm(
                c, injury_ctx, weather_ctx, line_ctx)
            self.ai_calls += 1

            c["ai_confidence"] = round(confidence, 2)
            c["ai_reasoning"]  = reasoning
            c["ai_confirmed"]  = confirmed
            confirmed_list.append(c)

            is_longshot = c["best_price"] > CONFIG["longshot_threshold"]
            c["longshot"] = is_longshot

            if is_longshot and confirmed and confidence >= CONFIG["pending_min_confidence"]:
                if c["game_id"] not in pending_game_ids:
                    log.info(f"  LONGSHOT → PENDING ({confidence:.0%}): {reasoning[:80]}")
                    self.pending.add(c, confidence, reasoning)
                    pending_game_ids.add(c["game_id"])
                    pending_added += 1

            elif not is_longshot and confirmed and confidence >= CONFIG["auto_bet_confidence"]:
                log.info(f"  AUTO BET ({confidence:.0%}): {reasoning[:80]}")
                placed = self._place_bet({
                    **c, "ai_confidence": confidence, "ai_reasoning": reasoning,
                    "status": "OPEN",
                }, source="AUTO")
                if placed:
                    bets_placed += 1

            elif confidence >= CONFIG["pending_min_confidence"] and \
                    c["game_id"] not in pending_game_ids:
                log.info(f"  BORDERLINE → PENDING ({confidence:.0%}): {reasoning[:80]}")
                self.pending.add(c, confidence, reasoning)
                pending_game_ids.add(c["game_id"])
                pending_added += 1

            else:
                log.info(f"  SKIP ({confidence:.0%}): {reasoning[:80]}")

        # ── Step 7: Parlay suggestions ──
        self._cur_parlays = self.parlays.build(confirmed_list)
        for par in self._cur_parlays:
            log.info("  [PARLAY] %s + %s | %+d | payout $%.2f",
                     par["legs"][0]["outcome"], par["legs"][1]["outcome"],
                     par["parlay_odds"], par["potential_payout"])

        # ── Step 8: Mood + dashboard ──
        current_pending = self.pending.load().get("pending", [])
        if bets_placed > 0:  mood = "hunting"
        elif pending_added:  mood = "pending"
        elif top:            mood = "watching"
        else:                mood = "sleeping"

        log.info("\nDone | games=%d | auto=%d | pending=%d | parlays=%d | %s\n",
                 len(top), bets_placed, pending_added, len(self._cur_parlays),
                 self.wallet.status())

        self._write_dashboard(confirmed_list or top, mood, current_pending, api_remaining)

    def _approval_watcher(self):
        """Background thread — checks for approved bets every 60s. Zero API credits."""
        log.info("[WATCHER] Approval watcher started — checking every 60s")
        while not self._stop_watcher.is_set():
            try:
                approved = self.pending.get_approved()
                for bet in approved:
                    log.info(f"[WATCHER] Placing approved: {bet.get('away','')} @ {bet.get('home','')}")
                    self._place_bet(bet, source="HUMAN")
                    current_pending = self.pending.load().get("pending", [])
                    self._write_dashboard([], "hunting", current_pending, "?")
            except Exception as e:
                log.debug(f"[WATCHER] {e}")
            self._stop_watcher.wait(60)

    def run_loop(self):
        import threading
        log.info("Loachy v3 | %s | $%.0f | %d sports",
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 CONFIG["paper_budget"], len(CONFIG["sports"]))

        # Start approval watcher (zero API credits — reads local JSON only)
        self._stop_watcher = threading.Event()
        threading.Thread(target=self._approval_watcher, daemon=True).start()

        # Restart cooldown
        d = State.load()
        if d:
            last_stop = d.get("last_stop")
            if last_stop:
                mins_ago = (datetime.now(ET) - datetime.fromisoformat(last_stop)
                            ).total_seconds() / 60
                cooldown = CONFIG["restart_cooldown_mins"]
                if mins_ago < cooldown:
                    wait = int((cooldown - mins_ago) * 60)
                    log.info(f"  [COOLDOWN] Restarted {mins_ago:.1f}min ago — waiting {wait}s")
                    self.next_scan_at = (datetime.now(ET) + timedelta(seconds=wait)).isoformat()
                    time.sleep(wait)

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("Loachy rests. Goodbye.")
                self._stop_watcher.set()
                break
            except Exception as e:
                log.error("Error: %s", e, exc_info=True)

            interval = CONFIG["scan_interval_sec"]
            self.next_scan_at = (datetime.now(ET) + timedelta(seconds=interval)).isoformat()
            log.info(f"Sleeping {interval}s...\n")
            time.sleep(interval)


if __name__ == "__main__":
    bot = LoachyBot()
    if "--once" in sys.argv:
        bot.run_once(force="--force" in sys.argv)
    else:
        bot.run_loop()
