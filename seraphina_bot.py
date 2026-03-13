"""
╔══════════════════════════════════════════════════════════════╗
║         SERAPHINA'S CRYPTO ENGINE v5.0 — GRID TRADER        ║
║  Strategy: Automated grid trading on BTC, ETH, SOL, DOGE    ║
║  Places buy/sell orders at fixed price intervals.            ║
║  Profits from volatility — no direction prediction needed.   ║
╚══════════════════════════════════════════════════════════════╝

CHANGES v5 vs v4:
  - max_open_per_coin: 4 → 2 (keeps more cash available to act)
  - max_open_total: 12 → 8 (leaner overall deployment)
  - prefill_levels: 2 → 1 (less capital locked at startup)
  - grid_drift_pct: 5% → 3% (recenters more aggressively)
  - Drift rebuild now CLOSES open positions at current price first
    (v4 silently abandoned them — cash accounting bug fixed)
  - Sweep sells: on upward move, sell ALL profitable positions
    at that level, not just the single lowest buy price one

HOW IT WORKS:
  On startup, Seraphina fetches the current price of each coin.
  She builds a grid of price levels above and below the current price.
  Every scan she checks if price has crossed a grid line:
    - Crossed DOWN through a level → BUY (pick up cheap coins)
    - Crossed UP through a level   → SELL (take profit)
  Profit = grid spacing % of trade size per round trip.
  More volatility = more crossings = more profit.

WALLET ACCOUNTING (simple and correct):
  - Wallet starts at paper_budget
  - BUY:  wallet -= trade_size  (cash leaves, position opens)
  - SELL: wallet += trade_size + profit  (cash returns + profit)
  - daily_pnl tracks REALIZED profit/loss from SELLs only
  - Prefill buys deduct from wallet but NOT from daily_pnl (deployment, not loss)

NO AI CALLS. No Anthropic credits used. Pure math.
"""

import os, csv, json, logging, time, sys, random
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

    # Coins to trade
    "coins":            ["BTC", "ETH", "SOL", "DOGE"],

    # Grid settings
    "grid_levels":      8,        # levels above AND below center (17 total)
    "grid_spacing_pct": 0.010,    # 1.0% between levels

    # Trade sizing
    "trade_size_pct":   0.08,     # 8% of wallet per trade (base)
    "trade_size_min":   1.0,
    "trade_size_max":   100.0,

    # Streak-based sizing tiers {min_streak: size_pct}
    "streak_tiers": {
        0:  0.08,
        3:  0.10,
        6:  0.12,
        10: 0.14,
    },

    # Volatility regime — needs 20 readings before triggering rebuild
    "vol_min_readings":  20,      # don't judge regime until we have this many readings
    "vol_hot_threshold": 2.5,     # >2.5% swing = Hot  → tighten grid
    "vol_cold_threshold": 0.8,    # <0.8% swing = Cold → widen grid

    # Hybrid prefill: buy N levels closest below current price on grid init
    "prefill_levels":   1,        # 1 per coin = lean start, more cash reserve

    # Position limits
    "max_open_per_coin": 2,       # max open buys per coin (keep powder dry)
    "max_open_total":   8,        # max open buys across all coins

    # Risk
    "daily_loss_cap":       20.0,  # stop buying if realized losses exceed $20 today
    "grid_drift_pct":       0.03,  # rebuild grid if price drifts 3% from center
    "drawdown_pause_pct":   0.15,  # pause ALL new buys if portfolio drops 15% from peak
    "drawdown_resume_pct":  0.08,  # resume when recovered to within 8% of peak
    "coin_stoploss_pct":    0.20,  # close all open buys for a coin if unrealized loss > 20%

    # Timing
    "scan_interval_sec": 15,

    # Files
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
    """
    Manages buy/sell levels for a single coin.
    Wallet accounting is handled OUTSIDE this class by the bot.
    This class only tracks grid state.
    """

    def __init__(self, coin, center, spacing_mult=1.0):
        self.coin         = coin
        self.center       = center
        self.spacing_mult = spacing_mult
        self.created_at   = datetime.now(ET).isoformat()
        self.levels       = self._build(center, spacing_mult)
        self.open_buys    = {}  # {level_idx: {buy_price, size_usd, bought_at}}
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
        """Return list of (action, level_idx, level_price) for levels crossed."""
        lo, hi = min(prev, curr), max(prev, curr)
        results = []
        for i, lvl in enumerate(self.levels):
            if not (lo <= lvl <= hi):
                continue
            if curr < prev:
                # price dropped through this level → potential BUY
                results.append(("BUY", i, lvl))
            else:
                # price rose through this level → potential SELL
                results.append(("SELL", i, lvl))
        return results

    def record_buy(self, idx, price, size_usd):
        self.open_buys[idx] = {
            "buy_price": price,
            "size_usd":  size_usd,
            "bought_at": datetime.now(ET).isoformat(),
        }

    def record_sell(self, buy_idx):
        self.open_buys.pop(buy_idx, None)

    def prefill(self, current_price, n_levels):
        """
        Place n_levels buys at levels closest to (and at/below) current price.
        Returns list of (idx, level_price) that were filled.
        Does NOT touch wallet — caller handles accounting.
        """
        candidates = [(i, lvl) for i, lvl in enumerate(self.levels)
                      if lvl <= current_price * 1.0005]  # at or just below
        # Sort by closest to current price first
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
        return g


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: WALLET  (single source of truth for all money)
# ══════════════════════════════════════════════════════════════════════════════
class Wallet:
    """
    Simple, correct accounting:
      buy(size)  → wallet -= size
      sell(size, profit) → wallet += size + profit
      daily_pnl tracks REALIZED profit/loss from sells only.
      Prefill is treated as deployment (wallet decreases, no daily_pnl impact).
    """

    def __init__(self):
        self.cash                  = CONFIG["paper_budget"]
        self.total_pnl             = 0.0   # realized profit from sells
        self.daily_pnl             = 0.0   # realized profit today from sells
        self.win_streak            = 0
        self.last_date             = datetime.now(ET).date().isoformat()
        self.trade_log             = []    # recent trades for dashboard
        self.wallet_history        = []    # portfolio value over time
        self.peak_portfolio        = CONFIG["paper_budget"]  # all-time high portfolio value
        self.circuit_breaker_active = False  # True = pause all new buys

    def reset_daily_if_needed(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_date:
            self.daily_pnl = 0.0
            self.last_date = today
            log.info("[WALLET] Daily P&L reset")

    def buy(self, coin, price, size_usd):
        """Deduct cost from wallet. Returns actual size used."""
        size_usd = min(size_usd, self.cash)  # can't spend more than we have
        size_usd = round(size_usd, 2)
        self.cash = round(self.cash - size_usd, 4)
        self.trade_log.append({
            "ts": datetime.now(ET).isoformat(), "coin": coin,
            "action": "BUY", "price": price, "size": size_usd,
            "pnl": 0, "cash": round(self.cash, 2),
        })
        return size_usd

    def sell(self, coin, price, size_usd, profit_usd):
        """Return cost + profit to wallet. Track realized PnL."""
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
        """Prefill: deduct from cash but don't count as daily loss."""
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
        """Only block buying if we've LOST more than the cap today (realized)."""
        return self.daily_pnl < -CONFIG["daily_loss_cap"]

    def update_peak(self, portfolio_value):
        """Track all-time high and manage circuit breaker."""
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
        """Streak-based trade size percentage."""
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
# MODULE 4: STATE (save/load)
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
        self.fetcher       = PriceFetcher()
        self.wallet        = Wallet()
        self.grids         = {}        # coin → Grid
        self.prev_prices   = {}        # coin → float
        self.price_hist    = {}        # coin → [float] for vol detection
        self.scan_count    = 0
        self._load_state()

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

        min_readings = CONFIG["vol_min_readings"]
        if len(hist) < min_readings:
            return "normal", 1.0, 1.0

        swing = (max(hist) - min(hist)) / min(hist) * 100
        if swing >= CONFIG["vol_hot_threshold"]:
            return "hot",    0.7, 1.2
        elif swing <= CONFIG["vol_cold_threshold"]:
            return "cold",   1.8, 0.8
        return "normal", 1.0, 1.0

    def _new_grid(self, coin, price, spacing_mult=1.0):
        """Create a fresh grid, run prefill, handle wallet deduction."""
        g = Grid(coin, price, spacing_mult)

        # Prefill: buy N levels closest below current price
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
        self.prev_prices[coin] = price * 0.9999  # nudge so first scan runs crossings

    def _portfolio_value(self, prices):
        """Cash + current market value of all open positions."""
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
                })

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
                    "volRegimes":      vol_regimes,
                    "circuitBreaker":  self.wallet.circuit_breaker_active,
                    "peakPortfolio":   round(self.wallet.peak_portfolio, 2),
                    "drawdownPct":     round((self.wallet.peak_portfolio - portfolio_value) / self.wallet.peak_portfolio * 100, 2) if self.wallet.peak_portfolio > 0 else 0,
                    "prices":          {c: round(p, 4) for c, p in prices.items() if p},
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Dashboard write failed: {e}")

    def run_once(self):
        self.scan_count += 1
        self.wallet.reset_daily_if_needed()

        log.info("=" * 60)
        log.info("  SERAPHINA v5 #%d | %s | cash=$%.2f | total_pnl=$%+.2f | daily=$%+.2f | streak=%dW",
                 self.scan_count,
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                 self.wallet.cash, self.wallet.total_pnl,
                 self.wallet.daily_pnl, self.wallet.win_streak)
        log.info("=" * 60)

        # ── Step 1: Fetch prices ──
        portfolio_value_now = round(self.wallet.cash + sum(
            sum(b["size_usd"] for b in g.open_buys.values())
            for g in self.grids.values()
        ), 2)  # pre-calculated so dashboard always has it
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
        # Always defined — calculated before any price-dependent logic
        open_position_value = sum(
            sum(b["size_usd"] for b in g.open_buys.values())
            for g in self.grids.values()
        )
        portfolio_value_now = round(self.wallet.cash + open_position_value, 2)
        drawdown = self.wallet.update_peak(portfolio_value_now)

        # ── Step 3b: Per-coin stop loss — close all buys if unrealized loss > 20% ──
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

        # ── Step 4: Check crossings ──
        trades_this_scan = 0
        total_open = sum(len(g.open_buys) for g in self.grids.values())

        for coin, price in prices.items():
            if coin not in self.grids:
                continue
            g    = self.grids[coin]
            prev = self.prev_prices.get(coin, price)

            if prev == price:
                log.info("  [GRID/%s] No price change — skip", coin)
                continue

            regime, spacing_mult, size_mult = self._vol_regime(coin, price)

            # Rebuild on regime shift — only after enough readings
            last_regime = getattr(g, "_last_regime", regime)
            if last_regime != regime:
                log.info("  [GRID/%s] Regime shift %s->%s — noted, no rebuild", coin, last_regime, regime)
            g._last_regime = regime

            crossings = g.find_crossings(prev, price)

            for action, idx, lvl in crossings:
                if action == "BUY":
                    # Guards
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
                        continue  # already have a buy here

                    # Size
                    effective_pct = min(0.14, self.wallet.size_pct() * size_mult)
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
                    # Sweep: sell ALL open buys that are profitable at this level
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
            log.info("  [GRID/%s] Price=$%.4f | open=%d | regime=%s",
                     coin, price, len(g.open_buys), regime)

        # ── Step 4: Save & dashboard ──
        State.save(self.wallet, self.grids)

        total_open_final = sum(len(g.open_buys) for g in self.grids.values())
        mood = "hunting" if trades_this_scan > 0 else ("watching" if total_open_final > 0 else "sleeping")
        log.info("\nScan done | trades=%d | open=%d | cash=$%.2f | total_pnl=$%+.2f\n",
                 trades_this_scan, total_open_final, self.wallet.cash, self.wallet.total_pnl)

        self._write_dashboard(prices, mood, trades_this_scan, vol_regimes, portfolio_value_now)

    def run_loop(self):
        log.info("Seraphina v5 starting | mode=%s | budget=$%.0f | coins=%s",
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
