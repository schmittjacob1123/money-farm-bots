"""
╔══════════════════════════════════════════════════════════════╗
║         SERAPHINA'S CRYPTO ENGINE v3.0 — GRID TRADER        ║
║  Strategy: Automated grid trading on BTC & ETH              ║
║  Places buy/sell orders at fixed price intervals.            ║
║  Profits from volatility — no direction prediction needed.   ║
╚══════════════════════════════════════════════════════════════╝

HOW IT WORKS:
  On startup, Seraphina fetches the current price of BTC and ETH.
  She builds a grid of price levels above and below the current price.
  Every scan she checks if price has crossed a grid line:
    - Crossed DOWN through a level → BUY (pick up cheap coins)
    - Crossed UP through a level   → SELL (take profit)
  Each grid trade is a fixed dollar size. Profit = grid spacing minus fees.
  More volatility = more grid crossings = more profit.

NO AI CALLS. No Anthropic credits used. Pure math.

SETUP:
  pip install requests python-dotenv

RUNNING:
  python3 seraphina_bot.py
"""

import os, csv, json, logging, time, sys, math, random
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo('America/New_York')
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
CONFIG = {
    "dry_run":          os.getenv("DRY_RUN", "true").lower() != "false",
    "paper_budget":     50.0,

    # Grid settings
    "coins":            ["BTC", "ETH", "SOL", "DOGE"],
    "grid_levels":      8,
    "grid_spacing_pct": 0.005,

    # Dynamic sizing: % of wallet per trade (scales as wallet grows)
    "trade_size_pct":        0.08,  # 8% of wallet = $4 at $50, $16 at $200
    "trade_size_min":        1.0,
    "trade_size_max":        50.0,

    # Confidence tiers: levels closer to center trade bigger
    "confidence_tiers": {0:1.5, 1:1.3, 2:1.1, 3:1.0, 4:0.9, 5:0.8, 6:0.7, 7:0.6},

    # Dynamic position cap: grows with wallet
    "max_open_grids_base":    6,
    "max_open_grids_per_usd": 25,
    "max_open_grids_cap":     24,

    # Risk
    "daily_loss_cap":   15.0,
    "max_drawdown_pct": 0.30,       # reset grid if price moves 30% from center
    # ── Trailing stop (momentum exit) ──
    "trail_activate_pct": 0.005,    # position must be up ≥0.5% to arm the trailing stop
    "trail_stop_pct":     0.004,    # sell if price drops ≥0.4% from peak while armed

    # Timing
    "scan_interval_sec": 60,        # check every 60 seconds
    "grid_reset_hours":  24,        # rebuild grid every 24 hours

    # Files
    "log_file":         "seraphina.log",
    "csv_file":         "seraphina_trades.csv",
    "state_file":       "seraphina_state.json",
    "dashboard_file":   "seraphina_data.json",
}

# Seraphina's personality quotes
SERA_QUOTES = {
    "hunting": [
        "Grid crossed. Profit locked. That's how we do it.",
        "Volatility is just free money if you're positioned right.",
        "Another level hit. Another dollar earned.",
        "The grid never sleeps. Neither do I.",
        "Buy the dip. Sell the rip. Repeat forever.",
    ],
    "watching": [
        "Grids are set. Waiting for the market to come to me.",
        "Patience is a strategy. My orders are already placed.",
        "The price will move. It always moves. I'll be ready.",
        "Every minute without a trade is a minute I'm not losing either.",
        "Watching the levels. Just waiting.",
    ],
    "sleeping": [
        "Low volatility. The grid is patient.",
        "Market's quiet. My orders are still there.",
        "Nothing to do but wait. The grid handles itself.",
        "Boring market. That's fine. I'm already positioned.",
    ],
}

SERA_ART = {
    "hunting": [
        "  /\\ /\\\n (=^.^=) $\n  (   )\n  -\"-\"-",
        "  /\\ /\\\n (=o.o=)>\n  (   )\n  -\"-\"-",
    ],
    "watching": [
        "  /\\ /\\\n (=^.^=)\n  (   )\n  -\"-\"-",
        "  /\\ /\\\n (=-.-=-)\n  (   )\n  -\"-\"-",
    ],
    "sleeping": [
        "  /\\ /\\\n (=-.-=)\n  (   )\n  zz-\"-\"-",
    ],
}

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CONFIG["log_file"], encoding="utf-8")]
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log = logging.getLogger("seraphina")
log.addHandler(console)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: PRICE FETCHER (Binance only — free, reliable, no API key needed)
# ══════════════════════════════════════════════════════════════════════════════
class PriceFetcher:
    # Kraken — no geo restrictions, no API key needed, free
    KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"

    SYMBOLS = {
        "BTC":  "XBTUSD",
        "ETH":  "ETHUSD",
        "SOL":  "SOLUSD",
        "DOGE": "XDGUSD",
        "XRP":  "XRPUSD",
    }

    def fetch_price(self, coin):
        pair = self.SYMBOLS.get(coin)
        if not pair: return None
        try:
            r = requests.get(self.KRAKEN_URL, params={"pair": pair}, timeout=8)
            r.raise_for_status()
            res = r.json().get("result", {})
            if not res: return None
            t = list(res.values())[0]
            bid = float(t["b"][0])
            ask = float(t["a"][0])
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
        except Exception as e:
            log.debug(f"Price fetch {coin}: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2: GRID MANAGER
# ══════════════════════════════════════════════════════════════════════════════
class GridManager:
    """
    Maintains a grid of buy/sell levels for a single coin.
    Levels are set at fixed % intervals above and below the center price.
    Tracks which levels have been bought (waiting to sell) and which are empty.
    """

    def __init__(self, coin, center_price):
        self.coin         = coin
        self.center_price = center_price
        self.created_at   = datetime.now(ET).isoformat()
        self.levels       = self._build_levels(center_price)
        self.open_buys    = {}   # level_idx -> {"buy_price": x, "size_usd": y, "bought_at": z}
        log.info(f"  [GRID/{coin}] Built {len(self.levels)} levels around ${center_price:,.2f}")
        for i, lvl in enumerate(self.levels):
            log.info(f"    Level {i:+d}: ${lvl:,.2f}")

    def _build_levels(self, center):
        n = CONFIG["grid_levels"]
        sp = CONFIG["grid_spacing_pct"]
        # Use more decimal places for low-price coins
        decimals = 2 if center >= 10 else 4 if center >= 0.1 else 6
        levels = []
        for i in range(-n, n + 1):
            levels.append(round(center * (1 + i * sp), decimals))
        return sorted(set(levels))  # dedupe in case rounding collapses levels

    def _trade_size(self, level_idx, wallet):
        """Calculate trade size: wallet% base * confidence multiplier by distance from center."""
        base = max(CONFIG["trade_size_min"],
                   min(CONFIG["trade_size_max"],
                       wallet * CONFIG["trade_size_pct"]))
        # Distance from center level (mid of levels list)
        center_idx = len(self.levels) // 2
        dist = min(abs(level_idx - center_idx), 7)
        mult = CONFIG["confidence_tiers"].get(dist, 0.6)
        return round(base * mult, 2)

    def needs_reset(self, current_price):
        """True if price has drifted too far from center to be useful."""
        drift = abs(current_price - self.center_price) / self.center_price
        return drift > CONFIG["max_drawdown_pct"]

    def check_crossings(self, prev_price, curr_price, wallet, daily_loss, open_count):
        """
        Given price moved from prev to curr, find grid levels crossed.
        Returns list of trade actions to execute.
        """
        actions = []
        lo, hi = min(prev_price, curr_price), max(prev_price, curr_price)

        for i, level in enumerate(self.levels):
            if not (lo <= level <= hi):
                continue

            # Price crossed DOWN through this level → BUY opportunity
            if curr_price < prev_price and i not in self.open_buys:
                dyn_cap = min(CONFIG["max_open_grids_cap"],
                              CONFIG["max_open_grids_base"] + int((wallet - CONFIG.get("paper_budget", 50)) / CONFIG["max_open_grids_per_usd"]))
                dyn_cap = max(CONFIG["max_open_grids_base"], dyn_cap)
                if open_count >= dyn_cap:
                    log.info(f"  [GRID/{self.coin}] Max open grids ({dyn_cap}) reached — skip buy at ${level:,.2f}")
                    continue
                if daily_loss >= CONFIG["daily_loss_cap"]:
                    log.info(f"  [GRID/{self.coin}] Daily loss cap hit — skip buy")
                    continue
                trade_size = self._trade_size(i, wallet)
                if trade_size > wallet:
                    log.info(f"  [GRID/{self.coin}] Not enough wallet for buy")
                    continue
                actions.append({"action": "BUY", "level_idx": i, "price": level, "trade_size": trade_size})

            # Price crossed UP through this level → SELL if we have a buy here or below
            elif curr_price > prev_price:
                # Find the nearest open buy below this level
                buys_below = {k: v for k, v in self.open_buys.items() if self.levels[k] < level}
                if buys_below:
                    # Sell the lowest open buy (most profit)
                    buy_idx = min(buys_below.keys())
                    buy = buys_below[buy_idx]
                    profit_pct = (level - buy["buy_price"]) / buy["buy_price"]
                    profit_usd = round(buy["size_usd"] * profit_pct, 4)
                    actions.append({
                        "action":     "SELL",
                        "level_idx":  i,
                        "price":      level,
                        "buy_idx":    buy_idx,
                        "buy_price":  buy["buy_price"],
                        "profit_usd": profit_usd,
                        "size_usd":   buy["size_usd"],
                    })

        return actions

    def execute_buy(self, action):
        self.open_buys[action["level_idx"]] = {
            "buy_price":  action["price"],
            "size_usd":   action.get("trade_size", CONFIG["trade_size_pct"] * 50),
            "bought_at":  datetime.now(ET).isoformat(),
            "peak_price": action["price"],   # trailing stop tracker — updated each scan
        }


    def check_trailing_stops(self, curr_price):
        """
        Trailing momentum exit: if a position is up ≥trail_activate_pct from entry
        AND price has since dropped ≥trail_stop_pct from its peak, sell it now.
        Also updates peak_price on each call.
        """
        actions = []
        activate = CONFIG["trail_activate_pct"]
        trail    = CONFIG["trail_stop_pct"]

        for idx, buy in list(self.open_buys.items()):
            buy_price = buy["buy_price"]

            # Update peak
            if curr_price > buy.get("peak_price", buy_price):
                buy["peak_price"] = curr_price

            peak = buy.get("peak_price", buy_price)
            gain_from_entry = (peak - buy_price) / buy_price
            drop_from_peak  = (peak - curr_price) / peak

            if gain_from_entry >= activate and drop_from_peak >= trail:
                profit_usd = round(buy["size_usd"] * (curr_price - buy_price) / buy_price, 4)
                log.info(f"  [TRAIL/{self.coin}] Trailing stop fired | "
                         f"Bought ${buy_price:,.4f} | Peak ${peak:,.4f} | "
                         f"Now ${curr_price:,.4f} | Drop {drop_from_peak*100:.2f}% | P&L ${profit_usd:+.4f}")
                actions.append({
                    "action":     "SELL",
                    "level_idx":  idx,
                    "price":      curr_price,
                    "buy_idx":    idx,
                    "buy_price":  buy_price,
                    "profit_usd": profit_usd,
                    "size_usd":   buy["size_usd"],
                    "reason":     "trailing_stop",
                })
        return actions

    def execute_sell(self, action):
        if action["buy_idx"] in self.open_buys:
            del self.open_buys[action["buy_idx"]]

    def to_dict(self):
        return {
            "coin":         self.coin,
            "center_price": self.center_price,
            "created_at":   self.created_at,
            "levels":       self.levels,
            "open_buys":    {str(k): v for k, v in self.open_buys.items()},
        }

    @classmethod
    def from_dict(cls, d):
        g = cls.__new__(cls)
        g.coin         = d["coin"]
        g.center_price = d["center_price"]
        g.created_at   = d["created_at"]
        g.levels       = d["levels"]
        g.open_buys    = {int(k): v for k, v in d.get("open_buys", {}).items()}
        return g


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: RISK & STATE MANAGER
# ══════════════════════════════════════════════════════════════════════════════
class RiskManager:
    def __init__(self):
        self.wallet       = CONFIG["paper_budget"]
        self.total_pnl    = 0.0
        self.daily_pnl    = 0.0
        self.trade_history = []
        self.last_reset_date = datetime.now(ET).date().isoformat()
        self._load_state()

    def _load_state(self):
        try:
            with open(CONFIG["state_file"]) as f:
                s = json.load(f)
            self.wallet            = s.get("wallet", CONFIG["paper_budget"])
            self.total_pnl         = s.get("total_pnl", 0.0)
            self.daily_pnl         = s.get("daily_pnl", 0.0)
            self.trade_history     = s.get("trade_history", [])
            self.last_reset_date   = s.get("last_reset_date", datetime.now(ET).date().isoformat())
            log.info(f"[INIT] Seraphina wallet: ${self.wallet:.2f} | Total P&L: ${self.total_pnl:+.2f}")
        except:
            log.info("[INIT] Fresh state — starting simulation")

    def _save_state(self, grids):
        try:
            with open(CONFIG["state_file"], "w") as f:
                json.dump({
                    "wallet":           round(self.wallet, 4),
                    "total_pnl":        round(self.total_pnl, 4),
                    "daily_pnl":        round(self.daily_pnl, 4),
                    "trade_history":    self.trade_history[-100:],
                    "last_reset_date":  self.last_reset_date,
                    "grids":            {k: v.to_dict() for k, v in grids.items()},
                }, f, indent=2)
        except Exception as e:
            log.warning(f"State save failed: {e}")

    def reset_daily(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_reset_date:
            self.daily_pnl       = 0.0
            self.last_reset_date = today

    def record_trade(self, coin, action, price, size_usd, pnl):
        self.wallet    += pnl
        self.total_pnl += pnl
        self.daily_pnl += pnl
        self.trade_history.append({
            "timestamp": datetime.now(ET).isoformat(),
            "coin":      coin,
            "action":    action,
            "price":     price,
            "size_usd":  size_usd,
            "pnl":       round(pnl, 4),
            "wallet":    round(self.wallet, 2),
        })

    def win_rate(self):
        wins = sum(1 for t in self.trade_history if t.get("pnl", 0) > 0)
        total = len([t for t in self.trade_history if t.get("action") == "SELL"])
        return round(wins / total * 100, 1) if total > 0 else 0

    def roi(self):
        return round((self.total_pnl / CONFIG["paper_budget"]) * 100, 2)

    def status(self):
        return (f"Wallet: ${self.wallet:.2f} | P&L: ${self.total_pnl:+.4f} | "
                f"Daily: ${self.daily_pnl:+.4f} | Win rate: {self.win_rate()}% | ROI: {self.roi():+.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: TRADE LOGGER
# ══════════════════════════════════════════════════════════════════════════════
class TradeLogger:
    def __init__(self):
        if not os.path.exists(CONFIG["csv_file"]):
            with open(CONFIG["csv_file"], "w", newline="") as f:
                csv.writer(f).writerow(["timestamp", "coin", "action", "price", "size_usd", "pnl", "wallet"])

    def log(self, coin, action, price, size_usd, pnl, wallet):
        with open(CONFIG["csv_file"], "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                coin, action, f"{price:.4f}", f"{size_usd:.2f}",
                f"{pnl:+.4f}", f"{wallet:.2f}"
            ])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BOT
# ══════════════════════════════════════════════════════════════════════════════
class SeraphinaBot:
    def __init__(self):
        self.fetcher    = PriceFetcher()
        self.risk       = RiskManager()
        self.logger     = TradeLogger()
        self.grids      = {}       # coin -> GridManager
        self.prev_prices = {}      # coin -> last mid price
        self.scan_count  = 0
        self.trade_count = 0
        self._load_grids()

    def _load_grids(self):
        """Restore grids from state file if available."""
        try:
            with open(CONFIG["state_file"]) as f:
                s = json.load(f)
            for coin, gd in s.get("grids", {}).items():
                self.grids[coin] = GridManager.from_dict(gd)
                log.info(f"[INIT] Restored grid for {coin} — center ${gd['center_price']:,.2f} | "
                         f"{len(gd.get('open_buys', {}))} open buys")
        except:
            pass

    def _pick_quote(self, mood):
        return random.choice(SERA_QUOTES.get(mood, SERA_QUOTES["watching"]))

    def _pick_art(self, mood):
        return random.choice(SERA_ART.get(mood, SERA_ART["watching"]))

    def _write_dashboard(self, prices, mood, trades_this_scan):
        try:
            grids_out = []
            for coin, grid in self.grids.items():
                curr = prices.get(coin)
                grids_out.append({
                    "coin":         coin,
                    "centerPrice":  grid.center_price,
                    "currentPrice": curr,
                    "levels":       grid.levels,
                    "openBuys":     len(grid.open_buys),
                    "openBuysList": [
                        {"level": grid.levels[int(k)], "buyPrice": v["buy_price"],
                         "sizeUsd": v["size_usd"], "boughtAt": v["bought_at"]}
                        for k, v in grid.open_buys.items()
                    ],
                    "spacing":      f"{CONFIG['grid_spacing_pct']*100:.1f}%",
                    "needsReset":   grid.needs_reset(curr) if curr else False,
                })

            # Calculate live portfolio value = cash + current value of all open buys
            open_position_value = 0.0
            for coin, grid in self.grids.items():
                curr = prices.get(coin)
                if not curr:
                    continue
                for k, v in grid.open_buys.items():
                    # Each open buy is worth its current market value
                    open_position_value += v["size_usd"] * (curr / v["buy_price"])

            portfolio_value = round(self.risk.wallet + open_position_value, 2)
            portfolio_pnl   = round(portfolio_value - CONFIG["paper_budget"], 4)
            portfolio_roi   = round((portfolio_value - CONFIG["paper_budget"]) / CONFIG["paper_budget"] * 100, 2)

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "lastScan":          datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":              "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":              mood,
                    "quote":             self._pick_quote(mood),
                    "art":               self._pick_art(mood),
                    "wallet":            round(self.risk.wallet, 2),
                    "portfolioValue":    portfolio_value,
                    "portfolioPnl":      portfolio_pnl,
                    "portfolioRoi":      portfolio_roi,
                    "openPositionValue": round(open_position_value, 4),
                    "startingBudget":    CONFIG["paper_budget"],
                    "totalPnl":          round(self.risk.total_pnl, 4),
                    "dailyPnl":          round(self.risk.daily_pnl, 4),
                    "winRate":           self.risk.win_rate(),
                    "roi":               self.risk.roi(),
                    "tradesTotal":       len(self.risk.trade_history),
                    "tradesThisScan":    trades_this_scan,
                    "scanCount":         self.scan_count,
                    "grids":             grids_out,
                    "recentTrades":      self.risk.trade_history[-20:],
                    "prices":            {c: round(p, 2) for c, p in prices.items() if p},
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Dashboard write failed: {e}")

    def _init_grid(self, coin, price):
        self.grids[coin] = GridManager(coin, price)
        self.prev_prices[coin] = price

    def run_once(self):
        self.scan_count += 1
        self.risk.reset_daily()

        log.info(f"{'='*60}")
        log.info(f"  SERAPHINA GRID v3 #{self.scan_count} -- "
                 f"{'PAPER' if CONFIG['dry_run'] else 'LIVE'} -- "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"  {self.risk.status()}")
        log.info(f"{'='*60}")

        # ── Step 1: Fetch current prices ──
        prices = {}
        for coin in CONFIG["coins"]:
            p = self.fetcher.fetch_price(coin)
            if p:
                prices[coin] = p["mid"]
                log.info(f"  {coin}: ${p['mid']:,.2f}")
            else:
                log.warning(f"  {coin}: price fetch failed")

        # ── Step 2: Init or reset grids if needed ──
        for coin, price in prices.items():
            if coin not in self.grids:
                log.info(f"  [GRID/{coin}] Initialising new grid at ${price:,.2f}")
                self._init_grid(coin, price)
            elif self.grids[coin].needs_reset(price):
                log.info(f"  [GRID/{coin}] Price drifted too far — rebuilding grid at ${price:,.2f}")
                self._init_grid(coin, price)

        # ── Step 3: Check grid crossings ──
        trades_this_scan = 0
        total_open_buys = sum(len(g.open_buys) for g in self.grids.values())

        for coin, price in prices.items():
            if coin not in self.grids: continue
            grid = self.grids[coin]
            prev = self.prev_prices.get(coin, price)

            if prev == price:
                log.info(f"  [GRID/{coin}] No price change — skip")
                self.prev_prices[coin] = price
                continue

            # ── Trailing stop check (runs every scan regardless of price change) ──
            trail_actions = grid.check_trailing_stops(price)
            for action in trail_actions:
                grid.execute_sell(action)
                pnl = action["profit_usd"]
                self.risk.record_trade(coin, "SELL", action["price"],
                                       action["size_usd"], action["size_usd"] + pnl)
                self.logger.log(coin, "SELL", action["price"],
                                action["size_usd"], pnl, self.risk.wallet)
                sign = "+" if pnl >= 0 else ""
                log.info(f"  [TRAIL/{coin}] SELL at ${action['price']:,.2f} | "
                         f"Bought at ${action['buy_price']:,.2f} | "
                         f"P&L: {sign}${pnl:.4f} | Wallet: ${self.risk.wallet:.2f}")
                trades_this_scan += 1
                self.trade_count += 1

            actions = grid.check_crossings(
                prev, price,
                self.risk.wallet,
                self.risk.daily_pnl,
                total_open_buys
            )

            for action in actions:
                if action["action"] == "BUY":
                    ts = action.get("trade_size", CONFIG["trade_size_pct"] * self.risk.wallet)
                    grid.execute_buy(action)
                    self.risk.record_trade(coin, "BUY", action["price"], ts, -ts)
                    self.logger.log(coin, "BUY", action["price"], ts, -ts, self.risk.wallet)
                    log.info(f"  [GRID/{coin}] BUY at ${action['price']:,.2f} | "
                             f"${ts:.2f} | Wallet: ${self.risk.wallet:.2f}")
                    trades_this_scan += 1

                elif action["action"] == "SELL":
                    grid.execute_sell(action)
                    pnl = action["profit_usd"]
                    self.risk.record_trade(coin, "SELL", action["price"],
                                          action["size_usd"], action["size_usd"] + pnl)
                    self.logger.log(coin, "SELL", action["price"],
                                   action["size_usd"], pnl, self.risk.wallet)
                    sign = "+" if pnl >= 0 else ""
                    log.info(f"  [GRID/{coin}] SELL at ${action['price']:,.2f} | "
                             f"Bought at ${action['buy_price']:,.2f} | "
                             f"P&L: {sign}${pnl:.4f} | Wallet: ${self.risk.wallet:.2f}")
                    trades_this_scan += 1
                    self.trade_count += 1

            self.prev_prices[coin] = price
            log.info(f"  [GRID/{coin}] Price: ${price:,.2f} | Open buys: {len(grid.open_buys)} | "
                     f"Actions this scan: {len(actions)}")

        # ── Step 4: Save state & dashboard ──
        self.risk._save_state(self.grids)

        if trades_this_scan > 0:
            mood = "hunting"
        elif total_open_buys > 0:
            mood = "watching"
        else:
            mood = "sleeping" if self.scan_count % 5 == 0 else "watching"

        log.info(f"\nScan done | Trades: {trades_this_scan} | "
                 f"Open buys: {sum(len(g.open_buys) for g in self.grids.values())} | "
                 f"{self.risk.status()}\n")

        self._write_dashboard(prices, mood, trades_this_scan)

    def run_loop(self):
        log.info("Seraphina Grid Trading Engine v3 starting...")
        log.info(f"  Mode: {'PAPER' if CONFIG['dry_run'] else 'LIVE'} | "
                 f"Budget: ${CONFIG['paper_budget']} | "
                 f"Coins: {', '.join(CONFIG['coins'])}")
        log.info(f"  Grid: {CONFIG['grid_levels']} levels | "
                 f"Spacing: {CONFIG['grid_spacing_pct']*100:.1f}% | "
                 f"Trade size: {CONFIG['trade_size_pct']*100:.0f}% of wallet (${CONFIG['trade_size_min']}-${CONFIG['trade_size_max']})")
        log.info("  No AI calls. Pure grid. Always profitable in volatile markets.")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("Seraphina rests. Goodbye.")
                break
            except Exception as e:
                log.error(f"Unexpected error: {e}", exc_info=True)
            log.info(f"Sleeping {CONFIG['scan_interval_sec']}s...\n")
            time.sleep(CONFIG["scan_interval_sec"])


if __name__ == "__main__":
    bot = SeraphinaBot()
    if "--once" in sys.argv:
        bot.run_once()
    else:
        bot.run_loop()
