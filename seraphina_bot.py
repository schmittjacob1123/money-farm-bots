#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║      SERAPHINA v10 — GRID + RSI/MA HYBRID                   ║
║  Strategy:                                                   ║
║    1. Grid trading: buys every 1% price dip through a level  ║
║    2. RSI gate: skip grid buys if RSI > 70 (overbought)      ║
║    3. RSI boost: larger size when RSI < 40 (oversold dip)    ║
║    4. MA50 trend: reduce size in downtrend                   ║
║    5. Exits: TP 2.5%, SL 4%, trailing stop, RSI>65 sell     ║
║    6. Funding rate income on open positions                  ║
║    7. Maker-tier fees 0.16%                                  ║
╚══════════════════════════════════════════════════════════════╝

CHANGES v10 vs v9:
  - Grid trading restored: buys triggered by price crossing grid
    levels (every 1%), not just RSI<35. This generates consistent
    daily activity from normal crypto volatility.
  - RSI now a FILTER not the sole trigger:
      RSI > 70  → skip grid buy (overbought protection)
      RSI 40-70 → buy normally on grid cross
      RSI < 40  → buy with 1.3x size boost (oversold dip)
  - Multiple positions per coin supported (up to 3)
  - MA50 downtrend → halve position size (trend awareness)
  - All v9 safety features kept: circuit breaker, daily loss cap,
    cash floor, trailing stop, TP, SL, funding income
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
    "dry_run":              os.getenv("DRY_RUN", "true").lower() != "false",
    "paper_budget":         1000.0,
    "coins":                ["BTC", "ETH", "SOL", "DOGE"],

    # — grid —
    "grid_levels":          8,      # levels above and below center
    "grid_spacing_pct":     0.010,  # 1% between levels
    "grid_drift_pct":       0.04,   # rebuild grid if price drifts >4% from center

    # — indicators (1h candles) —
    "ma_period":            50,
    "rsi_period":           14,
    "candle_interval":      60,     # 60 min = 1h
    "candle_count":         75,

    # — RSI gates —
    "rsi_buy_max":          70,     # skip grid buy if RSI above this
    "rsi_boost_threshold":  40,     # boost size if RSI below this (oversold)
    "rsi_boost_mult":       1.3,    # size multiplier on oversold dip
    "rsi_sell_min":         65,     # close profitable positions if RSI above this

    # — trade management —
    "take_profit_pct":      0.025,  # 2.5% TP per position
    "stop_loss_pct":        0.040,  # 4% hard stop
    "trail_activate_pct":   0.015,  # trailing stop arms at +1.5%
    "trail_stop_pct":       0.010,  # trails 1.0% below peak

    # — sizing —
    "trade_size_pct":       0.08,   # 8% of portfolio per grid trade
    "trade_size_min":       1.0,
    "trade_size_max":       100.0,
    "downtrend_size_mult":  0.5,    # halve size when below MA50
    "loss_streak_halve":    3,      # halve size after N consecutive losses
    "max_open_per_coin":    3,      # grid allows stacking positions per coin
    "max_open_total":       12,

    # — fees (maker tier) —
    "trading_fee_pct":      0.0016,
    "spread_pct":           0.0005,

    # — funding rate —
    "funding_threshold":    0.0003,
    "funding_interval_h":   8,

    # — risk —
    "daily_loss_cap":       30.0,
    "drawdown_pause_pct":   0.15,
    "drawdown_resume_pct":  0.08,
    "cash_floor_pct":       0.15,

    # — misc —
    "scan_interval_sec":    60,
    "log_file":             "seraphina.log",
    "state_file":           "seraphina_state.json",
    "dashboard_file":       "seraphina_data.json",
    "daily_history_file":   "seraphina_daily.json",
}

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CONFIG["log_file"], encoding="utf-8")],
)
log = logging.getLogger("seraphina")
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_console)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ══════════════════════════════════════════════════════════════
# PERSONALITY
# ══════════════════════════════════════════════════════════════
QUOTES = {
    "hunting":  [
        "Grid level crossed. Deploying capital.",
        "Price dipped through a level — bought the pullback.",
        "Buy the dip. Sell the rip. Repeat.",
        "Volatility is free money when you're positioned right.",
    ],
    "watching": [
        "Grids are set. Waiting for the next crossing.",
        "RSI is high — holding off on new buys.",
        "Trend is bullish but RSI isn't there yet. Patience.",
        "Positions open. Watching for TP or RSI exit.",
    ],
    "sleeping": [
        "Overbought on all coins. Grid buys paused.",
        "No crossings yet. The grid is patient.",
        "Low volatility. My levels are still there.",
    ],
    "exiting": [
        "Target hit. Booking gains.",
        "RSI overbought — closing profitable positions.",
        "Sold into strength. Resetting grid.",
    ],
}


# ══════════════════════════════════════════════════════════════
# MODULE 1 — CANDLE + INDICATOR FETCHER
# ══════════════════════════════════════════════════════════════
class CandleFetcher:
    KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
    SYMBOLS = {
        "BTC":  "XBTUSD",
        "ETH":  "ETHUSD",
        "SOL":  "SOLUSD",
        "DOGE": "XDGUSD",
    }

    def fetch(self, coin):
        pair = self.SYMBOLS.get(coin)
        if not pair:
            return None
        try:
            r = requests.get(
                self.KRAKEN_OHLC,
                params={"pair": pair, "interval": CONFIG["candle_interval"]},
                timeout=10,
            )
            r.raise_for_status()
            result = r.json().get("result", {})
            raw = [v for k, v in result.items() if k != "last"]
            if not raw:
                return None
            candles = raw[0][-CONFIG["candle_count"]:]
            return [
                {
                    "time":   c[0],
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[6]),
                }
                for c in candles
            ]
        except Exception as e:
            log.debug("Candle fetch %s: %s", coin, e)
            return None

    @staticmethod
    def calc_rsi(closes, period=14):
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_g = sum(gains[:period]) / period
        avg_l = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 2)

    @staticmethod
    def calc_ma(closes, period=50):
        if len(closes) < period:
            return None
        return round(sum(closes[-period:]) / period, 6)


# ══════════════════════════════════════════════════════════════
# MODULE 2 — FUNDING RATE FETCHER
# ══════════════════════════════════════════════════════════════
class FundingFetcher:
    BINANCE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
    SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT"}

    def fetch(self, coin):
        symbol = self.SYMBOLS.get(coin)
        if not symbol:
            return None
        try:
            r = requests.get(self.BINANCE_URL, params={"symbol": symbol}, timeout=8)
            r.raise_for_status()
            d = r.json()
            return {
                "rate":              float(d.get("lastFundingRate", 0)),
                "next_funding_time": int(d.get("nextFundingTime", 0)),
                "mark_price":        float(d.get("markPrice", 0)),
            }
        except Exception as e:
            log.debug("Funding fetch %s: %s", coin, e)
            return None


# ══════════════════════════════════════════════════════════════
# MODULE 3 — GRID
# ══════════════════════════════════════════════════════════════
class Grid:
    def __init__(self, coin, center):
        self.coin       = coin
        self.center     = center
        self.created_at = datetime.now(ET).isoformat()
        self.levels     = self._build(center)
        self.occupied   = set()   # set of level indices that have an open position
        log.info("  [GRID/%s] Built | center=$%.4f | %d levels | spacing=%.1f%%",
                 coin, center, len(self.levels), CONFIG["grid_spacing_pct"] * 100)

    def _build(self, center):
        n  = CONFIG["grid_levels"]
        sp = CONFIG["grid_spacing_pct"]
        dec = 2 if center >= 10 else 4 if center >= 0.1 else 6
        return sorted(set(
            round(center * (1 + i * sp), dec)
            for i in range(-n, n + 1)
        ))

    def drifted(self, price):
        return abs(price - self.center) / self.center > CONFIG["grid_drift_pct"]

    def find_buy_crossings(self, prev, curr):
        """Return level indices crossed downward (buy signals)."""
        lo, hi = min(prev, curr), max(prev, curr)
        return [
            i for i, lvl in enumerate(self.levels)
            if lo <= lvl <= hi and curr < prev and i not in self.occupied
        ]

    def find_sell_crossings(self, prev, curr):
        """Return level indices crossed upward that have open positions below."""
        lo, hi = min(prev, curr), max(prev, curr)
        return [
            i for i, lvl in enumerate(self.levels)
            if lo <= lvl <= hi and curr > prev
        ]

    def to_dict(self):
        return {
            "coin":       self.coin,
            "center":     self.center,
            "created_at": self.created_at,
            "levels":     self.levels,
            "occupied":   list(self.occupied),
        }

    @classmethod
    def from_dict(cls, d):
        g = cls.__new__(cls)
        g.coin       = d["coin"]
        g.center     = d["center"]
        g.created_at = d.get("created_at", "")
        g.levels     = d["levels"]
        g.occupied   = set(d.get("occupied", []))
        return g


# ══════════════════════════════════════════════════════════════
# MODULE 4 — POSITION
# ══════════════════════════════════════════════════════════════
class Position:
    def __init__(self, coin, entry_price, size_usd, grid_level_idx=None):
        self.coin            = coin
        self.entry_price     = entry_price
        self.size_usd        = size_usd
        self.entry_time      = datetime.now(ET).isoformat()
        self.peak_price      = entry_price
        self.tp_price        = round(entry_price * (1 + CONFIG["take_profit_pct"]), 6)
        self.sl_price        = round(entry_price * (1 - CONFIG["stop_loss_pct"]),   6)
        self.trailing_active = False
        self.trailing_stop   = None
        self.grid_level_idx  = grid_level_idx  # which grid level opened this

    def update(self, price):
        if price > self.peak_price:
            self.peak_price = price
        if (self.peak_price - self.entry_price) / self.entry_price >= CONFIG["trail_activate_pct"]:
            self.trailing_active = True
            self.trailing_stop   = round(self.peak_price * (1 - CONFIG["trail_stop_pct"]), 6)

    def check_exit(self, price):
        self.update(price)
        if price >= self.tp_price:
            return True, "TP"
        if price <= self.sl_price:
            return True, "SL"
        if self.trailing_active and self.trailing_stop and price <= self.trailing_stop:
            return True, "TRAIL"
        return False, None

    def unrealized_pnl(self, price):
        return round(self.size_usd * (price - self.entry_price) / self.entry_price, 4)

    def unrealized_pct(self, price):
        return round((price - self.entry_price) / self.entry_price * 100, 2)

    def to_dict(self):
        return {
            "coin":            self.coin,
            "entry_price":     self.entry_price,
            "size_usd":        self.size_usd,
            "entry_time":      self.entry_time,
            "peak_price":      self.peak_price,
            "tp_price":        self.tp_price,
            "sl_price":        self.sl_price,
            "trailing_active": self.trailing_active,
            "trailing_stop":   self.trailing_stop,
            "grid_level_idx":  self.grid_level_idx,
        }

    @classmethod
    def from_dict(cls, d):
        p = cls.__new__(cls)
        p.coin            = d["coin"]
        p.entry_price     = d["entry_price"]
        p.size_usd        = d["size_usd"]
        p.entry_time      = d["entry_time"]
        p.peak_price      = d.get("peak_price", d["entry_price"])
        p.tp_price        = d["tp_price"]
        p.sl_price        = d["sl_price"]
        p.trailing_active = d.get("trailing_active", False)
        p.trailing_stop   = d.get("trailing_stop", None)
        p.grid_level_idx  = d.get("grid_level_idx", None)
        return p


# ══════════════════════════════════════════════════════════════
# MODULE 5 — WALLET
# ══════════════════════════════════════════════════════════════
class Wallet:
    def __init__(self):
        self.cash                   = CONFIG["paper_budget"]
        self.total_pnl              = 0.0
        self.daily_pnl              = 0.0
        self.wins                   = 0
        self.losses                 = 0
        self.win_streak             = 0
        self.loss_streak            = 0
        self.last_date              = datetime.now(ET).date().isoformat()
        self.trade_log              = []
        self.wallet_history         = []
        self.peak_portfolio         = CONFIG["paper_budget"]
        self.circuit_breaker_active = False
        self.total_fees             = 0.0
        self.funding_income         = 0.0

    def reset_daily(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_date:
            self.daily_pnl = 0.0
            self.last_date = today
            log.info("[WALLET] Daily P&L reset")

    def _fee(self, size_usd):
        return round(size_usd * (CONFIG["trading_fee_pct"] + CONFIG["spread_pct"]), 4)

    def buy(self, coin, price, size_usd):
        fee = self._fee(size_usd)
        self.cash       = round(self.cash - size_usd - fee, 4)
        self.total_fees = round(self.total_fees + fee, 4)
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "BUY", "price": price, "size": size_usd,
            "fee": fee, "pnl": 0, "cash": round(self.cash, 2),
        })

    def sell(self, coin, price, position, reason=""):
        gross_pnl = position.unrealized_pnl(price)
        fee       = self._fee(position.size_usd)
        net_pnl   = round(gross_pnl - fee, 4)
        returned  = round(position.size_usd + net_pnl, 4)
        self.cash       = round(self.cash + returned, 4)
        self.total_pnl  = round(self.total_pnl + net_pnl, 4)
        self.daily_pnl  = round(self.daily_pnl + net_pnl, 4)
        self.total_fees = round(self.total_fees + fee, 4)
        if net_pnl > 0:
            self.wins += 1; self.win_streak += 1; self.loss_streak = 0
        else:
            self.losses += 1; self.loss_streak += 1; self.win_streak = 0
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "SELL", "price": price, "size": position.size_usd,
            "fee": fee, "pnl": net_pnl, "cash": round(self.cash, 2), "reason": reason,
        })
        return net_pnl

    def add_funding(self, coin, amount, rate):
        self.cash           = round(self.cash + amount, 4)
        self.total_pnl      = round(self.total_pnl + amount, 4)
        self.daily_pnl      = round(self.daily_pnl + amount, 4)
        self.funding_income = round(self.funding_income + amount, 4)
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "FUNDING", "price": 0, "size": amount,
            "pnl": amount, "cash": round(self.cash, 2),
            "reason": f"{rate * 100:.4f}%/8h",
        })

    def daily_loss_hit(self):
        return self.daily_pnl < -CONFIG["daily_loss_cap"]

    def update_peak(self, portfolio):
        if portfolio > self.peak_portfolio:
            self.peak_portfolio = portfolio
        dd = (self.peak_portfolio - portfolio) / self.peak_portfolio
        if not self.circuit_breaker_active and dd >= CONFIG["drawdown_pause_pct"]:
            self.circuit_breaker_active = True
            log.warning("[CB] Down %.1f%% from peak $%.2f — PAUSING", dd * 100, self.peak_portfolio)
        elif self.circuit_breaker_active and dd <= CONFIG["drawdown_resume_pct"]:
            self.circuit_breaker_active = False
            log.info("[CB] Recovered (%.1f%% from peak) — RESUMING", dd * 100)
        return dd

    def win_rate(self):
        total = self.wins + self.losses
        return round(self.wins / total * 100, 1) if total else 0.0

    def trade_size(self, portfolio, rsi=None, above_ma=True):
        raw = portfolio * CONFIG["trade_size_pct"]
        # halve in downtrend
        if not above_ma:
            raw *= CONFIG["downtrend_size_mult"]
        # boost on oversold RSI
        if rsi is not None and rsi < CONFIG["rsi_boost_threshold"]:
            raw *= CONFIG["rsi_boost_mult"]
        # halve on loss streak
        if self.loss_streak >= CONFIG["loss_streak_halve"]:
            raw *= 0.5
        return round(max(CONFIG["trade_size_min"], min(CONFIG["trade_size_max"], raw)), 2)

    def to_dict(self):
        return {
            "cash":                   round(self.cash, 4),
            "total_pnl":              round(self.total_pnl, 4),
            "daily_pnl":              round(self.daily_pnl, 4),
            "wins":                   self.wins,
            "losses":                 self.losses,
            "win_streak":             self.win_streak,
            "loss_streak":            self.loss_streak,
            "last_date":              self.last_date,
            "trade_log":              self.trade_log[-200:],
            "wallet_history":         self.wallet_history[-500:],
            "peak_portfolio":         round(self.peak_portfolio, 4),
            "circuit_breaker_active": self.circuit_breaker_active,
            "total_fees":             round(self.total_fees, 4),
            "funding_income":         round(self.funding_income, 4),
        }

    def load_dict(self, d):
        self.cash                   = d.get("cash", CONFIG["paper_budget"])
        self.total_pnl              = d.get("total_pnl", 0.0)
        self.daily_pnl              = d.get("daily_pnl", 0.0)
        self.wins                   = d.get("wins", 0)
        self.losses                 = d.get("losses", 0)
        self.win_streak             = d.get("win_streak", 0)
        self.loss_streak            = d.get("loss_streak", 0)
        self.last_date              = d.get("last_date", datetime.now(ET).date().isoformat())
        self.trade_log              = d.get("trade_log", [])
        self.wallet_history         = d.get("wallet_history", [])
        self.peak_portfolio         = d.get("peak_portfolio", CONFIG["paper_budget"])
        self.circuit_breaker_active = d.get("circuit_breaker_active", False)
        self.total_fees             = d.get("total_fees", 0.0)
        self.funding_income         = d.get("funding_income", 0.0)


# ══════════════════════════════════════════════════════════════
# MODULE 6 — MAIN BOT
# ══════════════════════════════════════════════════════════════
class SeraphinaBot:
    def __init__(self):
        self.candles        = CandleFetcher()
        self.funding_api    = FundingFetcher()
        self.wallet         = Wallet()
        self.positions      = []        # List[Position]
        self.grids          = {}        # coin -> Grid
        self.prev_prices    = {}        # coin -> last price
        self.signals        = {}        # coin -> signal dict
        self.funding_rates  = {}
        self._last_funding  = {}
        self.scan_count     = 0
        self._daily_history = []
        self._load_state()
        self._load_daily_history()

    # ── persistence ──────────────────────────────────────────
    def _load_state(self):
        try:
            with open(CONFIG["state_file"]) as f:
                d = json.load(f)
            self.wallet.load_dict(d.get("wallet", {}))
            for pd in d.get("positions", []):
                self.positions.append(Position.from_dict(pd))
            for coin, gd in d.get("grids", {}).items():
                self.grids[coin] = Grid.from_dict(gd)
            self._last_funding = d.get("last_funding", {})
            log.info("[INIT] Loaded | cash=$%.2f | pnl=$%+.2f | positions=%d | grids=%d",
                     self.wallet.cash, self.wallet.total_pnl,
                     len(self.positions), len(self.grids))
        except FileNotFoundError:
            log.info("[INIT] No state — fresh start at $%.2f", CONFIG["paper_budget"])
        except Exception as e:
            log.warning("[INIT] State load failed: %s — fresh start", e)

    def _save_state(self):
        try:
            with open(CONFIG["state_file"], "w") as f:
                json.dump({
                    "wallet":       self.wallet.to_dict(),
                    "positions":    [p.to_dict() for p in self.positions],
                    "grids":        {c: g.to_dict() for c, g in self.grids.items()},
                    "last_funding": self._last_funding,
                }, f, indent=2)
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
        except Exception as e:
            log.warning("Daily history save failed: %s", e)

    # ── helpers ──────────────────────────────────────────────
    def _portfolio_value(self, prices):
        pos_val = sum(
            p.size_usd * (prices.get(p.coin, p.entry_price) / p.entry_price)
            for p in self.positions
        )
        return round(self.wallet.cash + pos_val, 2)

    def _open_for_coin(self, coin):
        return [p for p in self.positions if p.coin == coin]

    def _analyze(self, coin):
        candles = self.candles.fetch(coin)
        if not candles or len(candles) < CONFIG["ma_period"] + CONFIG["rsi_period"]:
            return None
        closes = [c["close"] for c in candles]
        price  = closes[-1]
        ma50   = CandleFetcher.calc_ma(closes, CONFIG["ma_period"])
        rsi    = CandleFetcher.calc_rsi(closes, CONFIG["rsi_period"])
        if ma50 is None or rsi is None:
            return None
        above_ma = price > ma50
        return {
            "coin":           coin,
            "price":          price,
            "ma50":           ma50,
            "rsi":            rsi,
            "trend":          "bullish" if above_ma else "bearish",
            "trend_strength": round((price - ma50) / ma50 * 100, 2),
            "above_ma":       above_ma,
            "high_24h":       max(c["high"]   for c in candles[-24:]),
            "low_24h":        min(c["low"]    for c in candles[-24:]),
            "volume_24h":     sum(c["volume"] for c in candles[-24:]),
        }

    def _collect_funding(self, pos):
        rate_data = self.funding_rates.get(pos.coin)
        if not rate_data:
            return
        rate = rate_data.get("rate", 0)
        if rate < CONFIG["funding_threshold"]:
            return
        last = self._last_funding.get(pos.coin)
        now  = datetime.now(ET)
        if last:
            hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600
            if hours < CONFIG["funding_interval_h"]:
                return
        income = round(pos.size_usd * rate, 4)
        self.wallet.add_funding(pos.coin, income, rate)
        self._last_funding[pos.coin] = now.isoformat()
        log.info("  [FUNDING/%s] +$%.4f (%.4f%%/8h)", pos.coin, income, rate * 100)

    # ── dashboard ────────────────────────────────────────────
    def _write_dashboard(self, prices, all_signals, trades_this_scan, portfolio):
        try:
            pnl = round(portfolio - CONFIG["paper_budget"], 4)
            roi = round(pnl / CONFIG["paper_budget"] * 100, 2)

            positions_out = []
            for pos in self.positions:
                price = prices.get(pos.coin, pos.entry_price)
                positions_out.append({
                    "coin":           pos.coin,
                    "entryPrice":     pos.entry_price,
                    "currentPrice":   price,
                    "sizeUsd":        pos.size_usd,
                    "entryTime":      pos.entry_time,
                    "peakPrice":      pos.peak_price,
                    "tpPrice":        pos.tp_price,
                    "slPrice":        pos.sl_price,
                    "trailingActive": pos.trailing_active,
                    "trailingStop":   pos.trailing_stop,
                    "unrealizedPnl":  pos.unrealized_pnl(price),
                    "unrealizedPct":  pos.unrealized_pct(price),
                    "gridLevel":      pos.grid_level_idx,
                })

            signals_out = {}
            for coin, sig in all_signals.items():
                signals_out[coin] = {
                    "price":         sig["price"],
                    "ma50":          sig["ma50"],
                    "rsi":           sig["rsi"],
                    "trend":         sig["trend"],
                    "trendStrength": sig["trend_strength"],
                    "aboveMa":       sig["above_ma"],
                    "buySignal":     sig["rsi"] < CONFIG["rsi_buy_max"] if sig.get("rsi") else False,
                    "sellSignal":    sig["rsi"] > CONFIG["rsi_sell_min"] if sig.get("rsi") else False,
                    "high24h":       sig["high_24h"],
                    "low24h":        sig["low_24h"],
                    "volume24h":     sig["volume_24h"],
                }

            grids_out = []
            for coin, g in self.grids.items():
                price = prices.get(coin)
                coin_positions = self._open_for_coin(coin)
                grids_out.append({
                    "coin":       coin,
                    "center":     g.center,
                    "levels":     g.levels,
                    "openCount":  len(coin_positions),
                    "spacing":    f"{CONFIG['grid_spacing_pct']*100:.1f}%",
                })

            funding_out = {}
            for coin, fd in self.funding_rates.items():
                rate = fd.get("rate", 0)
                funding_out[coin] = {
                    "rate":    rate,
                    "apy":     round(rate * 3 * 365 * 100, 2),
                    "notable": rate >= CONFIG["funding_threshold"],
                }

            if trades_this_scan > 0 and any(
                t["action"] == "SELL" for t in self.wallet.trade_log[-max(trades_this_scan, 1):]
            ):
                mood = "exiting"
            elif trades_this_scan > 0:
                mood = "hunting"
            elif self.positions:
                mood = "watching"
            else:
                any_bull = any(s["trend"] == "bullish" for s in all_signals.values())
                mood = "watching" if any_bull else "sleeping"

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "version":           "v10",
                    "lastScan":          datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":              "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":              mood,
                    "quote":             random.choice(QUOTES.get(mood, QUOTES["watching"])),
                    "cash":              round(self.wallet.cash, 2),
                    "portfolioValue":    portfolio,
                    "openPositionValue": round(portfolio - self.wallet.cash, 2),
                    "portfolioPnl":      pnl,
                    "portfolioRoi":      roi,
                    "totalPnl":          round(self.wallet.total_pnl, 4),
                    "dailyPnl":          round(self.wallet.daily_pnl, 4),
                    "fundingIncome":     round(self.wallet.funding_income, 4),
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
                    "startingBudget":    CONFIG["paper_budget"],
                    "circuitBreaker":    self.wallet.circuit_breaker_active,
                    "peakPortfolio":     round(self.wallet.peak_portfolio, 2),
                    "drawdownPct":       round(
                        (self.wallet.peak_portfolio - portfolio) / self.wallet.peak_portfolio * 100, 2
                    ) if self.wallet.peak_portfolio > 0 else 0,
                    "cashFloorPct":      CONFIG["cash_floor_pct"] * 100,
                    "rsiConfig":         {
                        "buyMax":  CONFIG["rsi_buy_max"],
                        "sellMin": CONFIG["rsi_sell_min"],
                        "boost":   CONFIG["rsi_boost_threshold"],
                    },
                    "tpPct":             CONFIG["take_profit_pct"] * 100,
                    "slPct":             CONFIG["stop_loss_pct"] * 100,
                    "positions":         positions_out,
                    "signals":           signals_out,
                    "grids":             grids_out,
                    "fundingRates":      funding_out,
                    "recentTrades":      self.wallet.trade_log[-30:],
                    "walletHistory":     self.wallet.wallet_history[-500:],
                    "dailyHistory":      self._daily_history,
                    "prices":            {c: round(p, 6) for c, p in prices.items()},
                }, f, indent=2)
        except Exception as e:
            log.warning("Dashboard write failed: %s", e)

    # ── main scan ────────────────────────────────────────────
    def run_once(self):
        self.scan_count += 1
        self.wallet.reset_daily()

        log.info("=" * 64)
        log.info("  SERAPHINA v10 #%d | %s | cash=$%.2f | pnl=$%+.2f | open=%d",
                 self.scan_count,
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                 self.wallet.cash, self.wallet.total_pnl, len(self.positions))
        log.info("=" * 64)

        # ── 1. Fetch candles + compute indicators ──────────────
        all_signals = {}
        prices      = {}
        for coin in CONFIG["coins"]:
            sig = self._analyze(coin)
            if sig:
                all_signals[coin] = sig
                prices[coin]      = sig["price"]
                log.info("  %s $%.4f | MA50=$%.4f | RSI=%.1f | %s",
                         coin, sig["price"], sig["ma50"], sig["rsi"],
                         sig["trend"].upper())
            else:
                log.warning("  %s: candle/analysis failed", coin)
        self.signals = all_signals

        # ── 2. Fetch funding rates ─────────────────────────────
        for coin in CONFIG["coins"]:
            fd = self.funding_api.fetch(coin)
            if fd:
                self.funding_rates[coin] = fd

        # ── 3. Portfolio + circuit breaker ─────────────────────
        portfolio = self._portfolio_value(prices)
        self.wallet.update_peak(portfolio)

        # ── 4. Init / rebuild grids ────────────────────────────
        for coin, price in prices.items():
            if coin not in self.grids:
                self.grids[coin] = Grid(coin, price)
                self.prev_prices[coin] = price * 0.9999
            elif self.grids[coin].drifted(price):
                log.info("  [GRID/%s] Drifted >%.0f%% from center — rebuilding",
                         coin, CONFIG["grid_drift_pct"] * 100)
                # Keep occupied set in sync with open positions
                self.grids[coin] = Grid(coin, price)
                self.prev_prices[coin] = price * 0.9999
            # Sync occupied set from actual open positions
            self.grids[coin].occupied = {
                p.grid_level_idx for p in self.positions
                if p.coin == coin and p.grid_level_idx is not None
            }

        trades_this_scan = 0

        # ── 5. Check exits (TP / SL / trail / RSI overbought) ──
        for pos in list(self.positions):
            price = prices.get(pos.coin)
            if not price:
                continue
            should_exit, reason = pos.check_exit(price)
            # RSI overbought sell — only close if profitable
            if not should_exit:
                sig = all_signals.get(pos.coin)
                if sig and sig["rsi"] > CONFIG["rsi_sell_min"] and pos.unrealized_pnl(price) > 0:
                    should_exit, reason = True, "RSI_OB"
            if should_exit:
                pnl = self.wallet.sell(pos.coin, price, pos, reason or "")
                self.positions.remove(pos)
                trades_this_scan += 1
                log.info("  [EXIT/%s] %s | $%.4f | pnl=$%+.4f | cash=$%.2f",
                         pos.coin, reason, price, pnl, self.wallet.cash)

        # ── 6. Collect funding ─────────────────────────────────
        for pos in list(self.positions):
            self._collect_funding(pos)

        # ── 7. Grid crossings → entries ────────────────────────
        for coin, price in prices.items():
            if coin not in self.grids:
                continue
            g    = self.grids[coin]
            prev = self.prev_prices.get(coin, price)
            sig  = all_signals.get(coin, {})
            rsi      = sig.get("rsi")
            above_ma = sig.get("above_ma", True)

            if prev == price:
                self.prev_prices[coin] = price
                continue

            # Check RSI gate first — skip all buys if overbought
            if rsi is not None and rsi > CONFIG["rsi_buy_max"]:
                log.info("  [GRID/%s] RSI=%.1f > %.0f — skipping buys this scan",
                         coin, rsi, CONFIG["rsi_buy_max"])
                self.prev_prices[coin] = price
                continue

            # Safety checks
            if self.wallet.circuit_breaker_active:
                self.prev_prices[coin] = price
                continue
            if self.wallet.daily_loss_hit():
                self.prev_prices[coin] = price
                continue

            buy_idxs = g.find_buy_crossings(prev, price)
            for idx in buy_idxs:
                coin_open = self._open_for_coin(coin)
                if len(coin_open) >= CONFIG["max_open_per_coin"]:
                    log.info("  [GRID/%s] Max per coin (%d) — skip", coin, CONFIG["max_open_per_coin"])
                    break
                if len(self.positions) >= CONFIG["max_open_total"]:
                    log.info("  [GRID] Max total (%d) — skip", CONFIG["max_open_total"])
                    break
                floor = portfolio * CONFIG["cash_floor_pct"]
                if self.wallet.cash < floor:
                    log.info("  [GRID/%s] Cash floor $%.2f — skip", coin, floor)
                    break
                size = self.wallet.trade_size(portfolio, rsi=rsi, above_ma=above_ma)
                if size > self.wallet.cash:
                    break
                lvl = g.levels[idx]
                self.wallet.buy(coin, lvl, size)
                pos = Position(coin, lvl, size, grid_level_idx=idx)
                self.positions.append(pos)
                g.occupied.add(idx)
                trades_this_scan += 1
                log.info("  [GRID/%s] BUY level $%.4f | RSI=%.1f | %s | size=$%.2f | cash=$%.2f",
                         coin, lvl, rsi if rsi else 0,
                         "UPTREND" if above_ma else "downtrend",
                         size, self.wallet.cash)

            self.prev_prices[coin] = price

        # ── 8. Save + dashboard ────────────────────────────────
        portfolio = self._portfolio_value(prices)
        self.wallet.wallet_history.append({"t": datetime.now(ET).isoformat(), "v": portfolio})
        if len(self.wallet.wallet_history) > 500:
            self.wallet.wallet_history = self.wallet.wallet_history[-500:]
        self._save_state()
        self._save_daily_history(portfolio)
        self._write_dashboard(prices, all_signals, trades_this_scan, portfolio)

        log.info("  Scan done | trades=%d | open=%d | cash=$%.2f | pnl=$%+.2f\n",
                 trades_this_scan, len(self.positions), self.wallet.cash, self.wallet.total_pnl)

    # ── loop ─────────────────────────────────────────────────
    def run_loop(self):
        log.info("Seraphina v10 starting | mode=%s | budget=$%.0f | coins=%s",
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 CONFIG["paper_budget"], ", ".join(CONFIG["coins"]))
        log.info("Grid %.1f%% spacing | RSI buy<%.0f sell>%.0f | boost<%.0f | MA50 trend | maker fees",
                 CONFIG["grid_spacing_pct"] * 100,
                 CONFIG["rsi_buy_max"], CONFIG["rsi_sell_min"],
                 CONFIG["rsi_boost_threshold"])
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("Seraphina rests. Goodbye.")
                break
            except Exception as e:
                log.error("Unexpected error: %s", e, exc_info=True)
            log.info("Sleeping %ds...\n", CONFIG["scan_interval_sec"])
            time.sleep(CONFIG["scan_interval_sec"])


if __name__ == "__main__":
    bot = SeraphinaBot()
    if "--once" in sys.argv:
        bot.run_once()
    else:
        bot.run_loop()
