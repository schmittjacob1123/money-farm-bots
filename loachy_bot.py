"""
╔══════════════════════════════════════════════════════════════╗
║           LOACHY BOY'S SPORTS BETTING ENGINE  v4            ║
║  De-vigged edge detection → AI veto → Sharp money follow    ║
║  The loach finds true value. No fake edges, no guessing.    ║
║  Part of Jacob's Money Farm.                                 ║
╚══════════════════════════════════════════════════════════════╝

FIXES vs v3:
  - REAL edge: de-vigged consensus probability vs best available price
    (v3 just compared best vs average vig-inflated price = meaningless)
  - Kelly uses live wallet balance, not fixed starting budget
  - No zero-edge fallback candidates — AI only sees real statistical edges
  - MAX_LIVE_CALLS raised to 4 (was 1) — more fresh data per scan
  - Overnight skip changed to 2–6 UTC (was 0–8, cut NFL/NBA primetime)
  - auto_bet_confidence raised to 0.72 (was 0.65)
  - pending_expiry: 90 min OR game start − 30 min, whichever sooner
    (v3 had 30 min expiry — bets expired before next 60 min scan ran)
  - Weather checker uses real team city coordinates (was Kansas for every game)
  - INDOOR_TEAMS set — skip weather for domed venues entirely
  - min_edge = 0.02 (2% real de-vigged edge required, was 1%)
  - min_book_count = 3 (new gate — need 3 books confirming price)
  - Streak reduce factor 0.70 (was 0.50 — less aggressive bankroll cut)
  - Score settlement: 30-min cache per sport (saves API credits)
  - AI model → claude-haiku for cheaper/faster confirmations
  - AI prompt enriched: true prob %, edge %, book count, sharp signal
  - AI role clarified: veto mechanism, not random confidence generator
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
    "auto_bet_confidence":    0.72,   # was 0.65 — tighter gate
    "pending_min_confidence": 0.55,   # was 0.50
    "longshot_threshold":    -104,    # odds > -104 = longshot → always pending
    "min_odds_american":     -280,
    "max_odds_american":     +200,
    "min_edge":               0.02,   # 2% real de-vigged edge required (was 1%)
    "min_book_count":         3,      # NEW — minimum books confirming price

    # Risk
    "paper_budget":    50.0,
    "max_bet_size":     8.0,
    "min_bet_size":     1.0,
    "max_open_bets":    10,
    "daily_loss_cap":  20.0,
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
    "pending_expiry_mins":   90,    # was 30 — shorter than scan interval!
    "restart_cooldown_mins": 10,
    "overnight_skip_start":   2,    # was 0 — now only 2-6 UTC (9pm-1am ET)
    "overnight_skip_end":     6,    # was 8 — was cutting NFL/NBA primetime

    # Streak management
    "streak_reduce_after":  3,
    "streak_reduce_factor": 0.70,   # was 0.50 — less aggressive

    # Odds history
    "line_move_threshold": 8,

    # Candidates sent to AI per scan
    "max_top_candidates": 10,

    # Files
    "log_file":          "loachy.log",
    "csv_file":          "loachy_trades.csv",
    "state_file":        "loachy_state.json",
    "dashboard_file":    "loachy_data.json",
    "pending_file":      "loachy_pending.json",
    "sports_config_file":"loachy_sports_config.json",
    "odds_history_file": "loachy_odds_history.json",
    "clv_log_file":      "loachy_clv.json",

    "ai_model": "claude-haiku-4-5-20251001",   # cheaper/faster for this task
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
# TEAM COORDINATES (real venues — for accurate weather data)
# ══════════════════════════════════════════════════════════════════════════════
TEAM_COORDS = {
    # NFL outdoor / open-air
    "Buffalo Bills":          (42.77, -78.79),
    "Miami Dolphins":         (25.96, -80.24),
    "New England Patriots":   (42.09, -71.26),
    "New York Jets":          (40.81, -74.07),
    "New York Giants":        (40.81, -74.07),
    "Baltimore Ravens":       (39.28, -76.62),
    "Cincinnati Bengals":     (39.10, -84.52),
    "Cleveland Browns":       (41.51, -81.70),
    "Pittsburgh Steelers":    (40.45, -80.02),
    "Jacksonville Jaguars":   (30.32, -81.64),
    "Tennessee Titans":       (36.17, -86.77),
    "Denver Broncos":         (39.74, -105.02),
    "Kansas City Chiefs":     (39.05, -94.48),
    "Philadelphia Eagles":    (39.90, -75.17),
    "Washington Commanders":  (38.91, -76.86),
    "Chicago Bears":          (41.86, -87.62),
    "Green Bay Packers":      (44.50, -88.06),
    "Carolina Panthers":      (35.23, -80.85),
    "Tampa Bay Buccaneers":   (27.98, -82.50),
    "San Francisco 49ers":    (37.40, -121.97),
    "Seattle Seahawks":       (47.60, -122.33),
    # MLB outdoor
    "Chicago Cubs":           (41.95, -87.66),
    "Colorado Rockies":       (39.76, -104.99),
    "San Francisco Giants":   (37.78, -122.39),
    "Boston Red Sox":         (42.35, -71.10),
    "Pittsburgh Pirates":     (40.45, -80.00),
    "Philadelphia Phillies":  (39.91, -75.17),
    "Cincinnati Reds":        (39.10, -84.51),
    "Cleveland Guardians":    (41.50, -81.69),
    "Baltimore Orioles":      (39.28, -76.62),
    "New York Yankees":       (40.83, -73.93),
    "New York Mets":          (40.76, -73.85),
    "Detroit Tigers":         (42.34, -83.05),
    "Seattle Mariners":       (47.59, -122.33),
    "Los Angeles Dodgers":    (34.07, -118.24),
    "Los Angeles Angels":     (33.80, -117.88),
    "San Diego Padres":       (32.71, -117.16),
    "Washington Nationals":   (38.87, -77.01),
    "Oakland Athletics":      (37.75, -122.20),
    "Kansas City Royals":     (39.05, -94.48),
    "Chicago White Sox":      (41.83, -87.63),
    "St. Louis Cardinals":    (38.62, -90.19),
    "Milwaukee Brewers":      (43.03, -87.97),
    "Atlanta Braves":         (33.89, -84.47),
    "Texas Rangers":          (32.75, -97.08),
    # MLS outdoor
    "Portland Timbers":       (45.52, -122.69),
    "Seattle Sounders FC":    (47.60, -122.33),
    "Vancouver Whitecaps":    (49.28, -123.11),
    "Colorado Rapids":        (39.81, -104.89),
    "New England Revolution": (42.09, -71.26),
    "DC United":              (38.87, -77.01),
    "New York Red Bulls":     (40.74, -74.15),
    "Chicago Fire FC":        (41.86, -87.62),
    "FC Dallas":              (33.15, -96.84),
    "LA Galaxy":              (33.86, -118.26),
    "Los Angeles FC":         (34.01, -118.29),
    "Real Salt Lake":         (40.58, -111.89),
    "San Jose Earthquakes":   (37.35, -121.93),
    "Sporting Kansas City":   (39.12, -94.52),
    # EPL
    "Arsenal":                (51.56, -0.11),
    "Chelsea":                (51.48, -0.19),
    "Liverpool":              (53.43, -2.96),
    "Manchester City":        (53.48, -2.20),
    "Manchester United":      (53.46, -2.29),
    "Tottenham Hotspur":      (51.60, -0.07),
    "Everton":                (53.44, -2.97),
    "Newcastle United":       (54.97, -1.62),
    "Aston Villa":            (52.51, -1.88),
    "West Ham United":        (51.54,  0.02),
    "Brighton":               (50.86, -0.08),
    "Wolverhampton Wanderers":(52.59, -2.13),
    "Crystal Palace":         (51.40, -0.09),
    "Brentford":              (51.49, -0.31),
    "Fulham":                 (51.47, -0.22),
    "Nottingham Forest":      (52.94, -1.13),
    "Bournemouth":            (50.74, -1.84),
    "Burnley":                (53.79, -2.23),
    "Leicester City":         (52.62, -1.14),
    "Ipswich Town":           (52.06,  1.14),
    "Southampton":            (50.91, -1.40),
    "Sheffield United":       (53.37, -1.47),
    "Luton Town":             (51.88, -0.43),
}

# Teams that play indoors — weather is irrelevant, skip API call
INDOOR_TEAMS = {
    "Houston Texans", "Indianapolis Colts", "Las Vegas Raiders",
    "Los Angeles Chargers", "Los Angeles Rams", "Dallas Cowboys",
    "Detroit Lions", "Minnesota Vikings", "Atlanta Falcons",
    "New Orleans Saints", "Arizona Cardinals",
    "Miami Marlins", "Minnesota Twins", "Milwaukee Brewers",
    "Toronto Blue Jays", "Tampa Bay Rays", "Houston Astros",
}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: WALLET
# ══════════════════════════════════════════════════════════════════════════════
class Wallet:
    """
    Single source of truth for bankroll.
      place_bet(bet)       → cash -= bet_size
      settle_bet(id, won)  → cash += bet_size + pnl
      portfolio_value      = cash + staked (open bets never look like losses)
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
        self.seen_game_ids  = set()

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
        payout = size * price / 100 if price > 0 else size * 100 / abs(price)
        pnl    = round(payout if won else -size, 4)
        self.cash       += size + pnl
        self.total_pnl  += pnl
        self.daily_pnl  += pnl
        entry = {**bet, "pnl": pnl, "won": won,
                 "settled_at": datetime.now(ET).isoformat()}
        self.bet_history.append(entry)
        return entry

    def portfolio_value(self):
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
    BASE_URL   = "https://api.the-odds-api.com/v4/sports"
    _cache     = {}
    CACHE_SECS = 1500    # 25 min
    MAX_LIVE_CALLS = 4   # was 1 — now gets 4 fresh sport feeds per scan

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
                ]},
                {"key": "fanduel", "title": "FanDuel", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Duke Blue Devils", "price": -135},
                        {"name": "UNC Tar Heels",   "price": +115},
                    ]},
                ]},
                {"key": "betmgm", "title": "BetMGM", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Duke Blue Devils", "price": -128},
                        {"name": "UNC Tar Heels",   "price": +108},
                    ]},
                ]},
            ]
        }]


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: STATISTICAL MODEL (de-vigged edge detection)
# ══════════════════════════════════════════════════════════════════════════════
class StatModel:

    @staticmethod
    def to_prob(odds):
        """American odds → implied probability."""
        return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)

    def _devige_binary(self, outcomes_dict):
        """
        Additive de-vig for binary markets (exactly 2 outcomes).

        The bookmaker's vig inflates implied probabilities so they sum to > 1.
        By averaging each side's implied probs across books, then normalizing
        to sum to 1.0, we get the market's best estimate of true probability
        without the house margin baked in.

        Returns {outcome_key: true_probability} or {} if not binary.
        """
        if len(outcomes_dict) != 2:
            return {}

        keys = list(outcomes_dict.keys())
        avg_imps = {}
        for key in keys:
            prices = outcomes_dict[key]["prices"]
            if not prices:
                return {}
            avg_imps[key] = sum(self.to_prob(p) for p in prices) / len(prices)

        total = sum(avg_imps.values())
        if total <= 0:
            return {}

        return {k: v / total for k, v in avg_imps.items()}

    def analyse_game(self, game, bankroll):
        """
        Find positive-EV bets in this game.

        Edge = de-vigged true probability − implied probability of best available price.
        Positive edge means we're being offered better odds than the market's true estimate.
        Requires min_edge AND min_book_count to reduce false positives.

        bankroll: current wallet cash (for correct Kelly sizing).
        """
        bookmakers = game.get("bookmakers", [])
        if not bookmakers:
            return []

        home      = game.get("home_team", "Home")
        away      = game.get("away_team", "Away")
        sport     = game.get("sport_title", game.get("sport_key", ""))
        sport_key = game.get("sport_key", "")
        commence  = game.get("commence_time", "")

        try:
            game_dt     = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            hours_until = (game_dt.replace(tzinfo=None) - datetime.utcnow()).total_seconds() / 3600
            if hours_until < 0.5 or hours_until > 48:
                return []
        except:
            hours_until = 12

        # Group prices by market then outcome key (name + point)
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
            # De-vig for binary markets → true probability estimate
            true_probs = self._devige_binary(outcomes)

            for oc_key, oc in outcomes.items():
                if not oc["prices"]:
                    continue

                best_price  = max(oc["prices"])
                book_count  = len(oc["prices"])
                best_prob   = self.to_prob(best_price)

                # De-vigged true probability; fallback to best_prob if not binary
                true_prob = true_probs.get(oc_key, best_prob)

                # Real edge: what we think is true vs what the best price implies
                edge = round(true_prob - best_prob, 4)

                # Hard gates — all must pass
                if not (CONFIG["min_odds_american"] <= best_price <= CONFIG["max_odds_american"]):
                    continue
                if edge < CONFIG["min_edge"]:
                    continue
                if book_count < CONFIG["min_book_count"]:
                    continue

                # Kelly bet sizing using CURRENT WALLET BALANCE (not fixed starting budget)
                dec   = best_price / 100 + 1 if best_price > 0 else 100 / abs(best_price) + 1
                b     = dec - 1
                p     = true_prob
                kf    = CONFIG["sport_kelly"].get(sport_key, CONFIG["kelly_fraction"])
                kelly = max(0, (b * p - (1-p)) / b * kf) if b > 0 else 0
                size  = round(min(max(kelly * bankroll, CONFIG["min_bet_size"]),
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
                    "true_prob":      round(true_prob, 4),
                    "consensus_prob": round(true_prob, 4),   # alias for dashboard compat
                    "best_prob":      round(best_prob, 4),
                    "edge":           edge,
                    "bet_size":       size,
                    "hours_until":    round(hours_until, 1),
                    "commence_time":  commence,
                    "book_count":     book_count,
                })

        return sorted(candidates, key=lambda x: x["edge"], reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 5: AI CONFIRMER (veto mechanism)
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
                "This is a LONGSHOT bet (+odds). Only agree if there is a genuine upset reason."
                if is_longshot else
                "This is a FAVOURITE bet (-odds). Only agree if one side is clearly dominant."
            )

            extra = []
            if injury_ctx:  extra.append(f"INJURIES: {injury_ctx}")
            if weather_ctx: extra.append(f"WEATHER: {weather_ctx}")
            if line_ctx:    extra.append(f"LINE MOVEMENT: {line_ctx}")
            extra_str = ("\n\nContext:\n" + "\n".join(extra)) if extra else ""

            true_prob   = candidate.get("true_prob", candidate.get("consensus_prob", 0))
            edge_pct    = candidate.get("edge", 0) * 100
            book_count  = candidate.get("book_count", "?")

            prompt = f"""You are Loachy Boy — a sharp, disciplined sports bettor who only bets real edges.

Game: {candidate['away']} @ {candidate['home']}
Sport: {candidate['sport']}
Bet: {market_label}
Odds: {candidate['best_price']:+d}
Hours until game: {candidate['hours_until']:.1f}

EDGE ANALYSIS (pre-confirmed by statistical model):
  De-vigged true probability: {true_prob*100:.1f}%
  Best available price implies: {candidate['best_prob']*100:.1f}%
  Edge: +{edge_pct:.1f}% across {book_count} books{extra_str}

{tier_note}

YOUR ROLE IS TO VETO — not to generate confidence randomly.
Agree only when you can cite a SPECIFIC fact supporting this pick.

VETO if:
- Key player is injured or out for OUR pick's team
- Opponent has major home crowd / altitude / weather advantage that hurts our bet
- Our team is on back-to-back games or has heavy recent travel
- Weather context (if provided) undermines our bet type (e.g. high wind kills totals Over)

AGREE if:
- Opponent has a key injury that weakens them
- Our pick has clear recent form advantage (last 5+ games)
- Line is moving in our direction (sharp money signal)
- Home advantage strongly favors our pick

Be calibrated: 0.80 confidence = you believe this wins 80% of the time.

JSON only: {{"agree": true, "confidence": 0.80, "reasoning": "Max 2 sentences citing specific facts."}}"""

            msg = client.messages.create(
                model=CONFIG["ai_model"], max_tokens=200,
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
        entries = self.history.get(game_id, {}).get(outcome, [])
        return entries[-1]["odds"] if entries else None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 8: WEATHER (real team coordinates)
# ══════════════════════════════════════════════════════════════════════════════
class WeatherChecker:
    OUTDOOR = {"americanfootball_nfl", "americanfootball_ncaaf",
               "baseball_mlb", "soccer_usa_mls", "soccer_epl"}

    def get_conditions(self, candidate):
        if candidate.get("sport_key", "") not in self.OUTDOOR:
            return None

        home = candidate.get("home", "")
        away = candidate.get("away", "")

        # Skip if either team plays indoors
        if home in INDOOR_TEAMS or away in INDOOR_TEAMS:
            return None

        # Home team hosts → use home venue coordinates
        coords = TEAM_COORDS.get(home) or TEAM_COORDS.get(away)
        if not coords:
            return None  # Unknown venue — don't send Kansas weather as fake data

        lat, lon = coords
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

        # Expiry = min(now + 90min, game_start - 30min)
        # Never let a pending bet sit through game start
        try:
            game_dt      = datetime.fromisoformat(
                candidate["commence_time"].replace("Z", "+00:00"))
            game_start   = game_dt.astimezone(ET)
            max_expiry   = datetime.now(ET) + timedelta(minutes=CONFIG["pending_expiry_mins"])
            game_cutoff  = game_start - timedelta(minutes=30)
            expires_at   = min(max_expiry, game_cutoff)
            expires_str  = expires_at.isoformat()
        except:
            expires_str = (datetime.now(ET) + timedelta(
                           minutes=CONFIG["pending_expiry_mins"])).isoformat()

        data["pending"].append({
            **candidate,
            "ai_confidence": round(confidence, 2),
            "ai_reasoning":  reasoning,
            "pending_since": datetime.now(ET).isoformat(),
            "expires_at":    expires_str,
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
    MIN_CONF    = 0.72    # raised to match auto_bet_confidence
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
                d1, d2    = self._dec(l1["best_price"]), self._dec(l2["best_price"])
                par_dec   = d1 * d2
                # Use de-vigged true_prob for parlay edge calculation
                true_prob = l1.get("true_prob", l1["consensus_prob"]) * l2.get("true_prob", l2["consensus_prob"])
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

        self.scan_count   = 0
        self.ai_calls     = 0
        self.next_scan_at = None
        self._cur_parlays = []

        # Score settlement cache {sport_key: (timestamp, scores_list)}
        self._scores_cache     = {}
        self._scores_cache_ttl = 1800   # 30 min — saves API credits

        self._load_state()

        try:
            with open(CONFIG["csv_file"], "x", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp","game_id","sport","matchup","market","outcome",
                    "odds","edge_pct","true_prob_pct","ai_confidence","bet_size",
                    "pnl","won","source","status"
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
                    round(bet.get("true_prob", bet.get("consensus_prob",0))*100, 1),
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

        log.info("  [%s/%s] BET %s %s %+d | $%.2f | Edge %.1f%% | true=%.1f%% | conf %.0f%%",
                 "PAPER" if CONFIG["dry_run"] else "LIVE", source,
                 bet.get("outcome_name",""), bet.get("market",""),
                 bet.get("best_price",0), bet.get("bet_size",0),
                 bet.get("edge",0)*100,
                 bet.get("true_prob", bet.get("consensus_prob",0))*100,
                 bet.get("ai_confidence",0)*100)
        return True

    def _fetch_scores(self, sport_key):
        """Fetch completed scores from Odds API. Uses 30-min cache to save credits."""
        now = time.time()
        if sport_key in self._scores_cache:
            ts, data = self._scores_cache[sport_key]
            if now - ts < self._scores_cache_ttl:
                return data
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/",
                params={"apiKey": CONFIG["odds_api_key"], "daysFrom": 3,
                        "dateFormat": "iso"},
                timeout=10)
            if r.status_code != 200:
                log.debug(f"[SCORES] {sport_key} HTTP {r.status_code}")
                return []
            data = r.json()
            self._scores_cache[sport_key] = (now, data)
            return data
        except Exception as e:
            log.debug(f"[SCORES] fetch error: {e}")
            return []

    def _determine_winner(self, bet, scores_by_id):
        game_id  = bet.get("game_id", "")
        market   = bet.get("market", "h2h")
        outcome  = bet.get("outcome_name", "")
        point    = bet.get("point", None)
        home     = bet.get("home", "")
        away     = bet.get("away", "")

        score_obj = scores_by_id.get(game_id)
        if not score_obj or not score_obj.get("completed"):
            return None

        scores    = score_obj.get("scores") or []
        score_map = {s["name"]: int(s["score"]) for s in scores if s.get("score") is not None}

        if len(score_map) < 2:
            return None

        home_score = score_map.get(home)
        away_score = score_map.get(away)

        if home_score is None or away_score is None:
            names = list(score_map.keys())
            if len(names) == 2:
                away_score = score_map[names[0]]
                home_score = score_map[names[1]]

        if home_score is None or away_score is None:
            log.debug(f"[SCORES] Can't match scores for {away} @ {home}: {score_map}")
            return None

        if market == "h2h":
            if outcome == home:
                return home_score > away_score
            elif outcome == away:
                return away_score > home_score
            else:
                if home.lower() in outcome.lower():
                    return home_score > away_score
                elif away.lower() in outcome.lower():
                    return away_score > home_score
                return None

        elif market == "spreads":
            if point is None: return None
            if home.lower() in outcome.lower():
                return (home_score + point) > away_score
            elif away.lower() in outcome.lower():
                return (away_score + point) > home_score
            return None

        elif market == "totals":
            total = home_score + away_score
            if point is None: return None
            if "over" in outcome.lower():  return total > point
            elif "under" in outcome.lower(): return total < point
            return None

        return None

    def _settle_old_bets(self):
        """Settle bets 3+ hours after game start using real Odds API scores."""
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
            bets_by_sport.setdefault(sport_key, []).append((game_id, bet))

        if not bets_by_sport:
            return

        for sport_key, bets in bets_by_sport.items():
            scores = self._fetch_scores(sport_key)
            scores_by_id = {s["id"]: s for s in scores}

            for game_id, bet in bets:
                won = self._determine_winner(bet, scores_by_id)

                if won is None:
                    log.info("  [SCORES] %s @ %s — result not available yet",
                             bet.get("away",""), bet.get("home",""))
                    continue

                settled = self.wallet.settle(game_id, won)
                if settled:
                    State.save(self.wallet)
                    sign = "+" if settled["pnl"] >= 0 else ""
                    log.info("  [SETTLED] %s @ %s — %s | P&L: %s$%.2f | Cash: $%.2f",
                             bet.get("away",""), bet.get("home",""),
                             "WON ✓" if won else "LOST ✗",
                             sign, settled["pnl"], self.wallet.cash)
                    self._log_csv(bet, pnl=settled["pnl"], won=won)

                # CLV tracking
                latest = self.lines.get_latest_odds(game_id, bet.get("outcome_name",""))
                if latest and latest != bet.get("best_price"):
                    placed  = bet.get("best_price", 0)
                    clv_pts = placed - latest
                    beat    = clv_pts < 0
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
            if not data: return None
            return round(sum(1 for e in data if e.get("clv_positive")) / len(data) * 100, 1)
        except:
            return None

    def _write_dashboard(self, candidates, mood, pending_bets, api_remaining):
        try:
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
                    "lastScan":        datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":            "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":            mood,
                    "quote":           random.choice(QUOTES.get(mood, QUOTES["watching"])),
                    "art":             random.choice(ART.get(mood, ART["watching"])),
                    "wallet":          round(self.wallet.cash, 2),
                    "portfolioValue":  portfolio,
                    "stakedValue":     round(portfolio - self.wallet.cash, 2),
                    "startingBudget":  CONFIG["paper_budget"],
                    "totalPnl":        round(self.wallet.total_pnl, 4),
                    "dailyPnl":        round(self.wallet.daily_pnl, 4),
                    "winRate":         self.wallet.win_rate(),
                    "roi":             self.wallet.roi(),
                    "betsTotal":       len(self.wallet.bet_history),
                    "openBets":        len(self.wallet.open_bets),
                    "scanCount":       self.scan_count,
                    "gamesScanned":    len(candidates),
                    "apiRemaining":    api_remaining,
                    "nextScanAt":      self.next_scan_at,
                    "aiCallsTotal":    self.ai_calls,
                    "aiCostEstimate":  round(self.ai_calls * 0.0004, 4),  # Haiku pricing
                    "pendingCount":    len(pending_bets),
                    "lossStreak":      loss_streak,
                    "clvPct":          clv_pct,
                    "minEdgePct":      CONFIG["min_edge"] * 100,
                    "minBookCount":    CONFIG["min_book_count"],
                    "candidates":      candidates[:10],
                    "pendingBets":     pending_bets,
                    "openBetsList":    list(self.wallet.open_bets.values()),
                    "recentBets":      self.wallet.bet_history[-15:],
                    "suggestedParlays":self._cur_parlays,
                    "walletHistory":   self.wallet.wallet_history[-500:],
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Dashboard: {e}")

    def run_once(self, force=False):
        self.scan_count += 1
        self.wallet.reset_daily_if_needed()

        log.info("=" * 60)
        log.info("  LOACHY v4 #%d | %s | %s",
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

        # ── Step 5: Build candidates (de-vigged edge, using live bankroll for Kelly) ──
        all_candidates = []
        for game in games:
            all_candidates.extend(self.model.analyse_game(game, self.wallet.cash))

        # Best candidate per game (highest real edge)
        best_per_game = {}
        for c in all_candidates:
            gid = c["game_id"]
            if gid not in best_per_game or c["edge"] > best_per_game[gid]["edge"]:
                best_per_game[gid] = c

        # Top N sorted by edge — only real edges (no zero-edge fallbacks)
        top = sorted(best_per_game.values(), key=lambda x: x["edge"], reverse=True
                     )[:CONFIG["max_top_candidates"]]

        log.info("  %d games with real edge (≥%.0f%% de-vigged, ≥%d books) | AI queue: %d",
                 len(all_candidates), CONFIG["min_edge"]*100,
                 CONFIG["min_book_count"], len(top))

        if not top:
            log.info("  No qualifying edges this scan — bankroll preserved")

        # ── Step 6: AI evaluation ──
        kelly_mult     = self.wallet.kelly_multiplier()
        bets_placed    = 0
        pending_added  = 0
        confirmed_list = []

        # Record odds for line movement tracking
        for c in top:
            self.lines.record(c["game_id"], c["outcome_name"], c["best_price"])

        for c in top:
            ok, reason = self.wallet.can_bet(c["game_id"])
            if not ok:
                log.info(f"  Blocked: {reason}")
                continue

            if c["game_id"] in self.wallet.seen_game_ids:
                log.info(f"  Skip (already bet): {c['away']} @ {c['home']}")
                continue

            # Apply streak Kelly multiplier to size
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

            log.info("  AI: %s @ %s | %s %+d | edge +%.1f%% | true %.1f%%",
                     c["away"], c["home"], c["outcome_name"], c["best_price"],
                     c["edge"]*100, c.get("true_prob", c["consensus_prob"])*100)

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
        log.info("Loachy v4 | %s | $%.0f | %d sports | min_edge=%.0f%% | min_books=%d",
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 CONFIG["paper_budget"], len(CONFIG["sports"]),
                 CONFIG["min_edge"]*100, CONFIG["min_book_count"])

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
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                log.info("Loachy rests. Goodbye.")
                self._stop_watcher.set()
                break


if __name__ == "__main__":
    bot = LoachyBot()
    if "--once" in sys.argv:
        bot.run_once(force="--force" in sys.argv)
    else:
        bot.run_loop()
