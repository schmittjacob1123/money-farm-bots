#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║      VESPER v1.0 — POLYMARKET PREDICTION SIGNAL BOT         ║
║  Strategy:                                                   ║
║    1. Fetches active Polymarket markets via Gamma API        ║
║    2. Runs 3 signal types:                                   ║
║       a) Weather  — NOAA forecast vs market price           ║
║       b) Momentum — follow 24h price moves > 12%            ║
║       c) Volume   — follow direction on 3x volume spikes    ║
║    3. Enters when calculated edge > 7%                      ║
║    4. Exits: TP +35%, SL -25%, max 96h hold                 ║
║    5. Paper trading · $500 starting budget                  ║
╚══════════════════════════════════════════════════════════════╝
"""
import os, json, logging, time, sys, random
from datetime import datetime
import requests
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
ET = ZoneInfo("America/New_York")

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
CONFIG = {
    "dry_run":              True,
    "paper_budget":         500.0,
    "state_file":           "vesper_state.json",
    "daily_history_file":   "vesper_daily.json",
    "dashboard_file":       "vesper_data.json",
    "log_file":             "vesper.log",

    # scan timing
    "scan_interval_s":      300,    # 5 min between scans
    "signal_refresh_s":     300,    # 5 min between full signal refresh (match scan)

    # market filters
    "min_liquidity":        500.0,
    "min_volume_24h":       50.0,
    "max_markets_scan":     100,
    "min_end_hours":        24,     # skip markets ending within 24h (same-day props)

    # player prop keywords to skip entirely — these resolve same-day with zero signal value
    "prop_keywords": [
        "points o/u", "rebounds o/u", "assists o/u", "steals o/u", "blocks o/u",
        "threes o/u", "turnovers o/u", "pts o/u", "reb o/u", "ast o/u",
        "passing yards", "rushing yards", "receiving yards", "touchdowns o/u",
        "strikeouts o/u", "hits o/u", "home runs o/u",
    ],

    # position sizing
    "position_size_pct":    0.07,
    "position_size_min":    2.0,
    "position_size_max":    70.0,
    "max_open_positions":   8,
    "cash_floor_pct":       0.25,

    # signal thresholds
    "min_edge":             0.04,   # 4% min edge (prediction markets move slowly)
    "momentum_threshold":   0.03,   # 3% 1d move triggers momentum signal
    "volume_spike_mult":    1.8,    # 1.8x daily average = meaningful spike
    "weather_min_edge":     0.08,
    "weather_min_prob":     0.50,   # NOAA must be >= 50% to enter YES

    # exits
    "take_profit_pct":      0.35,
    "stop_loss_pct":        0.25,
    "max_hold_hours":       96,

    # fees (Polymarket ~0.5% maker)
    "fee_rate":             0.005,

    # risk
    "daily_loss_cap":       60.0,
    "loss_streak_reduce":   3,
}

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"]),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# QUOTES
# ══════════════════════════════════════════════════════════════
QUOTES = {
    "scanning": [
        "Reading the omens...", "The data speaks if you listen.",
        "Probability is truth without certainty.",
        "Every market is a question. I find the answer.",
        "Calibrating signals across all categories...",
        "The crowd is often wrong. The data rarely is.",
    ],
    "hunting": [
        "Edge found. Entering position.",
        "The gap between price and truth — that is my domain.",
        "NOAA says 72%. Market says 41%. The math is simple.",
        "Momentum confirmed. Following the signal.",
        "Volume spike detected. Something knows something.",
    ],
    "watching": [
        "Positions open. Monitoring convergence.",
        "The oracle watches, always.", "Patience is an edge.",
        "Waiting for price to find truth.",
        "The market will come to me.", "Trust the signal.",
    ],
    "sleeping": [
        "No edge exceeds threshold. Resting.",
        "The stars are quiet tonight.",
        "No signal found. Standing by.",
        "Stillness is not inaction — it is discipline.",
    ],
    "exiting": [
        "Target reached. Closing position.",
        "The prophecy is fulfilled.",
        "Exit confirmed. Signal delivered.",
        "Taking profit. The oracle was right.",
    ],
}

# US cities for weather signal (name → lat, lon)
US_CITIES = {
    "new york": (40.7128, -74.0060), "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298), "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740), "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936), "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970), "san jose": (37.3382, -121.8863),
    "austin": (30.2672, -97.7431), "jacksonville": (30.3322, -81.6557),
    "fort worth": (32.7555, -97.3308), "columbus": (39.9612, -82.9988),
    "charlotte": (35.2271, -80.8431), "san francisco": (37.7749, -122.4194),
    "seattle": (47.6062, -122.3321), "denver": (39.7392, -104.9903),
    "nashville": (36.1627, -86.7816), "boston": (42.3601, -71.0589),
    "miami": (25.7617, -80.1918), "atlanta": (33.7490, -84.3880),
    "minneapolis": (44.9778, -93.2650), "portland": (45.5051, -122.6750),
    "las vegas": (36.1699, -115.1398), "memphis": (35.1495, -90.0490),
    "louisville": (38.2527, -85.7585), "baltimore": (39.2904, -76.6122),
    "milwaukee": (43.0389, -87.9065), "albuquerque": (35.0844, -106.6504),
    "tucson": (32.2226, -110.9747), "fresno": (36.7468, -119.7726),
    "sacramento": (38.5816, -121.4944), "kansas city": (39.0997, -94.5786),
    "raleigh": (35.7796, -78.6382), "omaha": (41.2565, -95.9345),
    "cleveland": (41.4993, -81.6944), "pittsburgh": (40.4406, -79.9959),
    "tampa": (27.9506, -82.4572), "orlando": (28.5383, -81.3792),
    "new orleans": (29.9511, -90.0715), "detroit": (42.3314, -83.0458),
    "washington": (38.9072, -77.0369), "dc": (38.9072, -77.0369),
    "oklahoma city": (35.4676, -97.5164), "el paso": (31.7619, -106.4850),
    "indianapolis": (39.7684, -86.1581), "charlotte nc": (35.2271, -80.8431),
}

# ══════════════════════════════════════════════════════════════
# MODULE 1 — POSITION
# ══════════════════════════════════════════════════════════════
class Position:
    def __init__(self, market_id, question, outcome, entry_price,
                 size_usd, signal_type, edge, entry_time=None):
        self.market_id   = market_id
        self.question    = question
        self.outcome     = outcome        # "YES" or "NO"
        self.entry_price = entry_price
        self.size_usd    = size_usd
        self.size_shares = round(size_usd / entry_price, 6)
        self.signal_type = signal_type
        self.edge        = edge
        self.entry_time  = entry_time or datetime.now(ET).isoformat()
        self.peak_value  = size_usd

    def current_value(self, price):
        return round(self.size_shares * price, 4)

    def unrealized_pnl(self, price):
        return round(self.current_value(price) - self.size_usd, 4)

    def unrealized_pct(self, price):
        return round((price - self.entry_price) / self.entry_price * 100, 2)

    def hours_held(self):
        entry = datetime.fromisoformat(self.entry_time)
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=ET)
        return (datetime.now(ET) - entry).total_seconds() / 3600

    def to_dict(self):
        return {
            "market_id":   self.market_id,
            "question":    self.question,
            "outcome":     self.outcome,
            "entry_price": self.entry_price,
            "size_usd":    self.size_usd,
            "size_shares": self.size_shares,
            "signal_type": self.signal_type,
            "edge":        self.edge,
            "entry_time":  self.entry_time,
            "peak_value":  self.peak_value,
        }

    @classmethod
    def from_dict(cls, d):
        p = cls(d["market_id"], d["question"], d["outcome"],
                d["entry_price"], d["size_usd"], d["signal_type"],
                d["edge"], d.get("entry_time"))
        p.size_shares = d.get("size_shares", d["size_usd"] / d["entry_price"])
        p.peak_value  = d.get("peak_value", d["size_usd"])
        return p


# ══════════════════════════════════════════════════════════════
# MODULE 2 — WALLET
# ══════════════════════════════════════════════════════════════
class Wallet:
    def __init__(self):
        self.cash           = CONFIG["paper_budget"]
        self.total_pnl      = 0.0
        self.daily_pnl      = 0.0
        self.wins           = 0
        self.losses         = 0
        self.win_streak     = 0
        self.loss_streak    = 0
        self.total_fees     = 0.0
        self.trade_log      = []
        self.wallet_history = []
        self.peak_portfolio = CONFIG["paper_budget"]
        self.last_date      = datetime.now(ET).date().isoformat()

    def _fee(self, size):
        return round(size * CONFIG["fee_rate"], 4)

    def open_position(self, market_id, question, outcome, price, size_usd):
        fee = self._fee(size_usd)
        self.cash       = round(self.cash - size_usd - fee, 4)
        self.total_fees = round(self.total_fees + fee, 4)
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(),
            "market_id": market_id, "question": question[:60],
            "action": "BUY", "outcome": outcome, "price": price,
            "size": size_usd, "fee": fee, "pnl": 0, "cash": round(self.cash, 2),
        })

    def close_position(self, pos, current_price, reason=""):
        fee       = self._fee(pos.size_usd)
        gross_pnl = pos.unrealized_pnl(current_price)
        net_pnl   = round(gross_pnl - fee, 4)
        returned  = round(pos.size_usd + gross_pnl, 4)
        self.cash       = round(self.cash + returned, 4)
        self.total_pnl  = round(self.total_pnl + net_pnl, 4)
        self.daily_pnl  = round(self.daily_pnl + net_pnl, 4)
        self.total_fees = round(self.total_fees + fee, 4)
        if net_pnl > 0:
            self.wins += 1; self.win_streak += 1; self.loss_streak = 0
        else:
            self.losses += 1; self.loss_streak += 1; self.win_streak = 0
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(),
            "market_id": pos.market_id, "question": pos.question[:60],
            "action": "SELL", "outcome": pos.outcome, "price": current_price,
            "size": pos.size_usd, "fee": fee, "pnl": net_pnl,
            "cash": round(self.cash, 2), "reason": reason,
        })
        return net_pnl

    def reset_daily(self):
        today = datetime.now(ET).date().isoformat()
        if self.last_date != today:
            self.daily_pnl = 0.0
            self.last_date = today

    def daily_loss_hit(self):
        return self.daily_pnl < -CONFIG["daily_loss_cap"]

    def win_rate(self):
        total = self.wins + self.losses
        return round(self.wins / total * 100, 1) if total else 0.0

    def position_size(self, portfolio):
        raw = portfolio * CONFIG["position_size_pct"]
        if self.loss_streak >= CONFIG["loss_streak_reduce"]:
            raw *= 0.6
        return round(max(CONFIG["position_size_min"], min(CONFIG["position_size_max"], raw)), 2)

    def to_dict(self):
        return {
            "cash":           round(self.cash, 4),
            "total_pnl":      round(self.total_pnl, 4),
            "daily_pnl":      round(self.daily_pnl, 4),
            "wins":           self.wins,
            "losses":         self.losses,
            "win_streak":     self.win_streak,
            "loss_streak":    self.loss_streak,
            "total_fees":     round(self.total_fees, 4),
            "trade_log":      self.trade_log[-200:],
            "wallet_history": self.wallet_history[-500:],
            "peak_portfolio": round(self.peak_portfolio, 4),
            "last_date":      self.last_date,
        }

    def load_dict(self, d):
        self.cash           = d.get("cash", CONFIG["paper_budget"])
        self.total_pnl      = d.get("total_pnl", 0.0)
        self.daily_pnl      = d.get("daily_pnl", 0.0)
        self.wins           = d.get("wins", 0)
        self.losses         = d.get("losses", 0)
        self.win_streak     = d.get("win_streak", 0)
        self.loss_streak    = d.get("loss_streak", 0)
        self.total_fees     = d.get("total_fees", 0.0)
        self.trade_log      = d.get("trade_log", [])
        self.wallet_history = d.get("wallet_history", [])
        self.peak_portfolio = d.get("peak_portfolio", CONFIG["paper_budget"])
        self.last_date      = d.get("last_date", datetime.now(ET).date().isoformat())


# ══════════════════════════════════════════════════════════════
# MODULE 3 — GAMMA API (Polymarket)
# ══════════════════════════════════════════════════════════════
class GammaFetcher:
    BASE = "https://gamma-api.polymarket.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "VesperBot/1.0 polymarket-signal-research"

    def fetch_markets(self, limit=100):
        """Fetch active Polymarket events with nested markets, by 24h volume."""
        try:
            r = self.session.get(
                f"{self.BASE}/events",
                params={
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "limit": limit,
                },
                timeout=25,
            )
            r.raise_for_status()
            markets = []
            for event in r.json():
                tags = [t.get("slug", "") for t in event.get("tags", [])]
                for m in event.get("markets", []):
                    m["_event_title"] = event.get("title", "")
                    m["_tags"]        = tags
                    markets.append(m)
            return markets
        except Exception as e:
            log.warning("[GAMMA] Fetch failed: %s", e)
            return []

    @staticmethod
    def parse_yes_no_prices(market):
        """Returns (yes_price, no_price) or None.
        Gamma API returns outcomes/outcomePrices as JSON-encoded strings OR lists."""
        try:
            import json as _json
            outcomes = market.get("outcomes", [])
            prices   = market.get("outcomePrices", [])
            # Gamma API returns these as JSON strings — decode if needed
            if isinstance(outcomes, str):
                outcomes = _json.loads(outcomes)
            if isinstance(prices, str):
                prices = _json.loads(prices)
            if not outcomes or not prices or len(outcomes) != len(prices):
                return None
            yes_idx = next((i for i, o in enumerate(outcomes) if str(o).upper() == "YES"), None)
            no_idx  = next((i for i, o in enumerate(outcomes) if str(o).upper() == "NO"),  None)
            if yes_idx is None or no_idx is None:
                return None
            yp  = float(prices[yes_idx])
            np_ = float(prices[no_idx])
            if not (0.02 <= yp <= 0.98):
                return None
            return yp, np_
        except:
            return None


# ══════════════════════════════════════════════════════════════
# MODULE 4 — WEATHER SIGNAL (NOAA api.weather.gov)
# ══════════════════════════════════════════════════════════════
class WeatherSignal:
    PRECIP_KEYWORDS = [
        "rain", "rainfall", "precipitation", "snow", "snowfall",
        "blizzard", "storm", "hurricane", "flood", "tornado",
    ]

    def __init__(self):
        self._grid_cache     = {}   # "lat,lon" -> {"forecast": url}
        self._forecast_cache = {}   # url -> (data, timestamp)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "VesperBot/1.0 weather-signal jacob@farm"

    def _detect(self, question):
        """Returns (city, weather_type) if this looks like a US weather market."""
        q = question.lower()
        city  = next((c for c in US_CITIES if c in q), None)
        wtype = next((k for k in self.PRECIP_KEYWORDS if k in q), None)
        return (city, wtype) if city and wtype else None

    def _get_forecast(self, city):
        lat, lon = US_CITIES[city]
        grid_key = f"{lat},{lon}"

        if grid_key not in self._grid_cache:
            try:
                r = self.session.get(
                    f"https://api.weather.gov/points/{lat},{lon}", timeout=12
                )
                if r.status_code != 200:
                    return None
                props = r.json().get("properties", {})
                self._grid_cache[grid_key] = {"forecast": props.get("forecast")}
            except Exception as e:
                log.debug("[WEATHER] Grid lookup %s: %s", city, e)
                return None

        url = (self._grid_cache.get(grid_key) or {}).get("forecast")
        if not url:
            return None

        cached = self._forecast_cache.get(url)
        if cached and time.time() - cached[1] < 3600:
            return cached[0]

        try:
            r = self.session.get(url, timeout=12)
            if r.status_code != 200:
                return None
            periods = r.json().get("properties", {}).get("periods", [])
            if not periods:
                return None
            p = periods[0]
            precip = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
            result = {
                "precip_prob":    precip / 100.0,
                "temp":           p.get("temperature", 70),
                "short_forecast": p.get("shortForecast", ""),
            }
            self._forecast_cache[url] = (result, time.time())
            return result
        except Exception as e:
            log.debug("[WEATHER] Forecast %s: %s", city, e)
            return None

    def get_signal(self, question, yes_price):
        detection = self._detect(question)
        if not detection:
            return None
        city, wtype = detection
        forecast = self._get_forecast(city)
        if not forecast:
            return None

        noaa_prob = forecast["precip_prob"]
        edge      = abs(noaa_prob - yes_price)

        if edge < CONFIG["weather_min_edge"]:
            return None
        if noaa_prob < CONFIG["weather_min_prob"] and noaa_prob > yes_price:
            return None  # NOAA not confident enough for YES long

        return {
            "signal_type": "weather",
            "direction":   "YES" if noaa_prob > yes_price else "NO",
            "edge":        round(edge, 4),
            "probability": round(noaa_prob, 3),
            "city":        city,
            "forecast":    forecast["short_forecast"],
            "confidence":  min(edge * 2.5, 1.0),
        }


# ══════════════════════════════════════════════════════════════
# MODULE 5 — PRICE SIGNAL (momentum + volume + reversion)
# ══════════════════════════════════════════════════════════════
class PriceSignal:
    def get_signal(self, market, yes_price):
        signals = []
        chg_1d = market.get("oneDayPriceChange") or 0

        # ── MOMENTUM ──
        if abs(chg_1d) >= CONFIG["momentum_threshold"]:
            edge = abs(chg_1d) * 0.45
            if edge >= CONFIG["min_edge"]:
                signals.append({
                    "signal_type":  "momentum",
                    "direction":    "YES" if chg_1d > 0 else "NO",
                    "edge":         round(edge, 4),
                    "price_chg_1d": chg_1d,
                    "confidence":   min(abs(chg_1d) * 3.5, 1.0),
                })

        # ── VOLUME SURGE ──
        vol_24h = market.get("volume24hr") or 0
        vol_1wk = market.get("volume1wk")  or 0
        if vol_1wk > 0 and vol_24h > 0:
            avg_daily = vol_1wk / 7
            if avg_daily > 10:
                ratio = vol_24h / avg_daily
                if ratio >= CONFIG["volume_spike_mult"]:
                    edge = min((ratio - 1) * 0.035, 0.14)
                    if edge >= CONFIG["min_edge"]:
                        signals.append({
                            "signal_type":  "volume",
                            "direction":    "YES" if chg_1d >= 0 else "NO",
                            "edge":         round(edge, 4),
                            "volume_ratio": round(ratio, 2),
                            "confidence":   min(ratio / 12, 1.0),
                        })

        # ── MEAN REVERSION ──
        chg_1w = market.get("oneWeekPriceChange") or 0
        if abs(chg_1w) > 0.28:
            edge = abs(chg_1w) * 0.28
            if edge >= CONFIG["min_edge"]:
                signals.append({
                    "signal_type":  "reversion",
                    "direction":    "NO" if chg_1w > 0 else "YES",
                    "edge":         round(edge, 4),
                    "price_chg_1w": chg_1w,
                    "confidence":   min(abs(chg_1w) * 1.5, 0.65),
                })

        return max(signals, key=lambda s: s["edge"]) if signals else None


# ══════════════════════════════════════════════════════════════
# MODULE 6 — VESPER BOT
# ══════════════════════════════════════════════════════════════
class VesperBot:
    def __init__(self):
        self.gamma        = GammaFetcher()
        self.weather      = WeatherSignal()
        self.price_signal = PriceSignal()
        self.wallet       = Wallet()
        self.positions    = []
        self.scan_count   = 0
        self._last_signal_refresh = 0.0
        self._signal_cache        = {}
        self._market_cache        = {}
        self._daily_history       = []
        self._signal_log          = []
        self._load_state()
        self._load_daily_history()

    # ── PERSISTENCE ──────────────────────────────────────────
    def _load_state(self):
        try:
            with open(CONFIG["state_file"]) as f:
                d = json.load(f)
            self.wallet.load_dict(d.get("wallet", {}))
            for pd in d.get("positions", []):
                self.positions.append(Position.from_dict(pd))
            log.info("[INIT] Loaded | cash=$%.2f | pnl=$%+.2f | positions=%d",
                     self.wallet.cash, self.wallet.total_pnl, len(self.positions))
        except FileNotFoundError:
            log.info("[INIT] Fresh start at $%.2f", CONFIG["paper_budget"])
        except Exception as e:
            log.warning("[INIT] Load failed: %s — fresh start", e)

    def _save_state(self):
        try:
            with open(CONFIG["state_file"], "w") as f:
                json.dump({"wallet": self.wallet.to_dict(),
                           "positions": [p.to_dict() for p in self.positions]}, f, indent=2)
        except Exception as e:
            log.warning("State save failed: %s", e)

    def _load_daily_history(self):
        try:
            with open(CONFIG["daily_history_file"]) as f:
                self._daily_history = json.load(f)
        except:
            self._daily_history = []

    def _save_daily_history(self, portfolio):
        today = datetime.now(ET).date().isoformat()
        if self._daily_history and self._daily_history[-1]["d"] == today:
            self._daily_history[-1]["v"] = round(portfolio, 2)
        else:
            self._daily_history.append({"d": today, "v": round(portfolio, 2)})
        self._daily_history = self._daily_history[-365:]
        try:
            with open(CONFIG["daily_history_file"], "w") as f:
                json.dump(self._daily_history, f)
        except:
            pass

    # ── HELPERS ──────────────────────────────────────────────
    def _portfolio_value(self, pos_prices):
        pos_val = sum(
            p.current_value(pos_prices.get(p.market_id, p.entry_price))
            for p in self.positions
        )
        return round(self.wallet.cash + pos_val, 2)

    def _get_pos_price(self, pos, market):
        prices = GammaFetcher.parse_yes_no_prices(market)
        if not prices:
            return pos.entry_price
        return prices[0] if pos.outcome == "YES" else prices[1]

    # ── SIGNAL ENGINE ─────────────────────────────────────────
    def _refresh_signals(self, markets):
        self._market_cache = {m["id"]: m for m in markets if m.get("id")}
        new_signals = {}
        for m in markets:
            mid = m.get("id")
            if not mid:
                continue
            prices = GammaFetcher.parse_yes_no_prices(m)
            if not prices:
                continue
            yes_price = prices[0]
            liq   = m.get("liquidityNum") or 0
            vol24 = m.get("volume24hr")   or 0
            if liq < CONFIG["min_liquidity"] or vol24 < CONFIG["min_volume_24h"]:
                continue
            # Skip markets resolving within min_end_hours (same-day props)
            end_date = m.get("endDate") or m.get("endDateIso", "")
            if end_date:
                try:
                    from datetime import timezone
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left < CONFIG["min_end_hours"]:
                        continue
                except Exception:
                    pass
            q = m.get("question", "")
            # Skip player prop markets entirely — no signal value
            q_lower = q.lower()
            if any(kw in q_lower for kw in CONFIG["prop_keywords"]):
                continue
            sig = self.weather.get_signal(q, yes_price)
            if sig is None:
                sig = self.price_signal.get_signal(m, yes_price)
            if sig and sig["edge"] >= CONFIG["min_edge"]:
                sig.update({
                    "market_id":  mid,
                    "question":   q,
                    "yes_price":  yes_price,
                    "no_price":   round(1 - yes_price, 4),
                    "liquidity":  liq,
                    "volume_24h": vol24,
                })
                new_signals[mid] = sig
        self._signal_cache = new_signals
        log.info("[SIGNALS] %d signals from %d markets", len(new_signals), len(markets))

    # ── EXITS ─────────────────────────────────────────────────
    def _check_exits(self, pos_prices):
        closed = []
        for pos in self.positions:
            price  = pos_prices.get(pos.market_id, pos.entry_price)
            pct    = pos.unrealized_pct(price)
            hours  = pos.hours_held()
            reason = None
            if pct >= CONFIG["take_profit_pct"] * 100:
                reason = "TP"
            elif pct <= -CONFIG["stop_loss_pct"] * 100:
                reason = "SL"
            elif hours >= CONFIG["max_hold_hours"]:
                reason = "TIME"
            elif pos.market_id in self._market_cache:
                m = self._market_cache[pos.market_id]
                if m.get("closed") or not m.get("active", True):
                    reason = "CLOSED"
            if reason:
                pnl = self.wallet.close_position(pos, price, reason)
                log.info("  [EXIT/%s] %s | %s @ %.3f→%.3f | pct=%.1f%% | pnl=$%+.2f",
                         reason, pos.market_id[:12], pos.outcome,
                         pos.entry_price, price, pct, pnl)
                closed.append(pos)
        for pos in closed:
            self.positions.remove(pos)
        return len(closed)

    # ── ENTRIES ───────────────────────────────────────────────
    def _try_enter(self, portfolio):
        entered = 0
        # Weather signals rank first (highest quality), then momentum, reversion, volume
        TYPE_PRIORITY = {"weather": 0, "momentum": 1, "reversion": 2, "volume": 3}
        ranked = sorted(
            self._signal_cache.values(),
            key=lambda s: (TYPE_PRIORITY.get(s["signal_type"], 9), -s["edge"])
        )
        for sig in ranked:
            if len(self.positions) >= CONFIG["max_open_positions"]:
                break
            if self.wallet.cash < portfolio * CONFIG["cash_floor_pct"]:
                break
            if self.wallet.daily_loss_hit():
                break
            mid = sig["market_id"]
            if any(p.market_id == mid for p in self.positions):
                continue
            direction   = sig["direction"]
            entry_price = sig["yes_price"] if direction == "YES" else sig["no_price"]
            if not (0.03 <= entry_price <= 0.97):
                continue
            size = min(self.wallet.position_size(portfolio), self.wallet.cash * 0.85)
            if size < CONFIG["position_size_min"]:
                continue

            pos = Position(mid, sig["question"], direction, entry_price, size,
                           sig["signal_type"], sig["edge"])
            self.positions.append(pos)
            self.wallet.open_position(mid, sig["question"], direction, entry_price, size)
            self._signal_log.append({
                "ts":        datetime.now(ET).isoformat(),
                "type":      sig["signal_type"],
                "question":  sig["question"][:70],
                "direction": direction,
                "edge":      sig["edge"],
                "price":     entry_price,
                "size":      size,
            })
            self._signal_log = self._signal_log[-50:]
            log.info("  [BUY/%s] %s | %s @ %.3f | edge=%.1f%% | $%.2f",
                     sig["signal_type"].upper(), mid[:12],
                     direction, entry_price, sig["edge"] * 100, size)
            entered += 1
        return entered

    # ── DASHBOARD ─────────────────────────────────────────────
    def _write_dashboard(self, markets, portfolio, trades_this_scan, pos_prices):
        try:
            pnl = round(portfolio - CONFIG["paper_budget"], 4)
            roi = round(pnl / CONFIG["paper_budget"] * 100, 2)

            positions_out = []
            for pos in self.positions:
                price = pos_prices.get(pos.market_id, pos.entry_price)
                positions_out.append({
                    "marketId":      pos.market_id,
                    "question":      pos.question,
                    "outcome":       pos.outcome,
                    "entryPrice":    pos.entry_price,
                    "currentPrice":  price,
                    "sizeUsd":       pos.size_usd,
                    "entryTime":     pos.entry_time,
                    "signalType":    pos.signal_type,
                    "edge":          pos.edge,
                    "unrealizedPnl": pos.unrealized_pnl(price),
                    "unrealizedPct": pos.unrealized_pct(price),
                    "hoursHeld":     round(pos.hours_held(), 1),
                })

            signal_breakdown = {}
            for sig in self._signal_log:
                st = sig["type"]
                signal_breakdown[st] = signal_breakdown.get(st, 0) + 1

            mood = "sleeping"
            if trades_this_scan > 0:
                recent = self.wallet.trade_log[-max(trades_this_scan, 1):]
                mood = "hunting" if any(t["action"] == "BUY" for t in recent) else "exiting"
            elif self.positions:
                mood = "watching"
            elif self._signal_cache:
                mood = "scanning"

            top_signals = sorted(
                self._signal_cache.values(), key=lambda s: s["edge"], reverse=True
            )[:10]

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "version":           "v1",
                    "lastScan":          datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":              "PAPER",
                    "mood":              mood,
                    "quote":             random.choice(QUOTES.get(mood, QUOTES["scanning"])),
                    "cash":              round(self.wallet.cash, 2),
                    "portfolioValue":    portfolio,
                    "portfolioPnl":      pnl,
                    "portfolioRoi":      roi,
                    "totalPnl":          round(self.wallet.total_pnl, 4),
                    "dailyPnl":          round(self.wallet.daily_pnl, 4),
                    "winRate":           self.wallet.win_rate(),
                    "wins":              self.wallet.wins,
                    "losses":            self.wallet.losses,
                    "winStreak":         self.wallet.win_streak,
                    "lossStreak":        self.wallet.loss_streak,
                    "tradesTotal":       self.wallet.wins + self.wallet.losses,
                    "totalFees":         round(self.wallet.total_fees, 4),
                    "scanCount":         self.scan_count,
                    "tradesThisScan":    trades_this_scan,
                    "openCount":         len(self.positions),
                    "openPositionValue": round(portfolio - self.wallet.cash, 2),
                    "marketsScanned":    len(markets),
                    "signalsFound":      len(self._signal_cache),
                    "startingBudget":    CONFIG["paper_budget"],
                    "peakPortfolio":     round(self.wallet.peak_portfolio, 2),
                    "drawdownPct":       round(
                        (self.wallet.peak_portfolio - portfolio) / self.wallet.peak_portfolio * 100, 2
                    ) if self.wallet.peak_portfolio > 0 else 0,
                    "positions":         positions_out,
                    "recentTrades":      self.wallet.trade_log[-20:],
                    "recentSignals":     self._signal_log[-20:],
                    "signalBreakdown":   signal_breakdown,
                    "dailyHistory":      self._daily_history[-60:],
                    "topSignals":        top_signals,
                }, f, indent=2)
        except Exception as e:
            log.warning("Dashboard write failed: %s", e)

    # ── MAIN SCAN ─────────────────────────────────────────────
    def run_once(self):
        self.scan_count += 1
        self.wallet.reset_daily()
        log.info("=" * 64)
        log.info("  VESPER v1 #%d | %s | cash=$%.2f | pnl=$%+.2f | open=%d",
                 self.scan_count,
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                 self.wallet.cash, self.wallet.total_pnl, len(self.positions))
        log.info("=" * 64)

        markets = self.gamma.fetch_markets(limit=CONFIG["max_markets_scan"])
        if not markets:
            log.warning("[SCAN] No markets fetched — skipping")
            return 0
        log.info("  [SCAN] Fetched %d markets", len(markets))

        # Build position price map
        pos_prices = {}
        for pos in self.positions:
            m = next((x for x in markets if x.get("id") == pos.market_id), None)
            pos_prices[pos.market_id] = self._get_pos_price(pos, m) if m else pos.entry_price

        # Update peak values
        for pos in self.positions:
            curr = pos.current_value(pos_prices.get(pos.market_id, pos.entry_price))
            if curr > pos.peak_value:
                pos.peak_value = curr

        exits = self._check_exits(pos_prices)

        # Refresh signals periodically
        now = time.time()
        if now - self._last_signal_refresh >= CONFIG["signal_refresh_s"]:
            self._refresh_signals(markets)
            self._last_signal_refresh = now

        portfolio = self._portfolio_value(pos_prices)
        if portfolio > self.wallet.peak_portfolio:
            self.wallet.peak_portfolio = portfolio

        entries = 0
        if not self.wallet.daily_loss_hit():
            entries = self._try_enter(portfolio)
        else:
            log.warning("[RISK] Daily loss cap — no new entries")

        trades = exits + entries
        self._save_daily_history(portfolio)
        self.wallet.wallet_history.append({"ts": datetime.now(ET).isoformat(), "v": portfolio})
        self.wallet.wallet_history = self.wallet.wallet_history[-500:]
        self._write_dashboard(markets, portfolio, trades, pos_prices)
        self._save_state()
        log.info("  [DONE] exits=%d entries=%d open=%d cash=$%.2f portfolio=$%.2f",
                 exits, entries, len(self.positions), self.wallet.cash, portfolio)
        return trades

    def run_loop(self):
        log.info("[VESPER] Starting — budget=$%.2f dry_run=%s",
                 CONFIG["paper_budget"], CONFIG["dry_run"])
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("[VESPER] Shutdown")
                break
            except Exception as e:
                log.error("[VESPER] Error: %s", e, exc_info=True)
            try:
                log.info("Sleeping %ds...", CONFIG["scan_interval_s"])
                time.sleep(CONFIG["scan_interval_s"])
            except KeyboardInterrupt:
                log.info("[VESPER] Shutdown")
                break


if __name__ == "__main__":
    VesperBot().run_loop()
