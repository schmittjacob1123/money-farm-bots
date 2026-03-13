"""
╔══════════════════════════════════════════════════════════════╗
║         SERAPHINA'S CRYPTO ENGINE v7.0 — GRID TRADER        ║
║  Strategy: Automated grid trading on BTC, ETH, SOL, DOGE    ║
║  Places buy/sell orders at fixed price intervals.            ║
║  Profits from volatility — no direction prediction needed.   ║
╚══════════════════════════════════════════════════════════════╝

CHANGES v7 vs v6:
  - Momentum filter: skip buying if price dropped >1.5% over last 5 scans
    (falling knife protection — don't catch a falling blade)
  - Reversal detector: if price was falling but ticks up 2+ scans in a row,
    re-enable buying with a small size boost (catch the bounce)
  - Cash floor: never deploy new buys if cash < 25% of portfolio value
    (Jacob's salary buffer — can withdraw without disrupting the bot)
  - Daily history: permanent seraphina_daily.json — one snapshot per day,
    kept for 365 days, used by dashboard for all-time chart

CHANGES v6 vs v5:
  - Trailing momentum exit: once a position is up ≥trail_activate_pct
    from entry, arm a trailing stop. If price then drops ≥trail_stop_pct
    from its peak, sell immediately.
  - peak_price tracked per open buy, updated every scan

CHANGES v5 vs v4:
  - max_open_per_coin: 4 → 2
  - max_open_total: 12 → 8
  - prefill_levels: 2 → 1
  - grid_drift_pct: 5% → 3%
  - Drift rebuild now CLOSES open positions at current price first
  - Sweep sells: sell ALL profitable positions on upward move

HOW IT WORKS:
  On startup, Seraphina fetches the current price of each coin.
  She builds a grid of price levels above and below the current price.
  Every scan she checks if price has crossed a grid line:
    - Crossed DOWN through a level → BUY (pick up cheap coins)
    - Crossed UP through a level   → SELL (take profit)
  Plus: trailing stop fires if a good gain starts reversing.
  Plus: momentum filter blocks buys during falling knives.
  More volatility = more crossings = more profit.

WALLET ACCOUNTING:
  - Wallet starts at paper_budget
  - BUY:  wallet -= trade_size
  - SELL: wallet += trade_size + profit
  - daily_pnl tracks REALIZED profit/loss from SELLs only

NO AI CALLS. No Anthropic credits used. Pure math.
"""

import os, csv, json, logging, time, sys, random
from collections import deque
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

from dotenv import load_dotenv
load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
CONFIG = {
    "dry_run":          os.getenv("DRY_RUN", "true").lower() != "false",
    "paper_budget":     1000.0,

    "coins":            ["BTC", "ETH", "SOL", "DOGE"],

    "grid_levels":      8,
    "grid_spacing_pct": 0.010,

    "trade_size_pct":   0.08,
    "trade_size_min":   1.0,
    "trade_size_max":   100.0,

    "streak_tiers": {
        0:  0.08,
        3:  0.10,
        6:  0.12,
        10: 0.14,
    },

    "vol_min_readings":  20,
    "vol_hot_threshold": 2.5,
    "vol_cold_threshold": 0.8,

    "prefill_levels":   1,

    "max_open_per_coin": 2,
    "max_open_total":   8,

    "daily_loss_cap":       20.0,
    "grid_drift_pct":       0.03,
    "drawdown_pause_pct":   0.15,
    "drawdown_resume_pct":  0.08,
    "coin_stoploss_pct":    0.20,

    # ── Trailing momentum exit (v6) ──
    "trail_activate_pct": 0.005,
    "trail_stop_pct":     0.004,

    # ── Cash floor (v7) ──
    "cash_floor_pct":     0.25,   # never deploy if cash < 25% of portfolio value

    # ── Momentum filter (v7) ──
    "momentum_lookback":  5,      # scans to look back
    "momentum_drop_pct":  0.015,  # skip buy if price dropped >1.5% over lookback
    "reversal_ticks":     2,      # consecutive up-ticks needed to re-enable buying
    "reversal_size_mult": 1.2,    # size multiplier on confirmed reversal

    # ── Daily history (v7) ──
    "daily_history_file": "seraphina_daily.json",

    "scan_interval_sec": 15,

    "log_file":       "seraphina.log",
    "csv_file":       "seraphina_trades.csv",
    "state_file":     "seraphina_state.json",
    "dashboard_file": "seraphina_data.json",
}

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CONFIG["log_file"], encoding="utf-8")]
)
log = logging.getLogger("seraphina")
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
    "hunting":  ["Grid crossed. Profit locked.", "Another level hit. Another dollar earned.",
                 "Buy the dip. Sell the rip. Repeat.", "Volatility is free money if you're positioned right."],
    "watching": ["Grids are set. Waiting for the market.", "Price will move. It always moves. I'll be ready.",
                 "Patience is a strategy.", "Watching the levels."],
    "sleeping": ["Low volatility. The grid is patient.", "Quiet market. My orders are still there.",
                 "Nothing to do but wait."],
}
ART = {
    "hunting":  ["  /\\ /\\\n (=^.^=) $\n  (   )\n  -\"-\"-"],
    "watching": ["  /\\ /\\\n (=^.^=)\n  (   )\n  -\"-\"-"],
    "sleeping": ["  /\\ /\\\n (=-.-=)\n  (   )\n  zz-\"-\"-"],
}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: PRICE FETCHER
# ══════════════════════════════════════════════════════════════════════════════
class PriceFetcher:
    KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"
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
            r = requests.get(self.KRAKEN_URL, params={"pair": pair}, timeout=8)
            r.raise_for_status()
            res = r.json().get("result", {})
            if not res:
                return None
            t = list(res.values())[0]
            bid = float(t["b"][0])
            ask = float(t["a"][0])
            return (bid + ask) / 2
        except Exception as e:
            log.debug(f"Price fetch {coin}: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2: GRID
# ══════════════════════════════════════════════════════════════════════════════
class Grid:
    def __init__(self, coin, center, spacing_mult=1.0):
        self.coin         = coin
        self.center       = center
        self.spacing_mult = spacing_mult
        self.created_at   = datetime.now(ET).isoformat()
        self.levels       = self._build(center, spacing_mult)
        self.open_buys    = {}
        log.info(f"  [GRID/{coin}] New grid | center=${center:,.4f} | "
                 f"{len(self.levels)} levels | spacing={CONFIG['grid_spacing_pct']*spacing_mult*100:.2f}%")

    def _build(self, center, spacing_mult=1.0):
        n  = CONFIG["grid_levels"]
        sp = CONFIG["grid_spacing_pct"] * spacing_mult
        dec = 2 if center >= 10 else 4 if center >= 0.1 else 6
        levels = sorted(set(
            round(center * (1 + i * sp), dec)
            for i in range(-n, n + 1)
        ))
        return levels

    def drifted(self, price):
        return abs(price - self.center) / self.center > CONFIG["grid_drift_pct"]

    def find_crossings(self, prev, curr):
        lo, hi = min(prev, curr), max(prev, curr)
        results = []
        for i, lvl in enumerate(self.levels):
            if not (lo <= lvl <= hi):
                continue
            if curr < prev:
                results.append(("BUY", i, lvl))
            else:
                results.append(("SELL", i, lvl))
        return results

    def check_trailing_stops(self, curr_price):
        activate = CONFIG["trail_activate_pct"]
        trail    = CONFIG["trail_stop_pct"]
        to_close = []

        for idx, buy in self.open_buys.items():
            buy_price = buy["buy_price"]
            if curr_price > buy.get("peak_price", buy_price):
                buy["peak_price"] = curr_price

            peak            = buy.get("peak_price", buy_price)
            gain_from_entry = (peak - buy_price) / buy_price
            drop_from_peak  = (peak - curr_price) / peak if peak > 0 else 0

            if gain_from_entry >= activate and drop_from_peak >= trail:
                log.info(
                    "  [TRAIL/%s] Stop armed & fired | entry=$%.4f | peak=$%.4f | "
                    "now=$%.4f | gain=%.2f%% | drop=%.2f%%",
                    self.coin, buy_price, peak, curr_price,
                    gain_from_entry * 100, drop_from_peak * 100
                )
                to_close.append(idx)

        return to_close

    def record_buy(self, idx, price, size_usd):
        self.open_buys[idx] = {
            "buy_price":  price,
            "size_usd":   size_usd,
            "bought_at":  datetime.now(ET).isoformat(),
            "peak_price": price,
        }

    def record_sell(self, buy_idx):
        self.open_buys.pop(buy_idx, None)

    def prefill(self, current_price, n_levels):
        candidates = [(i, lvl) for i, lvl in enumerate(self.levels)
                      if lvl <= current_price * 1.0005]
        candidates.sort(key=lambda x: abs(x[1] - current_price))
        filled = []
        for i, lvl in candidates[:n_levels]:
            if i not in self.open_buys:
                filled.append((i, lvl))
        return filled

    def to_dict(self):
        return {
            "coin":         self.coin,
            "center":       self.center,
            "spacing_mult": self.spacing_mult,
            "created_at":   self.created_at,
            "levels":       self.levels,
            "open_buys":    {str(k): v for k, v in self.open_buys.items()},
        }

    @classmethod
    def from_dict(cls, d):
        g = cls.__new__(cls)
        g.coin         = d["coin"]
        g.center       = d.get("center", d.get("center_price", 0))
        g.spacing_mult = d.get("spacing_mult", 1.0)
        g.created_at   = d.get("created_at", "")
        g.levels       = d["levels"]
        g.open_buys    = {int(k): v for k, v in d.get("open_buys", {}).items()}
        for buy in g.open_buys.values():
            if "peak_price" not in buy:
                buy["peak_price"] = buy["buy_price"]
        return g


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: WALLET
# ══════════════════════════════════════════════════════════════════════════════
class Wallet:
    def __init__(self):
        self.cash                  = CONFIG["paper_budget"]
        self.total_pnl             = 0.0
        self.daily_pnl             = 0.0
        self.win_streak            = 0
        self.last_date             = datetime.now(ET).date().isoformat()
        self.trade_log             = []
        self.wallet_history        = []
        self.peak_portfolio        = CONFIG["paper_budget"]
        self.circuit_breaker_active = False

    def reset_daily_if_needed(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_date:
            self.daily_pnl = 0.0
            self.last_date = today
            log.info("[WALLET] Daily P&L reset")

    def buy(self, coin, price, size_usd):
        size_usd = min(size_usd, self.cash)
        size_usd = round(size_usd, 2)
        self.cash = round(self.cash - size_usd, 4)
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "BUY", "price": price, "size": size_usd,
            "pnl": 0, "cash": round(self.cash, 2),
        })
        return size_usd

    def sell(self, coin, price, size_usd, profit_usd):
        returned = round(size_usd + profit_usd, 4)
        self.cash       = round(self.cash + returned, 4)
        self.total_pnl  = round(self.total_pnl + profit_usd, 4)
        self.daily_pnl  = round(self.daily_pnl + profit_usd, 4)
        if profit_usd > 0:
            self.win_streak += 1
        else:
            self.win_streak = 0
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "SELL", "price": price, "size": size_usd,
            "pnl": round(profit_usd, 4), "cash": round(self.cash, 2),
        })
        return returned

    def prefill_buy(self, coin, price, size_usd):
        size_usd = min(size_usd, self.cash)
        size_usd = round(size_usd, 2)
        self.cash = round(self.cash - size_usd, 4)
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "BUY", "price": price, "size": size_usd,
            "pnl": 0, "cash": round(self.cash, 2), "prefill": True,
        })
        return size_usd

    def daily_loss_cap_hit(self):
        return self.daily_pnl < -CONFIG["daily_loss_cap"]

    def update_peak(self, portfolio_value):
        if portfolio_value > self.peak_portfolio:
            self.peak_portfolio = portfolio_value
        drawdown = (self.peak_portfolio - portfolio_value) / self.peak_portfolio
        if not self.circuit_breaker_active and drawdown >= CONFIG["drawdown_pause_pct"]:
            self.circuit_breaker_active = True
            log.warning("[CIRCUIT BREAKER] Portfolio down %.1f%% from peak $%.2f — PAUSING ALL NEW BUYS",
                        drawdown * 100, self.peak_portfolio)
        elif self.circuit_breaker_active and drawdown <= CONFIG["drawdown_resume_pct"]:
            self.circuit_breaker_active = False
            log.info("[CIRCUIT BREAKER] Portfolio recovered to within %.1f%% of peak — RESUMING",
                     drawdown * 100)
        return drawdown

    def size_pct(self):
        pct = 0.08
        for threshold in sorted(CONFIG["streak_tiers"]):
            if self.win_streak >= threshold:
                pct = CONFIG["streak_tiers"][threshold]
        return pct

    def trade_size(self, wallet_cash):
        raw = wallet_cash * self.size_pct()
        return round(max(CONFIG["trade_size_min"],
                         min(CONFIG["trade_size_max"], raw)), 2)

    def win_rate(self):
        sells = [t for t in self.trade_log if t["action"] == "SELL"]
        if not sells:
            return 0.0
        wins = sum(1 for t in sells if t["pnl"] > 0)
        return round(wins / len(sells) * 100, 1)

    def to_dict(self):
        return {
            "cash":                   round(self.cash, 4),
            "total_pnl":              round(self.total_pnl, 4),
            "daily_pnl":              round(self.daily_pnl, 4),
            "win_streak":             self.win_streak,
            "last_date":              self.last_date,
            "trade_log":              self.trade_log[-200:],
            "wallet_history":         self.wallet_history[-500:],
            "peak_portfolio":         round(self.peak_portfolio, 4),
            "circuit_breaker_active": self.circuit_breaker_active,
        }

    def load_dict(self, d):
        self.cash                   = d.get("cash", CONFIG["paper_budget"])
        self.total_pnl              = d.get("total_pnl", 0.0)
        self.daily_pnl              = d.get("daily_pnl", 0.0)
        self.win_streak             = d.get("win_streak", 0)
        self.last_date              = d.get("last_date", datetime.now(ET).date().isoformat())
        self.trade_log              = d.get("trade_log", [])
        self.wallet_history         = d.get("wallet_history", [])
        self.peak_portfolio         = d.get("peak_portfolio", CONFIG["paper_budget"])
        self.circuit_breaker_active = d.get("circuit_breaker_active", False)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: STATE
# ══════════════════════════════════════════════════════════════════════════════
class State:
    @staticmethod
    def save(wallet, grids):
        try:
            with open(CONFIG["state_file"], "w") as f:
                json.dump({
                    "wallet": wallet.to_dict(),
                    "grids":  {coin: g.to_dict() for coin, g in grids.items()},
                }, f, indent=2)
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
# MAIN BOT
# ══════════════════════════════════════════════════════════════════════════════
class SeraphinaBot:

    def __init__(self):
        self.fetcher         = PriceFetcher()
        self.wallet          = Wallet()
        self.grids           = {}
        self.prev_prices     = {}
        self.price_hist      = {}
        self.scan_count      = 0
        # v7 momentum state
        self.momentum_hist   = {}   # coin → deque of recent prices
        self.reversal_count  = {}   # coin → consecutive up-ticks
        self.momentum_paused = {}   # coin → bool
        self._daily_history  = []
        self._load_state()
        self._load_daily_history()

    # ── Daily history (v7) ──────────────────────────────────────────────────

    def _load_daily_history(self):
        try:
            with open(CONFIG["daily_history_file"]) as f:
                self._daily_history = json.load(f)
            log.info("[INIT] Loaded daily history: %d days", len(self._daily_history))
        except:
            self._daily_history = []

    def _save_daily_history(self, portfolio_value):
        today = datetime.now(ET).date().isoformat()
        if self._daily_history and self._daily_history[-1]["d"] == today:
            self._daily_history[-1]["v"] = round(portfolio_value, 2)
        else:
            self._daily_history.append({"d": today, "v": round(portfolio_value, 2)})
        self._daily_history = self._daily_history[-365:]
        try:
            with open(CONFIG["daily_history_file"], "w") as f:
                json.dump(self._daily_history, f)
        except Exception as e:
            log.warning(f"Daily history save failed: {e}")

    # ── Momentum filter (v7) ────────────────────────────────────────────────

    def _momentum_ok_to_buy(self, coin, curr_price):
        """
        Returns (ok_to_buy: bool, size_multiplier: float).
        Blocks buys during falling knives, boosts size on confirmed reversals.
        """
        hist = self.momentum_hist.setdefault(coin, deque(maxlen=CONFIG["momentum_lookback"] + 1))
        hist.append(curr_price)

        # Not enough data yet — allow freely
        if len(hist) < CONFIG["momentum_lookback"]:
            return True, 1.0

        oldest = hist[0]
        drop_pct = (oldest - curr_price) / oldest  # positive = price fell

        # Count consecutive up-ticks
        prices_list = list(hist)
        ticks = 0
        for i in range(len(prices_list) - 1, 0, -1):
            if prices_list[i] > prices_list[i - 1]:
                ticks += 1
            else:
                break
        self.reversal_count[coin] = ticks

        was_paused = self.momentum_paused.get(coin, False)

        if drop_pct >= CONFIG["momentum_drop_pct"]:
            # Falling knife — pause
            if not was_paused:
                log.info("  [MOMENTUM/%s] Falling knife (%.2f%% drop over %d scans) — pausing buys",
                         coin, drop_pct * 100, CONFIG["momentum_lookback"])
            self.momentum_paused[coin] = True
            return False, 1.0

        if was_paused:
            if ticks >= CONFIG["reversal_ticks"]:
                # Confirmed reversal — resume with size boost
                self.momentum_paused[coin] = False
                log.info("  [MOMENTUM/%s] Reversal confirmed (%d up-ticks) — resuming with %.1fx size",
                         coin, ticks, CONFIG["reversal_size_mult"])
                return True, CONFIG["reversal_size_mult"]
            else:
                # Still waiting for reversal confirmation
                return False, 1.0

        return True, 1.0

    # ── Cash floor (v7) ─────────────────────────────────────────────────────

    def _cash_floor_ok(self, portfolio_value):
        """Block buying if cash < 25% of portfolio (Jacob's salary buffer)."""
        floor = portfolio_value * CONFIG["cash_floor_pct"]
        if self.wallet.cash < floor:
            log.info("  [CASH FLOOR] Cash $%.2f < floor $%.2f (%.0f%% of $%.2f) — skip buy",
                     self.wallet.cash, floor, CONFIG["cash_floor_pct"] * 100, portfolio_value)
            return False
        return True

    # ────────────────────────────────────────────────────────────────────────

    def _load_state(self):
        data = State.load()
        if not data:
            log.info("[INIT] No state file — fresh start at $%.2f", CONFIG["paper_budget"])
            return
        self.wallet.load_dict(data.get("wallet", {}))
        log.info("[INIT] Loaded wallet: cash=$%.2f | total_pnl=$%+.2f | streak=%dW",
                 self.wallet.cash, self.wallet.total_pnl, self.wallet.win_streak)
        for coin, gd in data.get("grids", {}).items():
            g = Grid.from_dict(gd)
            self.grids[coin] = g
            log.info("[INIT] Restored grid %s | center=$%.4f | %d open buys",
                     coin, g.center, len(g.open_buys))

    def _vol_regime(self, coin, price):
        hist = self.price_hist.setdefault(coin, [])
        hist.append(price)
        if len(hist) > 120:
            hist = hist[-120:]
        self.price_hist[coin] = hist

        if len(hist) < CONFIG["vol_min_readings"]:
            return "normal", 1.0, 1.0

        swing = (max(hist) - min(hist)) / min(hist) * 100
        if swing >= CONFIG["vol_hot_threshold"]:
            return "hot",    0.7, 1.2
        elif swing <= CONFIG["vol_cold_threshold"]:
            return "cold",   1.8, 0.8
        return "normal", 1.0, 1.0

    def _new_grid(self, coin, price, spacing_mult=1.0):
        g = Grid(coin, price, spacing_mult)
        n = CONFIG["prefill_levels"]
        to_fill = g.prefill(price, n)
        for idx, lvl in to_fill:
            if self.wallet.cash < CONFIG["trade_size_min"]:
                break
            size = self.wallet.trade_size(self.wallet.cash)
            actual = self.wallet.prefill_buy(coin, lvl, size)
            g.record_buy(idx, lvl, actual)
            log.info("  [GRID/%s] PREFILL buy | level=$%.4f | size=$%.2f | cash=$%.2f",
                     coin, lvl, actual, self.wallet.cash)
        self.grids[coin] = g
        self.prev_prices[coin] = price * 0.9999

    def _portfolio_value(self, prices):
        pos_value = 0.0
        for coin, g in self.grids.items():
            curr = prices.get(coin)
            if not curr:
                continue
            for buy in g.open_buys.values():
                pos_value += buy["size_usd"] * (curr / buy["buy_price"])
        return round(self.wallet.cash + pos_value, 2)

    def _write_dashboard(self, prices, mood, trades_this_scan, vol_regimes, portfolio_value_now=None):
        try:
            if portfolio_value_now is None:
                open_pos = sum(sum(b["size_usd"] for b in g.open_buys.values()) for g in self.grids.values())
                portfolio_value_now = round(self.wallet.cash + open_pos, 2)
            portfolio_value = self._portfolio_value(prices)
            portfolio_pnl   = round(portfolio_value - CONFIG["paper_budget"], 4)
            portfolio_roi   = round(portfolio_pnl / CONFIG["paper_budget"] * 100, 2)

            self.wallet.wallet_history.append({
                "t": datetime.now(ET).isoformat(),
                "v": portfolio_value,
            })
            if len(self.wallet.wallet_history) > 500:
                self.wallet.wallet_history = self.wallet.wallet_history[-500:]

            grids_out = []
            for coin, g in self.grids.items():
                curr = prices.get(coin)
                buys_list = []
                for k, v in g.open_buys.items():
                    live_diff     = round(curr - v["buy_price"], 4) if curr else 0
                    live_diff_pct = round(live_diff / v["buy_price"] * 100, 2) if curr and v["buy_price"] else 0
                    buys_list.append({
                        "level":       g.levels[int(k)],
                        "buyPrice":    v["buy_price"],
                        "sizeUsd":     v["size_usd"],
                        "boughtAt":    v["bought_at"],
                        "livePrice":   curr,
                        "liveDiff":    live_diff,
                        "liveDiffPct": live_diff_pct,
                        "coin":        coin,
                        "peakPrice":   v.get("peak_price", v["buy_price"]),
                    })
                grids_out.append({
                    "coin":         coin,
                    "centerPrice":  g.center,
                    "currentPrice": curr,
                    "levels":       g.levels,
                    "openBuys":     len(g.open_buys),
                    "openBuysList": buys_list,
                    "spacing":      f"{CONFIG['grid_spacing_pct']*100:.1f}%",
                    "regime":       vol_regimes.get(coin, "normal"),
                    "momentumPaused": self.momentum_paused.get(coin, False),
                })

            # v7: momentum status for dashboard
            momentum_status = {
                coin: {
                    "paused":   self.momentum_paused.get(coin, False),
                    "upTicks":  self.reversal_count.get(coin, 0),
                }
                for coin in CONFIG["coins"]
            }

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "lastScan":        datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":            "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":            mood,
                    "quote":           random.choice(QUOTES.get(mood, QUOTES["watching"])),
                    "art":             random.choice(ART.get(mood, ART["watching"])),
                    "cash":            round(self.wallet.cash, 2),
                    "portfolioValue":  portfolio_value,
                    "openPositionValue": round(portfolio_value - self.wallet.cash, 2),
                    "portfolioPnl":    portfolio_pnl,
                    "portfolioRoi":    portfolio_roi,
                    "totalPnl":        round(self.wallet.total_pnl, 4),
                    "dailyPnl":        round(self.wallet.daily_pnl, 4),
                    "winRate":         self.wallet.win_rate(),
                    "winStreak":       self.wallet.win_streak,
                    "sizePct":         round(self.wallet.size_pct() * 100, 1),
                    "startingBudget":  CONFIG["paper_budget"],
                    "scanCount":       self.scan_count,
                    "tradesThisScan":  trades_this_scan,
                    "grids":           grids_out,
                    "recentTrades":    self.wallet.trade_log[-20:],
                    "walletHistory":   self.wallet.wallet_history[-500:],
                    "dailyHistory":    self._daily_history,
                    "volRegimes":      vol_regimes,
                    "momentumStatus":  momentum_status,
                    "circuitBreaker":  self.wallet.circuit_breaker_active,
                    "peakPortfolio":   round(self.wallet.peak_portfolio, 2),
                    "drawdownPct":     round((self.wallet.peak_portfolio - portfolio_value) / self.wallet.peak_portfolio * 100, 2) if self.wallet.peak_portfolio > 0 else 0,
                    "prices":          {c: round(p, 4) for c, p in prices.items() if p},
                    "cashFloorPct":    CONFIG["cash_floor_pct"] * 100,
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Dashboard write failed: {e}")

    def run_once(self):
        self.scan_count += 1
        self.wallet.reset_daily_if_needed()

        log.info("=" * 60)
        log.info("  SERAPHINA v7 #%d | %s | cash=$%.2f | total_pnl=$%+.2f | daily=$%+.2f | streak=%dW",
                 self.scan_count,
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                 self.wallet.cash, self.wallet.total_pnl,
                 self.wallet.daily_pnl, self.wallet.win_streak)
        log.info("=" * 60)

        # ── Step 1: Fetch prices ──
        prices = {}
        for coin in CONFIG["coins"]:
            p = self.fetcher.fetch(coin)
            if p:
                prices[coin] = p
                log.info("  %s: $%.4f", coin, p)
            else:
                log.warning("  %s: fetch failed", coin)

        # ── Step 2: Init / reset grids ──
        vol_regimes = {}
        for coin, price in prices.items():
            regime, spacing_mult, _ = self._vol_regime(coin, price)
            vol_regimes[coin] = regime

            if coin not in self.grids:
                log.info("  [GRID/%s] No grid — initialising", coin)
                self._new_grid(coin, price, spacing_mult)
            elif self.grids[coin].drifted(price):
                old_g = self.grids[coin]
                n_closed = 0
                for buy_idx, buy in list(old_g.open_buys.items()):
                    profit = round(buy["size_usd"] * (price - buy["buy_price"]) / buy["buy_price"], 4)
                    self.wallet.sell(coin, price, buy["size_usd"], profit)
                    sign = "+" if profit >= 0 else ""
                    log.info("  [GRID/%s] DRIFT CLOSE | bought=$%.4f | profit=%s$%.4f | cash=$%.2f",
                             coin, buy["buy_price"], sign, profit, self.wallet.cash)
                    n_closed += 1
                log.info("  [GRID/%s] Drifted >3%% — closed %d positions, rebuilding at $%.4f",
                         coin, n_closed, price)
                self._new_grid(coin, price, spacing_mult)

        # ── Step 3: Portfolio drawdown circuit breaker ──
        open_position_value = sum(
            sum(b["size_usd"] for b in g.open_buys.values())
            for g in self.grids.values()
        )
        portfolio_value_now = round(self.wallet.cash + open_position_value, 2)
        drawdown = self.wallet.update_peak(portfolio_value_now)

        # ── Step 3b: Per-coin stop loss ──
        for coin, price in list(prices.items()):
            if coin not in self.grids:
                continue
            g = self.grids[coin]
            if not g.open_buys:
                continue
            total_cost = sum(b["size_usd"] for b in g.open_buys.values())
            current_value = sum(
                b["size_usd"] * (price / b["buy_price"])
                for b in g.open_buys.values()
            )
            unrealized_loss_pct = (total_cost - current_value) / total_cost if total_cost > 0 else 0
            if unrealized_loss_pct >= CONFIG["coin_stoploss_pct"]:
                log.warning("[STOP LOSS] %s unrealized loss %.1f%% — closing %d open buys",
                            coin, unrealized_loss_pct * 100, len(g.open_buys))
                for buy_idx, buy in list(g.open_buys.items()):
                    loss = round(buy["size_usd"] * (price - buy["buy_price"]) / buy["buy_price"], 4)
                    self.wallet.sell(coin, price, buy["size_usd"], loss)
                    g.record_sell(buy_idx)
                    log.warning("  [STOP LOSS/%s] Closed buy @ $%.4f | loss=$%+.4f", coin, buy["buy_price"], loss)

        # ── Step 3c: Trailing stops (v6) ──
        trades_this_scan = 0
        total_open = sum(len(g.open_buys) for g in self.grids.values())

        for coin, price in prices.items():
            if coin not in self.grids:
                continue
            g = self.grids[coin]
            to_close = g.check_trailing_stops(price)
            for buy_idx in to_close:
                if buy_idx not in g.open_buys:
                    continue
                buy    = g.open_buys[buy_idx]
                profit = round(buy["size_usd"] * (price - buy["buy_price"]) / buy["buy_price"], 4)
                self.wallet.sell(coin, price, buy["size_usd"], profit)
                g.record_sell(buy_idx)
                total_open -= 1
                trades_this_scan += 1
                log.info("  [TRAIL/%s] SELL | $%.4f | bought=$%.4f | profit=$%+.4f | cash=$%.2f",
                         coin, price, buy["buy_price"], profit, self.wallet.cash)

        # ── Step 4: Check grid crossings ──
        for coin, price in prices.items():
            if coin not in self.grids:
                continue
            g    = self.grids[coin]
            prev = self.prev_prices.get(coin, price)

            if prev == price:
                log.info("  [GRID/%s] No price change — skip", coin)
                continue

            regime, spacing_mult, size_mult = self._vol_regime(coin, price)

            last_regime = getattr(g, "_last_regime", regime)
            if last_regime != regime:
                log.info("  [GRID/%s] Regime shift %s->%s — noted", coin, last_regime, regime)
            g._last_regime = regime

            crossings = g.find_crossings(prev, price)

            for action, idx, lvl in crossings:
                if action == "BUY":
                    if self.wallet.circuit_breaker_active:
                        log.info("  [GRID/%s] Circuit breaker active — skip buy", coin)
                        continue
                    if len(g.open_buys) >= CONFIG["max_open_per_coin"]:
                        log.info("  [GRID/%s] Max open per coin reached — skip buy", coin)
                        continue
                    if total_open >= CONFIG["max_open_total"]:
                        log.info("  [GRID/%s] Max open total reached — skip buy", coin)
                        continue
                    if self.wallet.daily_loss_cap_hit():
                        log.info("  [GRID/%s] Daily loss cap hit — skip buy", coin)
                        continue
                    if idx in g.open_buys:
                        continue

                    # v7: momentum filter
                    mom_ok, mom_mult = self._momentum_ok_to_buy(coin, price)
                    if not mom_ok:
                        continue

                    # v7: cash floor
                    if not self._cash_floor_ok(portfolio_value_now):
                        continue

                    effective_pct = min(0.14, self.wallet.size_pct() * size_mult * mom_mult)
                    raw = self.wallet.cash * effective_pct
                    size = round(max(CONFIG["trade_size_min"],
                                     min(CONFIG["trade_size_max"], raw)), 2)
                    if size > self.wallet.cash:
                        log.info("  [GRID/%s] Not enough cash — skip buy", coin)
                        continue

                    actual = self.wallet.buy(coin, lvl, size)
                    g.record_buy(idx, lvl, actual)
                    total_open += 1
                    trades_this_scan += 1
                    log.info("  [GRID/%s] BUY  | $%.4f | size=$%.2f | cash=$%.2f",
                             coin, lvl, actual, self.wallet.cash)

                elif action == "SELL":
                    buys_below = {k: v for k, v in g.open_buys.items()
                                  if g.levels[k] < lvl}
                    if not buys_below:
                        continue
                    for buy_idx, buy in list(buys_below.items()):
                        profit = round(buy["size_usd"] * (lvl - buy["buy_price"]) / buy["buy_price"], 4)
                        self.wallet.sell(coin, lvl, buy["size_usd"], profit)
                        g.record_sell(buy_idx)
                        total_open -= 1
                        trades_this_scan += 1
                        log.info("  [GRID/%s] SELL | $%.4f | bought=$%.4f | profit=$%+.4f | cash=$%.2f",
                                 coin, lvl, buy["buy_price"], profit, self.wallet.cash)

            self.prev_prices[coin] = price
            log.info("  [GRID/%s] Price=$%.4f | open=%d | regime=%s | mom_paused=%s",
                     coin, price, len(g.open_buys), regime,
                     self.momentum_paused.get(coin, False))

        # ── Step 5: Save & dashboard ──
        State.save(self.wallet, self.grids)
        self._save_daily_history(portfolio_value_now)

        total_open_final = sum(len(g.open_buys) for g in self.grids.values())
        mood = "hunting" if trades_this_scan > 0 else ("watching" if total_open_final > 0 else "sleeping")
        log.info("\nScan done | trades=%d | open=%d | cash=$%.2f | total_pnl=$%+.2f\n",
                 trades_this_scan, total_open_final, self.wallet.cash, self.wallet.total_pnl)

        self._write_dashboard(prices, mood, trades_this_scan, vol_regimes, portfolio_value_now)

    def run_loop(self):
        log.info("Seraphina v7 starting | mode=%s | budget=$%.0f | coins=%s",
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 CONFIG["paper_budget"], ", ".join(CONFIG["coins"]))
        log.info("Grid: %d levels | %.1f%% spacing | %.0f%% base size | 15s scans | NO API CALLS",
                 CONFIG["grid_levels"], CONFIG["grid_spacing_pct"] * 100,
                 CONFIG["trade_size_pct"] * 100)
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
