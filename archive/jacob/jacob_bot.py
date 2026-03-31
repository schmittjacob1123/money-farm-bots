#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         JACOB'S STOCK ENGINE  v5.0                          ║
║  Strategy: MA50 trend filter + daily RSI entry/exit         ║
║  Long-only. Cash in bear markets. Signal-based exits.       ║
║  The lizard sees alpha where the market doesn't.            ║
╚══════════════════════════════════════════════════════════════╝

v5 vs v4.2:
  - Daily candles (3mo) for MA50 + RSI instead of hourly (less noise)
  - Per-ticker MA50 gate: no longs when price < MA50
  - Long-only: bear regime → skip all new entries (go to cash)
  - Signal-based exits: RSI > 65 or +2.5% TP (no arbitrary 2-day hold)
  - Max hold extended to 5 days (safety net only)
  - Score threshold raised 62 → 68 (fewer, better trades)
  - Stop loss tightened 8% → 6%
  - Options disabled: 4% round-trip spread kills edge at $1000 budget
  - No mock fallback: skip ticker on API failure (no fake trades)
  - Trailing stop refined: arm +2.5%, fire -1.5% from peak

NO AI API CALLS. Zero Anthropic credits. Pure rules engine.
"""

import os, csv, json, logging, time, sys, random
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
ET = ZoneInfo("America/New_York")

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
CONFIG = {
    "dry_run":           os.getenv("DRY_RUN", "true").lower() != "false",
    "alpaca_api_key":    os.getenv("ALPACA_API_KEY", ""),
    "alpaca_secret_key": os.getenv("ALPACA_SECRET_KEY", ""),

    # Watchlist
    "watchlist": [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
        "AMD",  "NFLX", "JPM",  "SPY",   "QQQ",  "GLD",  "TLT",
        "BAC",  "DIS",  "PLTR", "COIN",  "SNOW", "UBER",
    ],

    # Risk
    "paper_budget":          500.0,
    "max_position_size_pct": 0.10,   # 10% of cash per trade
    "max_position_size_usd": 80.0,
    "min_position_size_usd": 5.0,
    "max_open_positions":    8,
    "daily_loss_cap_pct":    0.05,   # 5% of starting budget = $50 fixed
    "max_trades_per_day":    8,

    # Indicators (daily candles)
    "ma_period":             50,     # 50-day MA trend filter
    "rsi_period":            14,     # RSI period
    "rsi_entry":             40,     # buy when daily RSI < 40
    "rsi_exit":              65,     # sell when daily RSI > 65
    "candle_range":          "3mo",  # Yahoo range for daily candles
    "volume_surge_threshold": 1.5,

    # Scoring / confidence
    "score_threshold":       68,     # raised from 62
    "auto_trade_confidence": 0.72,
    "pending_confidence":    0.58,
    "min_confidence":        0.58,

    # Trade management
    "take_profit_pct":       0.025,  # 2.5% TP
    "stop_loss_pct":         0.06,   # 6% hard stop (tightened from 8%)
    "quick_cut_pct":         0.05,   # cut -5% within first 24h
    "trail_arm_pct":         0.025,  # arm trailing stop at +2.5%
    "trail_fire_pct":        0.015,  # fire if -1.5% from peak
    "max_hold_days":         5,      # safety net max hold

    # Fees (Alpaca: $0 commission, spread only)
    "stock_spread_pct":      0.0005, # 0.05% per side = 0.10% round trip

    # Market regime (SPY + QQQ 5-day momentum)
    "bear_threshold_pct":    -2.0,
    "bull_threshold_pct":     1.5,

    # Sector concentration
    "max_per_sector":         2,

    # Timing
    "scan_interval_sec":     300,    # 5 min during market hours

    # Pending
    "pending_expiry_mins":   1080,   # 18h

    # Files
    "state_file":     "jacob_state.json",
    "dashboard_file": "jacob_data.json",
    "pending_file":   "jacob_pending.json",
    "csv_file":       "jacob_trades.csv",
    "log_file":       "jacob.log",
}

DAILY_LOSS_CAP = CONFIG["paper_budget"] * CONFIG["daily_loss_cap_pct"]

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CONFIG["log_file"], encoding="utf-8")],
)
log = logging.getLogger("jacob")
_con = logging.StreamHandler()
_con.setLevel(logging.INFO)
_con.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_con)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ══════════════════════════════════════════════════════════════
# PERSONALITY
# ══════════════════════════════════════════════════════════════
QUOTES = {
    "hunting": [
        "RSI dip in an uptrend. MA50 holding. Entering.",
        "Volume surge confirmed. The lizard is in.",
        "Oversold below MA50? Wrong. Above MA50. Signal is clean.",
        "Price pulled back to RSI 40. Textbook entry.",
    ],
    "watching": [
        "Scanning 20 tickers. Looking for RSI < 40 above MA50.",
        "Good tape today. Waiting for the right pullback.",
        "Regime is neutral. Patience is alpha.",
        "Nothing at threshold yet. The right setup is coming.",
    ],
    "sleeping": [
        "Bear regime. All cash. Preservation mode.",
        "Market closed. Watchlist ready for morning.",
        "Zero trades = zero losses. Math is math.",
        "Choppy tape. The lizard doesn't trade chop.",
    ],
    "pending": [
        "Found something. Want your eyes on it first.",
        "Borderline signal above threshold — your call.",
        "MA50 confirmed, RSI borderline. Pending your review.",
    ],
    "exiting": [
        "RSI overbought. Taking profit. Moving on.",
        "Target hit. Booked. Waiting for the next dip.",
        "Sold into strength. Patience paid off.",
    ],
}

# ══════════════════════════════════════════════════════════════
# MODULE 1 — MARKET DATA
# ══════════════════════════════════════════════════════════════
class MarketData:
    """
    Fetches daily candles from Yahoo Finance.
    Computes MA50 + RSI14 on daily closes — less noise than hourly.
    No mock fallback: if Yahoo fails, returns None (ticker skipped).
    """
    _cache     = {}
    CACHE_SECS = 270  # 4.5 min — slightly under 5-min scan interval

    def _yahoo_daily(self, ticker):
        now = time.time()
        if ticker in self._cache:
            ts, data = self._cache[ticker]
            if now - ts < self.CACHE_SECS:
                return data
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            r = requests.get(
                url,
                params={"range": CONFIG["candle_range"], "interval": "1d",
                        "includePrePost": "false"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            r.raise_for_status()
            result = r.json().get("chart", {}).get("result", [])
            if not result:
                return None
            meta    = result[0].get("meta", {})
            q       = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes  = [c for c in q.get("close",  []) if c is not None]
            volumes = [v for v in q.get("volume", []) if v is not None]
            if len(closes) < CONFIG["ma_period"]:
                return None  # not enough history
            price = float(meta.get("regularMarketPrice", closes[-1]))
            avg_vol = int(sum(volumes[-20:]) / max(len(volumes[-20:]), 1)) if volumes else 1
            data = {
                "ticker":     ticker,
                "price":      round(price, 4),
                "prev_close": round(float(meta.get("chartPreviousClose", closes[-1])), 4),
                "volume":     int(meta.get("regularMarketVolume", volumes[-1] if volumes else 0)),
                "avg_volume": avg_vol,
                "closes":     closes,
                "volumes":    volumes,
            }
            if data["prev_close"] > 0:
                data["change_pct"] = round(
                    (price - data["prev_close"]) / data["prev_close"] * 100, 2)
            else:
                data["change_pct"] = 0.0
            self._cache[ticker] = (now, data)
            return data
        except Exception as e:
            log.debug("Yahoo daily failed %s: %s", ticker, e)
            return None

    @staticmethod
    def _calc_rsi(closes, period=14):
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        ag = sum(gains[:period]) / period
        al = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            ag = (ag * (period - 1) + gains[i]) / period
            al = (al * (period - 1) + losses[i]) / period
        if al == 0:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + ag / al), 1)

    @staticmethod
    def _calc_ma(closes, period=50):
        if len(closes) < period:
            return None
        return round(sum(closes[-period:]) / period, 4)

    @staticmethod
    def _calc_momentum(closes, period=10):
        if len(closes) < period + 1:
            return 0.0
        return round((closes[-1] - closes[-period - 1]) / max(closes[-period - 1], 0.01) * 100, 2)

    def get_quote(self, ticker):
        """Returns enriched quote dict or None if data unavailable."""
        data = self._yahoo_daily(ticker)
        if not data:
            return None
        closes = data["closes"]
        data["rsi"]          = self._calc_rsi(closes, CONFIG["rsi_period"])
        data["ma50"]         = self._calc_ma(closes, CONFIG["ma_period"])
        data["above_ma50"]   = data["price"] > data["ma50"] if data["ma50"] else False
        data["momentum_5d"]  = self._calc_momentum(closes, 5)
        data["momentum_10d"] = self._calc_momentum(closes, 10)
        avg_vol = data["avg_volume"] or 1
        data["volume_ratio"] = round(data["volume"] / avg_vol, 2)
        return data

    def get_live_price(self, ticker):
        """Live price bypass — for stop loss and TP checks."""
        clean = ticker.replace("_OPT", "")
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean}"
            r = requests.get(url, params={"range": "1d", "interval": "1m"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            r.raise_for_status()
            result = r.json().get("chart", {}).get("result", [])
            if not result:
                return None
            price = result[0].get("meta", {}).get("regularMarketPrice")
            return round(float(price), 4) if price else None
        except Exception as e:
            log.debug("Live price failed %s: %s", clean, e)
            return None


# ══════════════════════════════════════════════════════════════
# MODULE 2 — MARKET REGIME
# ══════════════════════════════════════════════════════════════
class MarketRegime:
    """
    SPY + QQQ 5-day momentum → BEAR / NEUTRAL / BULL.
    BEAR: block all new long entries (go to cash on exits).
    BULL: boost confidence slightly.
    """
    def __init__(self, market):
        self.market      = market
        self._regime     = "neutral"
        self._spy5       = 0.0
        self._qqq5       = 0.0
        self._spy_chg    = 0.0
        self._cache_time = None

    def get(self):
        now = datetime.now(ET)
        if self._cache_time and (now - self._cache_time).total_seconds() < 290:
            return self._regime, self._spy5, self._qqq5
        try:
            spy = self.market.get_quote("SPY")
            qqq = self.market.get_quote("QQQ")
            spy5 = spy["momentum_5d"] if spy else 0
            qqq5 = qqq["momentum_5d"] if qqq else 0
            self._spy_chg = spy["change_pct"] if spy else 0

            if spy5 < CONFIG["bear_threshold_pct"] and qqq5 < CONFIG["bear_threshold_pct"]:
                self._regime = "bear"
            elif spy5 > CONFIG["bull_threshold_pct"] and qqq5 > CONFIG["bull_threshold_pct"]:
                self._regime = "bull"
            else:
                self._regime = "neutral"

            self._spy5       = spy5
            self._qqq5       = qqq5
            self._cache_time = now
            log.info("  [REGIME] SPY 5d=%+.1f%% | QQQ 5d=%+.1f%% | today=%+.1f%% → %s",
                     spy5, qqq5, self._spy_chg, self._regime.upper())
        except Exception as e:
            log.warning("  [REGIME] Fetch failed: %s — keeping %s", e, self._regime.upper())
        return self._regime, self._spy5, self._qqq5

    @property
    def high_vol_day(self):
        return abs(self._spy_chg) > 2.0


# ══════════════════════════════════════════════════════════════
# MODULE 3 — SIGNAL SCORER
# ══════════════════════════════════════════════════════════════
class SignalScorer:
    """
    Scores tickers 0–100 for LONG setups only.
    Requires price > MA50 as a hard pre-filter before scoring matters.
    """

    def score(self, quote):
        rsi       = quote.get("rsi", 50)
        mom5      = quote.get("momentum_5d", 0)
        mom10     = quote.get("momentum_10d", 0)
        vol_ratio = quote.get("volume_ratio", 1.0)
        chg_pct   = quote.get("change_pct", 0)
        above_ma  = quote.get("above_ma50", False)

        score   = 50
        signals = []

        # MA50 gate contributes to score too
        if above_ma:
            score += 5; signals.append("Above MA50 — uptrend confirmed")
        else:
            score -= 15; signals.append("Below MA50 — downtrend, skipping")

        # RSI: buy the dip in an uptrend
        if rsi < CONFIG["rsi_entry"]:
            score += 18; signals.append(f"RSI {rsi:.0f} — oversold dip")
        elif rsi < 50:
            score += 8;  signals.append(f"RSI {rsi:.0f} — cooling")
        elif rsi > CONFIG["rsi_exit"]:
            score -= 15; signals.append(f"RSI {rsi:.0f} — overbought, no entry")

        # Momentum: want positive but not chasing
        if 2 <= mom5 <= 8:
            score += 12; signals.append(f"Healthy momentum +{mom5:.1f}%")
        elif mom5 > 8:
            score += 5;  signals.append(f"Hot momentum +{mom5:.1f}% — extended")
        elif mom5 < -3:
            score -= 10; signals.append(f"Falling momentum {mom5:.1f}%")

        if mom10 > 5:
            score += 8;  signals.append(f"10d uptrend +{mom10:.1f}%")

        # Volume conviction
        if vol_ratio >= 2.0:
            score += 14; signals.append(f"Volume surge {vol_ratio:.1f}x — conviction")
        elif vol_ratio >= CONFIG["volume_surge_threshold"]:
            score += 8;  signals.append(f"Volume elevated {vol_ratio:.1f}x")
        elif vol_ratio < 0.6:
            score -= 8;  signals.append(f"Low volume {vol_ratio:.1f}x — weak")

        # Daily move
        if 1 < chg_pct < 5:
            score += 6;  signals.append(f"Positive day +{chg_pct:.1f}%")
        elif chg_pct > 5:
            score += 2;  signals.append(f"Large move +{chg_pct:.1f}% — may be extended")
        elif chg_pct < -3:
            score -= 5;  signals.append(f"Selling pressure {chg_pct:.1f}%")

        return {"score": max(0, min(100, score)), "direction": "LONG", "signals": signals}


# ══════════════════════════════════════════════════════════════
# MODULE 4 — RULES ENGINE
# ══════════════════════════════════════════════════════════════
class RulesEngine:
    """
    Per-ticker knowledge + rules-based confidence scorer.
    No API calls. Outputs confidence 0.0–1.0 for LONG setups.
    """

    PROFILES = {
        "AAPL":  {"sector": "tech",      "beta": 1.2, "tq": "high",
                  "note": "Strong brand moat. Momentum trades well. Dips above MA50 are reliable entries."},
        "MSFT":  {"sector": "tech",      "beta": 0.9, "tq": "high",
                  "note": "Cloud+AI tailwind. Lower beta, dips recover steadily."},
        "NVDA":  {"sector": "semis",     "beta": 1.9, "tq": "high",
                  "note": "AI capex dominant. High beta — volume surges are meaningful entries."},
        "GOOGL": {"sector": "tech",      "beta": 1.1, "tq": "high",
                  "note": "Ad revenue + cloud. Solid floor. Lags NVDA in momentum."},
        "AMZN":  {"sector": "tech",      "beta": 1.3, "tq": "high",
                  "note": "AWS growth driver. Momentum works in bull tape."},
        "META":  {"sector": "tech",      "beta": 1.4, "tq": "high",
                  "note": "Ad market dominant. Momentum very reliable."},
        "TSLA":  {"sector": "ev",        "beta": 2.2, "tq": "low",
                  "note": "Narrative-driven. Technicals unreliable — only trade on massive volume surges."},
        "AMD":   {"sector": "semis",     "beta": 1.8, "tq": "medium",
                  "note": "NVDA competitor. Volume surges and MA50 holding are key."},
        "NFLX":  {"sector": "streaming", "beta": 1.3, "tq": "medium",
                  "note": "Ad tier tailwind. Momentum trades OK."},
        "JPM":   {"sector": "finance",   "beta": 1.1, "tq": "medium",
                  "note": "Best-in-class bank. Strong floor. Dips above MA50 buy well."},
        "SPY":   {"sector": "etf",       "beta": 1.0, "tq": "high",
                  "note": "S&P 500. Very reliable technicals. Dips above MA50 are classic buys."},
        "QQQ":   {"sector": "etf",       "beta": 1.2, "tq": "high",
                  "note": "Nasdaq 100. MA50 dips in uptrend work well."},
        "GLD":   {"sector": "commodity", "beta": 0.1, "tq": "medium",
                  "note": "Gold. Inverse to real rates. Momentum works in risk-off."},
        "TLT":   {"sector": "bonds",     "beta": -0.3, "tq": "medium",
                  "note": "20yr Treasury. Rate-sensitive. MA50 trend is everything here."},
        "BAC":   {"sector": "finance",   "beta": 1.3, "tq": "medium",
                  "note": "Rate-sensitive bank. More beta than JPM — needs volume confirmation."},
        "DIS":   {"sector": "media",     "beta": 1.1, "tq": "low",
                  "note": "Turnaround story. Fundamentals mixed — needs strong volume to confirm."},
        "PLTR":  {"sector": "tech",      "beta": 1.7, "tq": "low",
                  "note": "High beta, momentum bursts but mean-reverts hard. Volume surge is key."},
        "COIN":  {"sector": "crypto",    "beta": 2.5, "tq": "low",
                  "note": "Crypto proxy. Only trade on strong volume + momentum + MA50 alignment."},
        "SNOW":  {"sector": "tech",      "beta": 1.6, "tq": "medium",
                  "note": "Cloud data, AI tailwind returning. Volume surges meaningful."},
        "UBER":  {"sector": "transport", "beta": 1.4, "tq": "medium",
                  "note": "Profitable now. Momentum trades well."},
    }

    SECTOR_WEIGHT = {
        "tech": 1.12, "semis": 1.18, "etf": 1.05, "finance": 0.92,
        "streaming": 0.95, "ev": 0.82, "crypto": 0.78, "commodity": 0.90,
        "bonds": 0.95, "media": 0.80, "transport": 0.95,
    }

    def _profile(self, ticker):
        return self.PROFILES.get(ticker, {
            "sector": "unknown", "beta": 1.0, "tq": "medium",
            "note": "Unknown ticker — applying neutral rules.",
        })

    def analyse(self, quote, signal, regime):
        """Returns (confirmed: bool, confidence: float, reasoning: str)."""
        ticker   = quote["ticker"]
        rsi      = quote.get("rsi", 50)
        mom5     = quote.get("momentum_5d", 0)
        mom10    = quote.get("momentum_10d", 0)
        vol      = quote.get("volume_ratio", 1.0)
        chg      = quote.get("change_pct", 0)
        above_ma = quote.get("above_ma50", False)
        score    = signal["score"]
        p        = self._profile(ticker)
        tq       = p["tq"]
        beta     = p["beta"]
        sector   = p["sector"]
        reasons  = []
        conf     = 0.50

        # Hard gates
        if not above_ma:
            return False, 0.30, f"{ticker} below MA50 — no long entries"

        # Score gate adjusted for ticker quality
        tq_bonus = {"high": 0, "medium": 5, "low": 12}[tq]
        min_score = CONFIG["score_threshold"] + tq_bonus
        if score < min_score:
            return False, 0.35, f"{ticker} score {score} < {min_score} needed (tq:{tq})"

        # RSI: best entries are RSI < rsi_entry
        if rsi < CONFIG["rsi_entry"]:
            conf += 0.14; reasons.append(f"RSI {rsi:.0f} — oversold dip in uptrend")
        elif rsi < 50:
            conf += 0.07; reasons.append(f"RSI {rsi:.0f} — cooling, room to run")
        elif rsi > CONFIG["rsi_exit"]:
            conf -= 0.14; reasons.append(f"RSI {rsi:.0f} — overbought, chasing")

        # Momentum alignment
        if mom5 > 3 and mom10 > 5:
            conf += 0.10; reasons.append(f"Momentum aligned +{mom5:.1f}%/+{mom10:.1f}%")
        elif mom5 > 1:
            conf += 0.05; reasons.append(f"Positive 5d momentum +{mom5:.1f}%")
        elif mom5 < -2:
            conf -= 0.08; reasons.append(f"Momentum fading {mom5:.1f}%")

        # Volume conviction
        if vol >= 2.0:
            conf += 0.12; reasons.append(f"Strong volume {vol:.1f}x — institutional buying")
        elif vol >= CONFIG["volume_surge_threshold"]:
            conf += 0.07; reasons.append(f"Volume elevated {vol:.1f}x")
        elif vol < 0.6:
            conf -= 0.08; reasons.append(f"Low volume {vol:.1f}x — weak conviction")

        # Sector / quality weight
        sw = self.SECTOR_WEIGHT.get(sector, 1.0)
        conf += (sw - 1.0) * 0.15

        # High-beta without volume: penalise
        if beta > 1.8 and vol < 1.5:
            conf -= 0.08; reasons.append(f"High beta ({beta}) without volume confirmation")

        # Regime tailwind
        if regime == "bull":
            conf += 0.05; reasons.append("Bull regime tailwind")
        elif regime == "neutral":
            pass  # no adjustment
        # bear is blocked upstream — this code won't run in bear

        # ETF bonus: reliable technicals
        if sector == "etf":
            conf += 0.05; reasons.append("ETF — reliable technicals")

        # Crypto/EV need extra conviction
        if sector in ("crypto", "ev") and vol < 1.8:
            conf -= 0.10; reasons.append(f"{sector.title()} — needs volume ≥1.8x to confirm")

        # Earnings gap — large day move on earnings-sensitive stock
        if abs(chg) > 5 and tq in ("low", "medium"):
            conf -= 0.08; reasons.append(f"Large move {chg:+.1f}% — may be post-earnings noise")

        conf = round(max(0.0, min(1.0, conf)), 3)
        reasoning = p["note"] + " | " + "; ".join(reasons[:3]) if reasons else p["note"]
        return conf >= CONFIG["min_confidence"], conf, reasoning


# ══════════════════════════════════════════════════════════════
# MODULE 5 — WALLET
# ══════════════════════════════════════════════════════════════
class Wallet:
    def __init__(self):
        self.cash           = CONFIG["paper_budget"]
        self.total_pnl      = 0.0
        self.daily_pnl      = 0.0
        self.trades_today   = 0
        self.wins           = 0
        self.losses         = 0
        self.last_date      = datetime.now(ET).date().isoformat()
        self.open_positions = {}
        self.trade_history  = []
        self.wallet_history = []
        self.total_fees     = 0.0

    def reset_daily(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_date:
            self.daily_pnl    = 0.0
            self.trades_today = 0
            self.last_date    = today
            log.info("[WALLET] Daily reset")

    def can_trade(self, ticker):
        self.reset_daily()
        if self.daily_pnl        <= -DAILY_LOSS_CAP:                return False, f"Daily loss cap (${DAILY_LOSS_CAP:.0f})"
        if len(self.open_positions) >= CONFIG["max_open_positions"]: return False, "Max open positions"
        if self.trades_today      >= CONFIG["max_trades_per_day"]:   return False, "Max trades today"
        if self.cash              <  CONFIG["min_position_size_usd"]: return False, "Wallet too low"
        for pos in self.open_positions.values():
            if pos.get("ticker") == ticker:
                return False, f"Already long {ticker}"
        return True, "OK"

    def open(self, trade):
        fee = round(trade["cost"] * CONFIG["stock_spread_pct"], 4)
        self.cash       -= trade["cost"] + fee
        self.total_fees  = round(self.total_fees + fee, 4)
        trade["entry_fee"]  = fee
        trade["peak_price"] = trade["entry_price"]
        self.trades_today  += 1
        self.open_positions[trade["pos_id"]] = trade

    def close(self, pos_id, pnl, reason=""):
        pos = self.open_positions.pop(pos_id, None)
        if not pos:
            return None
        fee = round(pos["cost"] * CONFIG["stock_spread_pct"], 4)
        pnl = round(pnl - fee, 4)
        self.cash       += pos["cost"] + pnl
        self.total_pnl  += pnl
        self.daily_pnl  += pnl
        self.total_fees  = round(self.total_fees + fee, 4)
        if pnl > 0:
            self.wins   += 1
        else:
            self.losses += 1
        entry = {**pos, "pnl": round(pnl, 4), "exit_fee": fee,
                 "exit_reason": reason,
                 "settled_at": datetime.now(ET).isoformat()}
        self.trade_history.append(entry)
        return entry

    def position_size(self, regime, high_vol):
        size = self.cash * CONFIG["max_position_size_pct"]
        if regime == "bull":
            size = min(size * 1.15, CONFIG["max_position_size_usd"])
        if high_vol:
            size *= 0.75
        return round(max(CONFIG["min_position_size_usd"],
                         min(size, CONFIG["max_position_size_usd"])), 2)

    def portfolio_value(self, live_prices):
        pos_val = 0.0
        for pos in self.open_positions.values():
            ticker = pos.get("ticker", "")
            live   = live_prices.get(ticker)
            entry  = pos.get("entry_price", 0)
            if live and entry:
                pos_val += pos["cost"] * (live / entry)
            else:
                pos_val += pos["cost"]
        return round(self.cash + pos_val, 2)

    def win_rate(self):
        total = self.wins + self.losses
        return round(self.wins / total * 100, 1) if total else 0.0

    def roi(self):
        return round(self.total_pnl / CONFIG["paper_budget"] * 100, 2)

    def status(self):
        return (f"Cash: ${self.cash:.2f} | P&L: ${self.total_pnl:+.2f} | "
                f"Daily: ${self.daily_pnl:+.2f} | Open: {len(self.open_positions)} | "
                f"W/L: {self.wins}/{self.losses} | ROI: {self.roi():+.1f}%")

    def to_dict(self):
        return {
            "cash":           round(self.cash, 4),
            "total_pnl":      round(self.total_pnl, 4),
            "daily_pnl":      round(self.daily_pnl, 4),
            "trades_today":   self.trades_today,
            "wins":           self.wins,
            "losses":         self.losses,
            "last_date":      self.last_date,
            "open_positions": self.open_positions,
            "trade_history":  self.trade_history[-500:],
            "wallet_history": self.wallet_history[-500:],
            "total_fees":     round(self.total_fees, 4),
        }

    def load_dict(self, d):
        self.cash           = d.get("cash", CONFIG["paper_budget"])
        self.total_pnl      = d.get("total_pnl", 0.0)
        self.daily_pnl      = d.get("daily_pnl", 0.0)
        self.trades_today   = d.get("trades_today", 0)
        self.wins           = d.get("wins", 0)
        self.losses         = d.get("losses", 0)
        self.last_date      = d.get("last_date", datetime.now(ET).date().isoformat())
        self.open_positions = d.get("open_positions", {})
        self.trade_history  = d.get("trade_history", [])
        self.wallet_history = d.get("wallet_history", [])
        self.total_fees     = d.get("total_fees", 0.0)


# ══════════════════════════════════════════════════════════════
# MODULE 6 — PENDING MANAGER
# ══════════════════════════════════════════════════════════════
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
            log.warning("Pending save failed: %s", e)

    def add(self, trade):
        data = self.load()
        ids  = {p["pos_id"] for p in data["pending"]}
        if trade["pos_id"] in ids:
            return
        data["pending"].append({
            **trade,
            "pending_since": datetime.now(ET).isoformat(),
            "expires_at":    (datetime.now(ET) + timedelta(
                              minutes=CONFIG["pending_expiry_mins"])).isoformat(),
            "status": "PENDING",
        })
        self.save(data)
        log.info("  [PENDING] %s | conf %.0f%%", trade["ticker"], trade["confidence"] * 100)

    def get_approved(self):
        data     = self.load()
        approved = [p for p in data.get("pending", []) if p.get("status") == "APPROVED"]
        approved += data.get("approved", [])
        data["approved"] = []
        data["pending"]  = [p for p in data.get("pending", []) if p.get("status") != "APPROVED"]
        self.save(data)
        return approved

    def expire_old(self):
        data   = self.load()
        now    = datetime.now(ET)
        before = len(data["pending"])
        data["pending"] = [
            p for p in data["pending"]
            if datetime.fromisoformat(p["expires_at"]) > now
        ]
        removed = before - len(data["pending"])
        if removed:
            log.info("  [PENDING] %d expired", removed)
            self.save(data)
        return data["pending"]

    def count(self):
        return len(self.load().get("pending", []))


# ══════════════════════════════════════════════════════════════
# MODULE 7 — STATE
# ══════════════════════════════════════════════════════════════
class State:
    @staticmethod
    def save(wallet):
        try:
            with open(CONFIG["state_file"], "w") as f:
                json.dump(wallet.to_dict(), f, indent=2)
        except Exception as e:
            log.warning("State save failed: %s", e)

    @staticmethod
    def load():
        try:
            with open(CONFIG["state_file"]) as f:
                return json.load(f)
        except:
            return None


# ══════════════════════════════════════════════════════════════
# MAIN BOT
# ══════════════════════════════════════════════════════════════
class JacobBot:

    def __init__(self):
        self.market         = MarketData()
        self.scorer         = SignalScorer()
        self.rules          = RulesEngine()
        self.regime_engine  = MarketRegime(self.market)
        self.wallet         = Wallet()
        self.pending        = PendingManager()
        self.scan_count     = 0
        self.rules_evals    = 0
        self.current_regime = "neutral"
        self.regime_spy5    = 0.0
        self.regime_qqq5    = 0.0
        self.next_scan_at   = None
        self._load_state()
        self._init_csv()

    def _load_state(self):
        d = State.load()
        if not d:
            log.info("[INIT] Fresh start — $%.2f", CONFIG["paper_budget"])
            return
        self.wallet.load_dict(d)
        log.info("[INIT] Loaded | cash=$%.2f | pnl=$%+.2f | open=%d",
                 self.wallet.cash, self.wallet.total_pnl, len(self.wallet.open_positions))

    def _init_csv(self):
        try:
            with open(CONFIG["csv_file"], "x", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp", "ticker", "direction", "entry_price",
                    "cost", "confidence", "reasoning", "exit_reason", "pnl", "mode",
                ])
        except FileExistsError:
            pass

    def _log_csv(self, trade, pnl=None):
        try:
            with open(CONFIG["csv_file"], "a", newline="") as f:
                csv.writer(f).writerow([
                    datetime.now(ET).isoformat(),
                    trade.get("ticker", ""), "LONG",
                    trade.get("entry_price", 0),
                    trade.get("cost", 0),
                    trade.get("confidence", 0),
                    str(trade.get("reasoning", ""))[:120],
                    trade.get("exit_reason", "") if pnl is not None else "",
                    pnl if pnl is not None else "",
                    "PAPER" if CONFIG["dry_run"] else "LIVE",
                ])
        except Exception as e:
            log.debug("CSV log failed: %s", e)

    def _pos_id(self, ticker):
        return f"{ticker}_{int(time.time())}"

    def _place_trade(self, trade, source="AUTO", premarket=False):
        if premarket:
            trade["pos_id"] = self._pos_id(trade["ticker"])
            self.pending.add(trade)
            log.info("  [PRE-MARKET] %s queued — will place at open", trade["ticker"])
            return False

        ok, reason = self.wallet.can_trade(trade["ticker"])
        if not ok:
            log.info("  [BLOCKED] %s: %s", trade["ticker"], reason)
            return False

        trade["pos_id"]    = self._pos_id(trade["ticker"])
        trade["opened_at"] = datetime.now(ET).isoformat()
        trade["source"]    = source
        trade["status"]    = "OPEN"
        self.wallet.open(trade)
        self._log_csv(trade)
        log.info("  [%s/%s] LONG %s $%.2f @ $%.4f | conf %.0f%%",
                 "PAPER" if CONFIG["dry_run"] else "LIVE", source,
                 trade["ticker"], trade["cost"], trade["entry_price"],
                 trade.get("confidence", 0) * 100)
        return True

    def _get_live_prices(self):
        prices = {}
        for pos in self.wallet.open_positions.values():
            ticker = pos.get("ticker", "")
            if ticker and ticker not in prices:
                p = self.market.get_live_price(ticker)
                if p:
                    prices[ticker] = p
        return prices

    def _check_exits(self, live_prices, scan_quotes):
        """
        Signal-based exits (checked every scan):
          1. Take-profit: live price >= entry * (1 + tp_pct)
          2. RSI overbought: daily RSI > rsi_exit
          3. Hard stop loss: live price <= entry * (1 - sl_pct)
          4. Quick cut: down >5% in first 24h
          5. Trailing stop: armed at +2.5%, fires at -1.5% from peak
          6. Max hold: age >= max_hold_days
        """
        now = datetime.now(ET)
        for pos_id in list(self.wallet.open_positions.keys()):
            pos    = self.wallet.open_positions.get(pos_id)
            if not pos:
                continue
            ticker = pos.get("ticker", "")
            entry  = pos.get("entry_price", 0)
            live   = live_prices.get(ticker)
            if not live or not entry:
                continue

            gain = (live - entry) / entry
            cost = pos["cost"]
            pnl  = round(cost * gain, 4)

            # Update peak for trailing stop
            peak = pos.get("peak_price", entry)
            if live > peak:
                pos["peak_price"] = peak = live

            # Age in hours
            try:
                age_hrs = (now - datetime.fromisoformat(
                    pos.get("opened_at", "")).astimezone(ET)
                ).total_seconds() / 3600
            except:
                age_hrs = 999

            exit_reason = None

            # 1. Take-profit
            if gain >= CONFIG["take_profit_pct"]:
                exit_reason = f"TP +{gain*100:.1f}%"

            # 2. RSI overbought (from scan quote if available)
            elif ticker in scan_quotes:
                q_rsi = scan_quotes[ticker].get("rsi", 50)
                if q_rsi > CONFIG["rsi_exit"] and gain > 0:
                    exit_reason = f"RSI_OB {q_rsi:.0f}"

            # 3. Hard stop
            if not exit_reason and gain <= -CONFIG["stop_loss_pct"]:
                exit_reason = f"STOP {gain*100:.1f}%"

            # 4. Quick cut — down >5% in first 24h
            elif not exit_reason and gain <= -CONFIG["quick_cut_pct"] and age_hrs < 24:
                exit_reason = f"QUICK_CUT {gain*100:.1f}% @{age_hrs:.0f}h"

            # 5. Trailing stop
            elif not exit_reason and gain > 0:
                peak_gain = (peak - entry) / entry
                if peak_gain >= CONFIG["trail_arm_pct"]:
                    pullback = (peak - live) / peak
                    if pullback >= CONFIG["trail_fire_pct"]:
                        exit_reason = f"TRAIL peak={peak_gain*100:.1f}% pb={pullback*100:.1f}%"

            # 6. Max hold safety net
            elif not exit_reason and age_hrs >= CONFIG["max_hold_days"] * 24:
                exit_reason = f"MAX_HOLD {age_hrs/24:.1f}d"

            if exit_reason:
                settled = self.wallet.close(pos_id, pnl, exit_reason)
                if settled:
                    State.save(self.wallet)
                    log.info("  [EXIT] %s %s | P&L: $%+.2f | cash=$%.2f",
                             ticker, exit_reason, pnl, self.wallet.cash)
                    self._log_csv(pos, pnl=pnl)

    def _write_dashboard(self, mood, scan_results, pending_bets, live_prices, regime, spy5, qqq5):
        try:
            portfolio_val = self.wallet.portfolio_value(live_prices)
            self.wallet.wallet_history.append({
                "t": datetime.now(ET).isoformat(),
                "v": portfolio_val,
            })
            if len(self.wallet.wallet_history) > 500:
                self.wallet.wallet_history = self.wallet.wallet_history[-500:]

            open_list = []
            for pos_id, pos in self.wallet.open_positions.items():
                enriched = {**pos, "pos_id": pos_id}
                ticker   = pos.get("ticker", "")
                live     = live_prices.get(ticker)
                entry    = pos.get("entry_price", 0)
                if live and entry:
                    enriched["live_price"]    = round(live, 2)
                    enriched["live_diff"]     = round(live - entry, 2)
                    enriched["live_diff_pct"] = round((live - entry) / entry * 100, 2)
                    enriched["tp_price"]      = round(entry * (1 + CONFIG["take_profit_pct"]), 4)
                    enriched["sl_price"]      = round(entry * (1 - CONFIG["stop_loss_pct"]), 4)
                open_list.append(enriched)

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "version":        "v5",
                    "lastScan":       datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":           "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":           mood,
                    "quote":          random.choice(QUOTES.get(mood, QUOTES["watching"])),
                    "wallet":         round(self.wallet.cash, 2),
                    "portfolioValue": portfolio_val,
                    "openValue":      round(portfolio_val - self.wallet.cash, 2),
                    "startingBudget": CONFIG["paper_budget"],
                    "totalPnl":       round(self.wallet.total_pnl, 4),
                    "dailyPnl":       round(self.wallet.daily_pnl, 4),
                    "wins":           self.wallet.wins,
                    "losses":         self.wallet.losses,
                    "winRate":        self.wallet.win_rate(),
                    "roi":            self.wallet.roi(),
                    "tradesTotal":    len(self.wallet.trade_history),
                    "totalFees":      round(self.wallet.total_fees, 4),
                    "openCount":      len(self.wallet.open_positions),
                    "scanCount":      self.scan_count,
                    "rulesEvals":     self.rules_evals,
                    "regime":         regime,
                    "regimeSpy5":     round(spy5, 2),
                    "regimeQqq5":     round(qqq5, 2),
                    "pendingCount":   len(pending_bets),
                    "nextScanAt":     self.next_scan_at,
                    "tpPct":          CONFIG["take_profit_pct"] * 100,
                    "slPct":          CONFIG["stop_loss_pct"] * 100,
                    "rsiEntry":       CONFIG["rsi_entry"],
                    "rsiExit":        CONFIG["rsi_exit"],
                    "scanResults":    scan_results[:20],
                    "openPositions":  open_list,
                    "recentTrades":   self.wallet.trade_history[-20:],
                    "pendingTrades":  pending_bets,
                    "walletHistory":  self.wallet.wallet_history[-500:],
                }, f, indent=2)
        except Exception as e:
            log.warning("Dashboard write failed: %s", e)

    def run_once(self, premarket=False):
        self.scan_count += 1
        self.wallet.reset_daily()

        log.info("=" * 64)
        log.info("  JACOB v5 #%d | %s%s | %s",
                 self.scan_count,
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 " [PRE-MARKET]" if premarket else "",
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"))
        log.info("  %s", self.wallet.status())
        log.info("=" * 64)

        # ── 1. Market regime ───────────────────────────────────────
        regime, spy5, qqq5 = self.regime_engine.get()
        self.current_regime = regime
        self.regime_spy5    = spy5
        self.regime_qqq5    = qqq5
        high_vol = self.regime_engine.high_vol_day

        # ── 2. Live prices + signal-based exits ───────────────────
        live_prices = self._get_live_prices()
        scan_quotes = {}  # built up during ticker scan below

        # Run exits with whatever live prices we have first
        self._check_exits(live_prices, scan_quotes)
        live_prices = self._get_live_prices()  # refresh after exits

        # ── 3. Process approved pending ───────────────────────────
        for trade in self.pending.get_approved():
            log.info("  [HUMAN APPROVED] %s", trade["ticker"])
            self._place_trade(trade, source="HUMAN", premarket=premarket)

        # ── 4. Expire stale pending ───────────────────────────────
        current_pending = self.pending.expire_old()
        pending_tickers = {p.get("ticker") for p in current_pending}

        # ── 5. Scan watchlist ─────────────────────────────────────
        scan_results  = []
        trades_placed = 0
        pending_added = 0

        for ticker in CONFIG["watchlist"]:
            log.info("  Scanning %s...", ticker)
            quote = self.market.get_quote(ticker)
            if not quote:
                log.info("  %s: no data — skip", ticker)
                scan_results.append({"ticker": ticker, "action": "skip",
                                     "skip_reason": "No data"})
                continue

            live_prices[ticker] = quote["price"]
            scan_quotes[ticker] = quote
            signal = self.scorer.score(quote)

            result = {
                "ticker":       ticker,
                "price":        quote["price"],
                "change_pct":   quote["change_pct"],
                "rsi":          quote["rsi"],
                "ma50":         quote.get("ma50"),
                "above_ma50":   quote.get("above_ma50", False),
                "momentum_5d":  quote["momentum_5d"],
                "volume_ratio": quote["volume_ratio"],
                "direction":    "LONG",
                "score":        signal["score"],
                "signals":      signal["signals"],
                "confidence":   0.0,
                "reasoning":    "",
                "action":       "skip",
            }

            # Gate 1: MA50
            if not quote.get("above_ma50", False):
                result["skip_reason"] = f"Below MA50 (${quote.get('ma50',0):.2f})"
                scan_results.append(result)
                time.sleep(0.3)
                continue

            # Gate 2: Score
            if signal["score"] < CONFIG["score_threshold"]:
                result["skip_reason"] = f"Score {signal['score']} < {CONFIG['score_threshold']}"
                scan_results.append(result)
                time.sleep(0.3)
                continue

            # Gate 3: Bear regime — no new longs
            if regime == "bear":
                result["action"]      = "skip"
                result["skip_reason"] = f"BEAR regime — no new longs (SPY 5d={spy5:+.1f}%)"
                scan_results.append(result)
                time.sleep(0.3)
                continue

            # Gate 4: Wallet capacity
            ok, reason = self.wallet.can_trade(ticker)
            if not ok:
                result["action"]      = "blocked"
                result["skip_reason"] = reason
                scan_results.append(result)
                time.sleep(0.3)
                continue

            # Gate 5: Sector concentration
            sector = self.rules.PROFILES.get(ticker, {}).get("sector", "unknown")
            sector_count = sum(
                1 for p in self.wallet.open_positions.values()
                if self.rules.PROFILES.get(p.get("ticker", ""), {}).get("sector") == sector
            )
            if sector_count >= CONFIG["max_per_sector"]:
                result["action"]      = "skip"
                result["skip_reason"] = f"Sector cap: {sector_count}/{CONFIG['max_per_sector']} {sector}"
                scan_results.append(result)
                time.sleep(0.3)
                continue

            # Rules engine analysis
            confirmed, confidence, reasoning = self.rules.analyse(quote, signal, regime)
            self.rules_evals += 1
            result["confidence"] = round(confidence, 2)
            result["reasoning"]  = reasoning

            size = self.wallet.position_size(regime, high_vol)
            trade = {
                "ticker":       ticker,
                "direction":    "LONG",
                "price":        quote["price"],
                "entry_price":  quote["price"],
                "cost":         size,
                "confidence":   round(confidence, 2),
                "reasoning":    reasoning,
                "score":        signal["score"],
                "signals":      signal["signals"],
                "ma50":         quote.get("ma50"),
                "rsi_entry":    quote["rsi"],
            }

            if confirmed and confidence >= CONFIG["auto_trade_confidence"]:
                placed = self._place_trade(trade, source="AUTO", premarket=premarket)
                result["action"] = "trade" if placed else "blocked"
                if placed:
                    trades_placed += 1
            elif confidence >= CONFIG["pending_confidence"] and ticker not in pending_tickers:
                trade["pos_id"] = self._pos_id(ticker)
                self.pending.add(trade)
                pending_tickers.add(ticker)
                result["action"] = "pending"
                pending_added   += 1
            else:
                result["action"]      = "skip"
                result["skip_reason"] = f"Low confidence ({confidence:.0%})"

            scan_results.append(result)
            time.sleep(0.4)

        # ── 6. Re-check exits with updated scan quotes ────────────
        self._check_exits(live_prices, scan_quotes)
        live_prices = self._get_live_prices()

        # ── 7. Save + dashboard ───────────────────────────────────
        State.save(self.wallet)
        current_pending = self.pending.load().get("pending", [])

        if trades_placed > 0:
            mood = "hunting"
        elif any(t["exit_reason"].startswith("TP") or t["exit_reason"].startswith("RSI_OB")
                 for t in self.wallet.trade_history[-trades_placed - 3:] if "exit_reason" in t):
            mood = "exiting"
        elif pending_added > 0:
            mood = "pending"
        elif regime == "bear":
            mood = "sleeping"
        elif any(r["score"] >= 58 for r in scan_results):
            mood = "watching"
        else:
            mood = "sleeping"

        log.info("\nScan done | tickers=%d | trades=%d | pending=%d | %s\n",
                 len(scan_results), trades_placed, pending_added, self.wallet.status())

        self._write_dashboard(mood, scan_results, current_pending,
                              live_prices, regime, spy5, qqq5)

    # ── Market hours ─────────────────────────────────────────────────────────
    def _market_open(self):
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        open_  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_ = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return open_ <= now < close_

    def _in_premarket(self):
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        return now.replace(hour=9, minute=0, second=0, microsecond=0) <= now < \
               now.replace(hour=9, minute=30, second=0, microsecond=0)

    def _secs_until_premarket(self):
        now  = datetime.now(ET)
        next_ = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_:
            next_ += timedelta(days=1)
        while next_.weekday() >= 5:
            next_ += timedelta(days=1)
        return max(0, int((next_ - now).total_seconds()))

    def run_loop(self):
        log.info("Jacob v5 starting | %s | $%.0f | %d tickers | MA50+RSI | long-only",
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 CONFIG["paper_budget"], len(CONFIG["watchlist"]))
        log.info("Entry: RSI<%d above MA50 | Exit: RSI>%d or +%.1f%% TP | SL: %.0f%%",
                 CONFIG["rsi_entry"], CONFIG["rsi_exit"],
                 CONFIG["take_profit_pct"] * 100, CONFIG["stop_loss_pct"] * 100)

        trigger_path = "jacob_trigger.json"
        interval     = CONFIG["scan_interval_sec"]

        while True:
            try:
                if self._market_open():
                    self.run_once(premarket=False)
                    self.next_scan_at = (
                        datetime.now(ET) + timedelta(seconds=interval)
                    ).isoformat()
                    log.info("Next scan in %ds...\n", interval)
                    elapsed = 0
                    while elapsed < interval:
                        time.sleep(10)
                        elapsed += 10
                        if os.path.exists(trigger_path):
                            try: os.remove(trigger_path)
                            except: pass
                            log.info("  [TRIGGER] Approval — scanning now")
                            break
                        if not self._market_open():
                            break

                elif self._in_premarket():
                    self.run_once(premarket=True)
                    log.info("Pre-market scan done — waiting for open\n")
                    time.sleep(60)

                else:
                    secs = self._secs_until_premarket()
                    wake = (datetime.now(ET) + timedelta(seconds=secs)).strftime("%I:%M %p ET")
                    log.info("Market closed — sleeping until %s (%dm)\n", wake, secs // 60)
                    self.next_scan_at = (
                        datetime.now(ET) + timedelta(seconds=secs)
                    ).isoformat()
                    elapsed = 0
                    while elapsed < secs:
                        time.sleep(60)
                        elapsed += 60
                        if os.path.exists(trigger_path):
                            try: os.remove(trigger_path)
                            except: pass
                            log.info("  [TRIGGER] Waking early")
                            break

            except KeyboardInterrupt:
                log.info("Jacob rests. Goodbye.")
                break
            except Exception as e:
                log.error("Unexpected error: %s", e, exc_info=True)
                time.sleep(30)


if __name__ == "__main__":
    bot = JacobBot()
    if "--once" in sys.argv:
        bot.run_once()
    else:
        bot.run_loop()
