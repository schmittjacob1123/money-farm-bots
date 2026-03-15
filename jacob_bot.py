"""
╔══════════════════════════════════════════════════════════════╗
║         JACOB'S STOCK & OPTIONS ENGINE  v4.1                ║
║  Strategy: Momentum + mean reversion scanner                 ║
║  Options:  CSP / Long Calls via rules engine                 ║
║  Human-in-the-loop for borderline trades                     ║
║  The lizard sees alpha where the market doesn't.            ║
╚══════════════════════════════════════════════════════════════╝

FIXES vs v1:
  - Settlement uses REAL current price, not random simulation
  - Scan interval uses CONFIG value (not hardcoded 300)
  - Options excluded from stock stop-loss check
  - ROI uses starting budget as denominator
  - Pre-market gate: scans but never places trades before 9:30am
  - Score threshold lowered to 62 (catches more setups in quiet markets)
  - daily_loss_cap fixed to starting value, not moving target
  - Dead code removed (open_ticker_dirs)
  - Position size cap corrected to match budget

NO AI API CALLS. Zero Anthropic credits. Pure rules engine.
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
    "paper_budget":          1000.0,
    "max_position_size_pct": 0.10,   # 10% of wallet per trade
    "max_position_size_usd": 80.0,   # v4.1: raised to 8% of $1000 budget
    "min_position_size_usd": 5.0,
    "max_open_positions":    8,
    "daily_loss_cap_pct":    0.05,   # 5% of STARTING budget = $25 fixed cap
    "max_trades_per_day":    10,
    "stop_loss_pct":         0.08,   # v3: tightened from 0.25   # cut stock positions at -25%

    # Signals
    "rsi_oversold":           30,   # v3: stricter oversold
    "rsi_overbought":         70,   # v3: stricter overbought
    "volume_surge_threshold": 1.5,
    "score_threshold":        62,    # lowered from 68 — catches more setups
    "auto_trade_confidence":  0.70,
    "pending_confidence":     0.55,
    "min_ai_confidence":      0.60,

    # Options
    "options_enabled":      True,
    "options_score_min":    72,
    "options_conf_min":     0.65,
    "csp_min_premium_pct":  1.0,     # min 1% premium vs strike
    "options_dte_min":      7,
    "options_dte_max":      45,

    # Holding periods
    "stock_hold_days":   2,   # v3: faster exits
    "options_hold_days": 14,         # raised from 3 — more realistic

    # Timing — used by run_loop
    "scan_interval_sec":  300,       # 5 min during market hours

    # v3: Regime detection thresholds (SPY+QQQ 5d momentum)
    "bear_threshold_pct":    -2.0,   # both below → BEAR (block longs)
    "bull_threshold_pct":     1.5,   # both above → BULL (boost longs)

    # v3: Trailing stop
    "trail_arm_pct":   0.03,   # arm when up +3% from entry
    "trail_fire_pct":  0.02,   # fire if drops -2% from peak

    # v3: Quick cut for fast losers
    "quick_cut_pct":   0.04,   # cut if down >4% within first 24h

    # v4.1: Sector concentration
    "max_per_sector":  2,       # max open positions per sector (tech/finance/etc)
    "premarket_wake_min": 30,        # wake 30 min before open

    # Pending
    "pending_expiry_mins": 1080,

    # Files
    "state_file":    "jacob_state.json",
    "dashboard_file":"jacob_data.json",
    "pending_file":  "jacob_pending.json",
    "csv_file":      "jacob_trades.csv",
    "log_file":      "jacob.log",
}

# Fixed daily loss cap (doesn't shrink as wallet drops)
DAILY_LOSS_CAP = CONFIG["paper_budget"] * CONFIG["daily_loss_cap_pct"]

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CONFIG["log_file"], encoding="utf-8")]
)
log = logging.getLogger("jacob")
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
        '"That RSI divergence just spoke to me. Position opened."',
        '"Volume spike confirmed. The lizard is in."',
        '"Market mispriced this. Classic. We are in."',
        '"IV crush incoming on that CSP. Premium collected."',
    ],
    "watching": [
        '"Scanning 20 tickers. The right setup is in here somewhere."',
        '"RSI cooling. Momentum building. Almost time."',
        '"Waiting for volume confirmation. Patience is alpha."',
        '"Nothing trades yet. Discipline beats FOMO."',
    ],
    "sleeping": [
        '"No edge above threshold. Cash is a position."',
        '"Choppy tape. Lizard doesn\'t trade chop."',
        '"Market close. Lizard rests. Watchlist ready for morning."',
        '"Zero trades = zero losses. Math is math."',
    ],
    "pending": [
        '"Found something with potential. Want your eyes on it first."',
        '"Borderline signal. Your call."',
        '"Options setup looks clean but IV is high. Review pending."',
    ],
}
ART = {
    "hunting":  ['"  .__.\n ( >.< )\n  )   (\n TRADES"', '"  .__.\n ( *>* )\n  )   (\n ALPHA!"'],
    "watching": ['"  .__.\n ( o.o )\n  )   (\n  . . ."', '"  .__.\n ( -.o )\n  )   (\n  hmm."'],
    "sleeping": ['"  .__.\n ( -.- )\n  )   (\n  zzz."'],
    "pending":  ['"  .__.\n ( ?.? )\n  )   (\n  ???"'],
}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════
class MarketData:
    """Fetches quotes and technicals from Yahoo Finance. Free, no API key."""

    _cache = {}
    CACHE_SECS = 240  # 4 min cache

    def _yahoo_quote(self, ticker):
        if ticker in self._cache:
            ts, data = self._cache[ticker]
            if time.time() - ts < self.CACHE_SECS:
                return data
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            r = requests.get(url, params={"range": "5d", "interval": "1h",
                             "includePrePost": "false"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.raise_for_status()
            result = r.json().get("chart", {}).get("result", [])
            if not result:
                return None
            meta    = result[0].get("meta", {})
            quote   = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes  = [c for c in quote.get("close",  []) if c is not None]
            volumes = [v for v in quote.get("volume", []) if v is not None]
            if not closes:
                return None
            data = {
                "ticker":     ticker,
                "price":      round(meta.get("regularMarketPrice", closes[-1]), 4),
                "prev_close": round(meta.get("chartPreviousClose", closes[0]),  4),
                "change_pct": 0.0,
                "volume":     meta.get("regularMarketVolume", volumes[-1] if volumes else 0),
                "avg_volume": int(sum(volumes[-20:]) / max(len(volumes[-20:]), 1)) if volumes else 0,
                "closes":     closes[-30:],
                "volumes":    volumes[-30:],
            }
            if data["prev_close"] > 0:
                data["change_pct"] = round(
                    (data["price"] - data["prev_close"]) / data["prev_close"] * 100, 2)
            self._cache[ticker] = (time.time(), data)
            return data
        except Exception as e:
            log.debug(f"Yahoo quote failed {ticker}: {e}")
            return None

    def _calc_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
        gains  = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        ag = sum(gains[:period])  / period
        al = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            ag = (ag * (period-1) + gains[i])  / period
            al = (al * (period-1) + losses[i]) / period
        if al == 0:
            return 100.0
        return round(100 - 100 / (1 + ag / al), 1)

    def _calc_momentum(self, closes, period=10):
        if len(closes) < period + 1:
            return 0.0
        return round((closes[-1] - closes[-period-1]) / max(closes[-period-1], 0.01) * 100, 2)

    def _mock(self, ticker):
        base = {"AAPL":227,"MSFT":415,"NVDA":875,"GOOGL":185,"AMZN":220,
                "META":575,"TSLA":248,"AMD":168,"NFLX":980,"JPM":235,
                "SPY":565,"QQQ":488,"GLD":245,"TLT":92,"BAC":44,
                "DIS":113,"PLTR":82,"COIN":255,"SNOW":148,"UBER":82}
        price   = base.get(ticker, 100) * (1 + random.uniform(-0.02, 0.02))
        closes  = [price * (1 + random.uniform(-0.01, 0.01)) for _ in range(30)]
        closes[-1] = price
        volumes = [random.randint(5_000_000, 50_000_000) for _ in range(30)]
        return {"ticker": ticker, "price": round(price, 2),
                "prev_close": round(closes[-2], 2),
                "change_pct": round((closes[-1]-closes[-2])/closes[-2]*100, 2),
                "volume": volumes[-1], "avg_volume": int(sum(volumes)/len(volumes)),
                "closes": [round(c,2) for c in closes], "volumes": volumes}

    def get_quote(self, ticker):
        data = self._yahoo_quote(ticker) or self._mock(ticker)
        closes  = data["closes"]
        volumes = data["volumes"]
        data["rsi"]          = self._calc_rsi(closes)
        data["momentum_5d"]  = self._calc_momentum(closes, 5)
        data["momentum_10d"] = self._calc_momentum(closes, 10)
        avg_vol = data["avg_volume"] or 1
        data["volume_ratio"] = round(data["volume"] / avg_vol, 2)
        return data

    def get_live_price(self, ticker):
        """Fetch latest price only — for settlement and stop loss checks."""
        # Bypass cache for live price
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
            log.debug(f"Live price failed {clean}: {e}")
            return None

    def get_options_chain(self, ticker):
        try:
            url = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.raise_for_status()
            chain = r.json().get("optionChain", {}).get("result", [])
            if not chain:
                return []
            expirations   = chain[0].get("expirationDates", [])
            current_price = chain[0].get("quote", {}).get("regularMarketPrice", 0)
            now     = datetime.utcnow()
            results = []
            dte_min = CONFIG["options_dte_min"]
            dte_max = CONFIG["options_dte_max"]
            for exp_ts in expirations:
                exp_dt = datetime.utcfromtimestamp(exp_ts)
                dte    = (exp_dt - now).days
                if not (dte_min <= dte <= dte_max):
                    continue
                exp_str = exp_dt.strftime("%Y-%m-%d")
                r2 = requests.get(
                    f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}?date={exp_ts}",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                r2.raise_for_status()
                opts = r2.json().get("optionChain", {}).get("result", [{}])[0].get("options", [{}])[0]
                for put in opts.get("puts", []):
                    strike = put.get("strike", 0)
                    if not (current_price * 0.90 <= strike <= current_price * 1.01):
                        continue
                    bid = put.get("bid", 0) or 0
                    ask = put.get("ask", 0) or 0
                    iv  = put.get("impliedVolatility", 0) or 0
                    if bid <= 0:
                        continue
                    results.append({
                        "type": "put", "strategy": "CSP", "ticker": ticker,
                        "strike": strike, "expiry": exp_str, "dte": dte,
                        "bid": round(bid,2), "ask": round(ask,2),
                        "mid": round((bid+ask)/2, 2),
                        "iv":  round(iv*100, 1),
                        "volume": put.get("volume", 0) or 0,
                        "current_price": current_price,
                        "otm_pct":     round((current_price-strike)/current_price*100, 1),
                        "premium_pct": round(bid/strike*100, 2) if strike else 0,
                    })
                for call in opts.get("calls", []):
                    strike = call.get("strike", 0)
                    if not (current_price * 0.99 <= strike <= current_price * 1.08):
                        continue
                    bid = call.get("bid", 0) or 0
                    ask = call.get("ask", 0) or 0
                    iv  = call.get("impliedVolatility", 0) or 0
                    if bid <= 0:
                        continue
                    results.append({
                        "type": "call", "strategy": "CALL", "ticker": ticker,
                        "strike": strike, "expiry": exp_str, "dte": dte,
                        "bid": round(bid,2), "ask": round(ask,2),
                        "mid": round((bid+ask)/2, 2),
                        "iv":  round(iv*100, 1),
                        "volume": call.get("volume", 0) or 0,
                        "current_price": current_price,
                        "otm_pct":     round((strike-current_price)/current_price*100, 1),
                        "premium_pct": round(bid/strike*100, 2) if strike else 0,
                    })
                time.sleep(0.2)
                break  # first qualifying expiry only
            return sorted(results, key=lambda x: x["premium_pct"], reverse=True)
        except Exception as e:
            log.debug(f"Options chain failed {ticker}: {e}")
            return []


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2: SIGNAL SCORER
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1b: MARKET REGIME DETECTOR                                      [v3]
# ══════════════════════════════════════════════════════════════════════════════
class MarketRegime:
    """
    Fetches SPY + QQQ once per scan to determine broad market direction.
      BEAR   → SPY and QQQ both down >2% over 5 days — block new longs
      BULL   → SPY and QQQ both up  >1.5% over 5 days — boost long confidence
      NEUTRAL→ everything else — normal operation
    """
    def __init__(self, market):
        self.market      = market
        self._cache      = "neutral"
        self._spy5       = 0.0
        self._qqq5       = 0.0
        self._spy_chg    = 0.0
        self._cache_time = None

    def get(self):
        """Returns (regime, spy_5d, qqq_5d). Cached per scan (~5 min)."""
        now = datetime.now(ET)
        if self._cache_time and (now - self._cache_time).total_seconds() < 290:
            return self._cache, self._spy5, self._qqq5
        try:
            spy = self.market.get_quote("SPY")
            qqq = self.market.get_quote("QQQ")
            spy5 = spy.get("momentum_5d", 0)
            qqq5 = qqq.get("momentum_5d", 0)
            self._spy_chg = spy.get("change_pct", 0)

            if spy5 < CONFIG["bear_threshold_pct"] and qqq5 < CONFIG["bear_threshold_pct"]:
                regime = "bear"
            elif spy5 > CONFIG["bull_threshold_pct"] and qqq5 > CONFIG["bull_threshold_pct"]:
                regime = "bull"
            else:
                regime = "neutral"

            self._cache      = regime
            self._spy5       = spy5
            self._qqq5       = qqq5
            self._cache_time = now
            log.info("  [REGIME] SPY 5d=%+.1f%% | QQQ 5d=%+.1f%% | Today=%+.1f%% → %s",
                     spy5, qqq5, self._spy_chg, regime.upper())
        except Exception as e:
            log.warning("  [REGIME] Fetch failed: %s — using %s", e, self._cache.upper())
        return self._cache, self._spy5, self._qqq5

    @property
    def high_vol_day(self):
        """True if today's SPY move >2% — reduce size on high vol days."""
        return abs(self._spy_chg) > 2.0


class SignalScorer:
    """Scores each ticker 0-100. High score = strong setup."""

    def score(self, quote):
        rsi       = quote.get("rsi", 50)
        mom5      = quote.get("momentum_5d",  0)
        mom10     = quote.get("momentum_10d", 0)
        vol_ratio = quote.get("volume_ratio", 1.0)
        chg_pct   = quote.get("change_pct",   0)

        score     = 50
        direction = "LONG"
        signals   = []

        # RSI
        if rsi < CONFIG["rsi_oversold"]:
            score += 15; signals.append(f"RSI oversold ({rsi:.0f})"); direction = "LONG"
        elif rsi > CONFIG["rsi_overbought"]:
            score += 10; signals.append(f"RSI overbought ({rsi:.0f})"); direction = "SHORT"
        elif 40 <= rsi <= 60:
            signals.append(f"RSI neutral ({rsi:.0f})")

        # Momentum
        if mom5 > 3:
            score += 12; signals.append(f"Strong 5d momentum (+{mom5:.1f}%)"); direction = "LONG"
        elif mom5 < -3:
            score += 12; signals.append(f"Negative 5d momentum ({mom5:.1f}%)"); direction = "SHORT"

        if mom10 > 5:
            score += 8; signals.append(f"10d uptrend (+{mom10:.1f}%)")
        elif mom10 < -5:
            score += 8; signals.append(f"10d downtrend ({mom10:.1f}%)")

        # Volume
        if vol_ratio >= CONFIG["volume_surge_threshold"]:
            score += 12; signals.append(f"Volume surge ({vol_ratio:.1f}x avg)")

        # Daily move
        if abs(chg_pct) > 2:
            score += 8; signals.append(f"Big daily move ({chg_pct:+.1f}%)")

        return {"score": min(score, 100), "direction": direction, "signals": signals}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: RULES ENGINE (zero-cost AI analyst)
# ══════════════════════════════════════════════════════════════════════════════
class RulesEngine:
    """
    Jacob's market knowledge distilled into rules.
    No API calls. No credits. Mirrors what Claude would evaluate.
    """

    PROFILES = {
        "AAPL":  {"sector":"tech",      "beta":1.2,  "earnings":True,  "tq":"high",   "note":"Strong brand moat. Momentum trades well. Avoid before earnings."},
        "MSFT":  {"sector":"tech",      "beta":0.9,  "earnings":True,  "tq":"high",   "note":"Cloud+AI tailwind. Lower beta, dips recover. CSP-friendly."},
        "NVDA":  {"sector":"semis",     "beta":1.9,  "earnings":True,  "tq":"high",   "note":"AI capex dominant. High beta — volume surges are meaningful."},
        "GOOGL": {"sector":"tech",      "beta":1.1,  "earnings":True,  "tq":"high",   "note":"Ad revenue + cloud. Solid floor. Lags NVDA in momentum."},
        "AMZN":  {"sector":"tech",      "beta":1.3,  "earnings":True,  "tq":"high",   "note":"AWS growth driver. Momentum works in bull tape."},
        "META":  {"sector":"tech",      "beta":1.4,  "earnings":True,  "tq":"high",   "note":"Ad market dominant. Momentum very reliable."},
        "TSLA":  {"sector":"ev",        "beta":2.2,  "earnings":True,  "tq":"low",    "note":"Narrative-driven. Technicals unreliable — skip unless volume massive."},
        "AMD":   {"sector":"semis",     "beta":1.8,  "earnings":True,  "tq":"medium", "note":"NVDA competitor. Volume surges worth acting on."},
        "NFLX":  {"sector":"streaming", "beta":1.3,  "earnings":True,  "tq":"medium", "note":"Ad tier tailwind. Momentum trades OK."},
        "JPM":   {"sector":"finance",   "beta":1.1,  "earnings":True,  "tq":"medium", "note":"Best-in-class bank. CSP works well — strong floor."},
        "SPY":   {"sector":"etf",       "beta":1.0,  "earnings":False, "tq":"high",   "note":"S&P 500 ETF. Very reliable technicals. CSP in dips classic."},
        "QQQ":   {"sector":"etf",       "beta":1.2,  "earnings":False, "tq":"high",   "note":"Nasdaq 100 ETF. High IV — CSP premium attractive in pullbacks."},
        "GLD":   {"sector":"commodity", "beta":0.1,  "earnings":False, "tq":"medium", "note":"Gold. Inverse to real rates. Momentum works in risk-off."},
        "TLT":   {"sector":"bonds",     "beta":-0.3, "earnings":False, "tq":"medium", "note":"20yr Treasury. Inversely correlated to rates. Short momentum reliable."},
        "BAC":   {"sector":"finance",   "beta":1.3,  "earnings":True,  "tq":"medium", "note":"Rate-sensitive bank. More beta than JPM. Wider stop needed."},
        "DIS":   {"sector":"media",     "beta":1.1,  "earnings":True,  "tq":"low",    "note":"Turnaround story, fundamentals mixed. Be cautious on momentum."},
        "PLTR":  {"sector":"tech",      "beta":1.7,  "earnings":True,  "tq":"low",    "note":"High beta, momentum bursts but mean-reverts hard. Volume surge key."},
        "COIN":  {"sector":"crypto",    "beta":2.5,  "earnings":True,  "tq":"low",    "note":"Crypto proxy. Only trade on strong volume + momentum alignment."},
        "SNOW":  {"sector":"tech",      "beta":1.6,  "earnings":True,  "tq":"medium", "note":"Cloud data. AI tailwind returning. Volume surges meaningful."},
        "UBER":  {"sector":"transport", "beta":1.4,  "earnings":True,  "tq":"medium", "note":"Profitable now. Momentum trades well."},
    }

    SECTOR_WEIGHT = {
        "tech":1.15, "semis":1.20, "etf":1.00, "finance":0.90,
        "streaming":0.95, "ev":0.85, "crypto":0.80, "commodity":0.90,
        "bonds":0.95, "media":0.80, "transport":0.95,
    }

    IV_HIGH = 40
    IV_LOW  = 20

    def _profile(self, ticker):
        return self.PROFILES.get(ticker, {
            "sector":"unknown","beta":1.0,"earnings":False,"tq":"medium",
            "note":"Unknown ticker — applying neutral rules."
        })

    def _regime(self, quote):
        rsi = quote.get("rsi", 50); mom5 = quote.get("momentum_5d", 0)
        vol = quote.get("volume_ratio", 1.0)
        if rsi < 35 and mom5 < -2: return "oversold_bounce"
        if rsi > 65 and mom5 > 2:  return "overbought"
        if abs(mom5) > 3 and vol > 1.3: return "trending"
        return "choppy"

    def analyse_stock(self, quote, signal):
        ticker = quote["ticker"]; rsi = quote.get("rsi",50)
        mom5 = quote.get("momentum_5d",0); mom10 = quote.get("momentum_10d",0)
        vol   = quote.get("volume_ratio",1.0); chg = quote.get("change_pct",0)
        direction = signal["direction"]; score = signal["score"]
        p = self._profile(ticker)
        tq = p["tq"]; beta = p["beta"]; sector = p["sector"]
        regime = self._regime(quote)
        reasons = []
        conf = 0.50

        # Trend quality gate — low quality tickers need stronger signals
        tq_bonus = {"high":0,"medium":5,"low":10}[tq]
        if score < CONFIG["score_threshold"] + tq_bonus:
            return False, 0.38, f"{ticker} needs score {CONFIG['score_threshold']+tq_bonus}+ (tq:{tq})"

        # Sector weight
        conf += (self.SECTOR_WEIGHT.get(sector, 1.0) - 1.0) * 0.15

        if direction == "LONG":
            if rsi < 35:       conf += 0.12; reasons.append(f"RSI {rsi:.0f} oversold — bounce setup")
            elif rsi < 50:     conf += 0.06; reasons.append(f"RSI {rsi:.0f} cooling, room to run")
            elif rsi > 65:     conf -= 0.10; reasons.append(f"RSI {rsi:.0f} extended — chasing")

            if mom5 > 4 and mom10 > 6:  conf += 0.10; reasons.append(f"Momentum aligned +{mom5:.1f}%/+{mom10:.1f}%")
            elif mom5 > 2:              conf += 0.05; reasons.append(f"Positive 5d momentum +{mom5:.1f}%")
            elif mom5 < -1 and rsi > 50: conf -= 0.08; reasons.append("Momentum fading while RSI elevated")

            if vol >= 2.0:   conf += 0.12; reasons.append(f"Strong volume surge {vol:.1f}x — conviction")
            elif vol >= 1.5: conf += 0.07; reasons.append(f"Volume surge {vol:.1f}x above average")
            elif vol < 0.7:  conf -= 0.08; reasons.append("Low volume — weak conviction")

            if regime == "trending":       conf += 0.08; reasons.append("Trending regime")
            elif regime == "choppy":       conf -= 0.06; reasons.append("Choppy tape")
            elif regime == "overbought":   conf -= 0.10; reasons.append("Overbought — late entry risk")

            if beta > 1.8 and vol < 1.5:  conf -= 0.08; reasons.append(f"High beta ({beta}) without volume")

        elif direction == "SHORT":
            if rsi > 65:     conf += 0.10; reasons.append(f"RSI {rsi:.0f} overbought")
            elif rsi < 50:   conf -= 0.08; reasons.append("RSI not elevated — risky short")

            if mom5 < -3:    conf += 0.10; reasons.append(f"Negative momentum {mom5:.1f}%")
            elif mom5 > 2:   conf -= 0.12; reasons.append("Shorting into positive momentum")

            if vol >= 1.5:   conf += 0.07; reasons.append(f"Volume on downside — distribution")
            if sector in ("etf","tech") and mom10 > 0:
                conf -= 0.08; reasons.append("Shorting tech/ETF in uptrend")

        # Earnings risk
        if p["earnings"] and abs(chg) > 4:
            conf -= 0.12; reasons.append(f"Large move ({chg:+.1f}%) on earnings-sensitive ticker")

        # Crypto/EV in chop
        if sector in ("crypto","ev") and regime == "choppy":
            conf -= 0.10; reasons.append(f"{sector.title()} in choppy tape — noisy")

        # ETF bonus
        if sector == "etf":
            conf += 0.06; reasons.append("ETF — reliable technicals")

        conf = round(max(0.0, min(1.0, conf)), 3)
        reasoning = p["note"] + " | " + "; ".join(reasons[:3]) if reasons else p["note"]
        return conf >= CONFIG["min_ai_confidence"], conf, reasoning

    def analyse_option(self, opt, quote, signal):
        strategy = opt.get("strategy","CSP"); iv = opt.get("iv",30)
        dte = opt.get("dte",14); otm = opt.get("otm_pct",2.0)
        prem = opt.get("premium_pct",0); rsi = quote.get("rsi",50)
        mom5 = quote.get("momentum_5d",0); vol = quote.get("volume_ratio",1.0)
        p = self._profile(opt["ticker"])
        reasons = []; conf = 0.52

        if strategy == "CSP":
            if iv >= self.IV_HIGH:     conf += 0.12; reasons.append(f"IV {iv:.0f}% elevated — rich premium")
            elif iv >= 30:             conf += 0.06; reasons.append(f"IV {iv:.0f}% moderate")
            elif iv < self.IV_LOW:     conf -= 0.10; reasons.append(f"IV {iv:.0f}% low — thin premium")

            if otm >= 5:               conf += 0.10; reasons.append(f"{otm:.1f}% OTM — good buffer")
            elif otm >= 3:             conf += 0.05; reasons.append(f"{otm:.1f}% OTM — moderate buffer")
            elif otm < 2:              conf -= 0.10; reasons.append(f"Only {otm:.1f}% OTM — assignment risk")

            if prem >= 2:              conf += 0.08; reasons.append(f"{prem:.2f}% premium — attractive")
            elif prem < 0.8:           conf -= 0.08; reasons.append(f"Only {prem:.2f}% premium — not worth it")

            if rsi < 35:               conf -= 0.10; reasons.append("Stock oversold — downside real")
            elif rsi > 50:             conf += 0.06; reasons.append("Stock above RSI midpoint — CSP backdrop good")

            if mom5 < -3:              conf -= 0.12; reasons.append(f"Stock downtrending — CSP into falling knife")
            elif mom5 > 1:             conf += 0.06; reasons.append(f"Stock trending up — CSP aligned")

            if 14 <= dte <= 35:        conf += 0.06; reasons.append(f"{dte} DTE — theta sweet spot")
            elif dte < 7:              conf -= 0.08; reasons.append(f"{dte} DTE — gamma risk")
            elif dte > 40:             conf -= 0.04; reasons.append(f"{dte} DTE — capital tied up too long")

            if p["beta"] > 1.8 and otm < 5:
                conf -= 0.10; reasons.append(f"High beta ({p['beta']}) — needs >5% OTM")

        elif strategy == "CALL":
            if iv > self.IV_HIGH:      conf -= 0.12; reasons.append(f"IV {iv:.0f}% — long call expensive")
            elif iv < 30:              conf += 0.10; reasons.append(f"IV {iv:.0f}% — options cheap")

            if mom5 > 4:               conf += 0.12; reasons.append(f"Strong momentum {mom5:+.1f}%")
            elif mom5 > 2:             conf += 0.06; reasons.append(f"Positive momentum {mom5:+.1f}%")
            elif mom5 < 0:             conf -= 0.12; reasons.append("Negative momentum — wrong direction")

            if vol >= 2.0:             conf += 0.10; reasons.append(f"Volume surge {vol:.1f}x — institutional buying")
            elif vol < 1.0:            conf -= 0.06; reasons.append("Low volume — lacks conviction")

            if rsi > 70:               conf -= 0.10; reasons.append(f"RSI {rsi:.0f} overbought — late for call")
            elif 45 <= rsi <= 65:      conf += 0.06; reasons.append(f"RSI {rsi:.0f} — room to run")

            if dte < 14:               conf -= 0.10; reasons.append(f"Only {dte} DTE — not enough time")
            elif 21 <= dte <= 45:      conf += 0.06; reasons.append(f"{dte} DTE — enough runway")

        conf = round(max(0.0, min(1.0, conf)), 3)
        reasoning = "; ".join(reasons[:3]) if reasons else f"{strategy} evaluated"
        return conf >= CONFIG["min_ai_confidence"], conf, reasoning


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: WALLET (single source of truth)
# ══════════════════════════════════════════════════════════════════════════════
class Wallet:
    """
    Clean accounting:
      open_position(cost)  → wallet -= cost
      close_position(cost, pnl) → wallet += cost + pnl
      daily_pnl tracks REALIZED pnl from closes only.
      daily_loss_cap is fixed at 5% of starting budget.
    """

    def __init__(self):
        self.cash           = CONFIG["paper_budget"]
        self.total_pnl      = 0.0
        self.daily_pnl      = 0.0
        self.trades_today   = 0
        self.last_date      = datetime.now(ET).date().isoformat()
        self.open_positions = {}   # pos_id → trade dict
        self.trade_history  = []
        self.wallet_history = []

    def reset_daily_if_needed(self):
        today = datetime.now(ET).date().isoformat()
        if today != self.last_date:
            self.daily_pnl    = 0.0
            self.trades_today = 0
            self.last_date    = today
            log.info("[WALLET] Daily reset")

    def can_trade(self, ticker, direction="LONG"):
        self.reset_daily_if_needed()
        if self.daily_pnl        <= -DAILY_LOSS_CAP:              return False, f"Daily loss cap hit (${DAILY_LOSS_CAP:.0f})"
        if len(self.open_positions) >= CONFIG["max_open_positions"]: return False, "Max open positions"
        if self.trades_today     >= CONFIG["max_trades_per_day"]:  return False, "Max trades today"
        if self.cash             < CONFIG["min_position_size_usd"]: return False, "Wallet too low"
        for pos in self.open_positions.values():
            if pos.get("ticker") == ticker and pos.get("direction") == direction:
                return False, f"Duplicate: already {direction} {ticker}"
        return True, "OK"

    def open(self, trade):
        cost = trade["cost"]
        self.cash -= cost
        self.trades_today += 1
        self.open_positions[trade["pos_id"]] = trade

    def close(self, pos_id, pnl):
        pos = self.open_positions.pop(pos_id, None)
        if not pos:
            return None
        self.cash       += pos["cost"] + pnl
        self.total_pnl  += pnl
        self.daily_pnl  += pnl
        entry = {**pos, "pnl": round(pnl,4), "settled_at": datetime.now(ET).isoformat()}
        self.trade_history.append(entry)
        return entry

    def position_size(self):
        size = self.cash * CONFIG["max_position_size_pct"]
        return max(CONFIG["min_position_size_usd"],
                   min(round(size, 2), CONFIG["max_position_size_usd"]))

    def win_rate(self):
        closed = [t for t in self.trade_history if "pnl" in t]
        if not closed: return 0.0
        return round(sum(1 for t in closed if t["pnl"] > 0) / len(closed) * 100, 1)

    def roi(self):
        # ROI vs starting budget — clean and consistent
        return round(self.total_pnl / CONFIG["paper_budget"] * 100, 2)

    def portfolio_value(self, live_prices):
        """Cash + current market value of all open stock positions."""
        pos_value = 0.0
        for pos in self.open_positions.values():
            if pos.get("type") == "option":
                # Options: just use cost as conservative value (no mark-to-market)
                pos_value += pos["cost"]
                continue
            ticker = pos.get("ticker","").replace("_OPT","")
            live   = live_prices.get(ticker)
            entry  = pos.get("entry_price", 0)
            if live and entry:
                direction = pos.get("direction","LONG")
                if direction == "LONG":
                    pos_value += pos["cost"] * (live / entry)
                else:  # SHORT: profit when price drops
                    pos_value += pos["cost"] * (2 - live / entry)
            else:
                pos_value += pos["cost"]
        return round(self.cash + pos_value, 2)

    def status(self):
        return (f"Cash: ${self.cash:.2f} | P&L: ${self.total_pnl:+.2f} | "
                f"Daily: ${self.daily_pnl:+.2f} | Open: {len(self.open_positions)} | "
                f"Win rate: {self.win_rate():.0f}% | ROI: {self.roi():+.1f}%")

    def to_dict(self):
        return {
            "cash":           round(self.cash, 4),
            "total_pnl":      round(self.total_pnl, 4),
            "daily_pnl":      round(self.daily_pnl, 4),
            "trades_today":   self.trades_today,
            "last_date":      self.last_date,
            "open_positions": self.open_positions,
            "trade_history":  self.trade_history[-500:],
            "wallet_history": self.wallet_history[-500:],
        }

    def load_dict(self, d):
        self.cash           = d.get("cash", CONFIG["paper_budget"])
        self.total_pnl      = d.get("total_pnl", 0.0)
        self.daily_pnl      = d.get("daily_pnl", 0.0)
        self.trades_today   = d.get("trades_today", 0)
        self.last_date      = d.get("last_date", datetime.now(ET).date().isoformat())
        self.open_positions = d.get("open_positions", {})
        self.trade_history  = d.get("trade_history", [])
        self.wallet_history = d.get("wallet_history", [])


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 5: PENDING MANAGER
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
            log.warning(f"Pending save failed: {e}")

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
        log.info(f"  [PENDING] {trade['ticker']} {trade['direction']} — {trade['ai_confidence']:.0%}")

    def get_approved(self):
        data     = self.load()
        approved = [p for p in data.get("pending", []) if p.get("status") == "APPROVED"]
        approved += [p for p in data.get("approved", []) if p.get("status") == "APPROVED"]
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
            log.info(f"  [PENDING] {removed} expired")
            self.save(data)
        return data["pending"]

    def count(self):
        return len(self.load().get("pending", []))


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 6: STATE
# ══════════════════════════════════════════════════════════════════════════════
class State:
    @staticmethod
    def save(wallet):
        try:
            with open(CONFIG["state_file"], "w") as f:
                json.dump(wallet.to_dict(), f, indent=2)
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
class JacobBot:

    def __init__(self):
        self.market        = MarketData()
        self.scorer        = SignalScorer()
        self.rules         = RulesEngine()
        self.regime_engine = MarketRegime(self.market)
        self.current_regime = "neutral"
        self.regime_spy5    = 0.0
        self.regime_qqq5    = 0.0
        self.wallet   = Wallet()
        self.pending  = PendingManager()
        self.scan_count  = 0
        self.rules_evals = 0
        self.next_scan_at = None
        self._load_state()

        # CSV log
        try:
            with open(CONFIG["csv_file"], "x", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp","ticker","type","direction","entry_price",
                    "strike","expiry","cost","ai_confidence","ai_reasoning","pnl","status"
                ])
        except FileExistsError:
            pass

    def _load_state(self):
        d = State.load()
        if not d:
            log.info("[INIT] Fresh start — $%.2f", CONFIG["paper_budget"])
            return
        self.wallet.load_dict(d)
        log.info("[INIT] Loaded | cash=$%.2f | total_pnl=$%+.2f | open=%d",
                 self.wallet.cash, self.wallet.total_pnl, len(self.wallet.open_positions))

    def _pos_id(self, ticker, trade_type):
        return f"{ticker}_{trade_type}_{int(time.time())}"

    def _log_csv(self, trade, pnl=None):
        try:
            with open(CONFIG["csv_file"], "a", newline="") as f:
                csv.writer(f).writerow([
                    datetime.now(ET).isoformat(),
                    trade.get("ticker",""), trade.get("type","stock"),
                    trade.get("direction","LONG"),
                    trade.get("entry_price", trade.get("price",0)),
                    trade.get("strike",""), trade.get("expiry",""),
                    trade.get("cost",0), trade.get("ai_confidence",0),
                    str(trade.get("ai_reasoning",""))[:100],
                    pnl if pnl is not None else "",
                    "PAPER" if CONFIG["dry_run"] else "LIVE",
                ])
        except Exception as e:
            log.debug(f"CSV log failed: {e}")

    def _place_trade(self, trade, source="AUTO", premarket=False):
        """Place a trade. Blocked before 9:30am ET (premarket=True)."""
        if premarket:
            log.info(f"  [PRE-MARKET] {trade['ticker']} queued — will place at open")
            # Add to pending instead
            trade["pos_id"] = self._pos_id(trade["ticker"], trade.get("type","stock"))
            self.pending.add(trade)
            return False

        direction = trade.get("direction","LONG")
        ok, reason = self.wallet.can_trade(trade["ticker"], direction)
        if not ok:
            log.info(f"  Blocked: {reason}")
            return False

        trade["pos_id"]    = self._pos_id(trade["ticker"], trade.get("type","stock"))
        trade["opened_at"] = datetime.now(ET).isoformat()
        trade["source"]    = source
        trade["status"]    = "OPEN"

        self.wallet.open(trade)
        self._log_csv(trade)
        log.info("  [%s/%s] %s %s $%.2f | %s | conf %.0f%%",
                 "PAPER" if CONFIG["dry_run"] else "LIVE", source,
                 trade["ticker"], direction, trade["cost"],
                 trade.get("type","stock"), trade.get("ai_confidence",0)*100)
        return True

    def _get_live_prices(self):
        """Fetch current prices for all open stock positions."""
        prices = {}
        for pos in self.wallet.open_positions.values():
            if pos.get("type") == "option":
                continue
            ticker = pos.get("ticker","").replace("_OPT","")
            if ticker and ticker not in prices:
                p = self.market.get_live_price(ticker)
                if p:
                    prices[ticker] = p
        return prices

    def _check_stop_losses(self, live_prices):
        """
        v3 stop logic (per position, in priority order):
          1. Hard stop loss      — position down >8% from entry
          2. Quick cut           — down >4% within first 24h (fast losers out early)
          3. Trailing stop       — armed at +3%, fires at -2% from peak
          4. Standard close      — position expired (handled in _settle_positions)
        Also updates peak_price on winning positions each scan.
        """
        now = datetime.now(ET)
        for pos_id in list(self.wallet.open_positions.keys()):
            pos = self.wallet.open_positions.get(pos_id)
            if not pos or pos.get("type") == "option":
                continue
            ticker    = pos.get("ticker", "").replace("_OPT", "")
            direction = pos.get("direction", "LONG")
            entry     = pos.get("entry_price", 0)
            live      = live_prices.get(ticker)
            if not live or not entry:
                continue

            move = (live - entry) / entry
            gain = move if direction == "LONG" else -move  # positive = winning

            # Update peak price for trailing stop
            peak = pos.get("peak_price", entry)
            if direction == "LONG":
                new_peak = max(peak, live)
            else:
                new_peak = min(peak, live) if peak != entry else live
            pos["peak_price"] = new_peak

            # Age of position in hours
            try:
                age_hrs = (now - datetime.fromisoformat(pos.get("opened_at","")).astimezone(ET)).total_seconds() / 3600
            except:
                age_hrs = 999

            stop_reason = None
            pnl         = round(pos["cost"] * gain, 4)

            # 1. Hard stop loss
            if gain <= -CONFIG["stop_loss_pct"]:
                stop_reason = f"STOP LOSS {gain*100:.1f}%"

            # 2. Quick cut — down >4% in first 24h
            elif gain <= -CONFIG["quick_cut_pct"] and age_hrs < 24:
                stop_reason = f"QUICK CUT {gain*100:.1f}% <24h"

            # 3. Trailing stop — only arm if ever up >3%
            elif gain > 0:
                peak_gain = (new_peak - entry) / entry if direction == "LONG" else (entry - new_peak) / entry
                if peak_gain >= CONFIG["trail_arm_pct"]:
                    # How much has it given back from peak?
                    if direction == "LONG":
                        pullback = (new_peak - live) / new_peak
                    else:
                        pullback = (live - new_peak) / new_peak
                    if pullback >= CONFIG["trail_fire_pct"]:
                        stop_reason = f"TRAIL STOP peak={peak_gain*100:.1f}% pullback={pullback*100:.1f}%"

            if stop_reason:
                settled = self.wallet.close(pos_id, pnl)
                if settled:
                    State.save(self.wallet)
                    log.warning("  [%s] %s %s | P&L: $%+.2f | Cash: $%.2f",
                                stop_reason, ticker, direction, pnl, self.wallet.cash)
                    self._log_csv(pos, pnl=pnl)

    def _settle_positions(self, live_prices):
        """
        Settle positions past their hold period using REAL current price.
        No random simulation — actual P&L based on where the stock is now.
        Options use simplified settlement (premium collected/lost).
        """
        for pos_id in list(self.wallet.open_positions.keys()):
            pos      = self.wallet.open_positions.get(pos_id)
            if not pos:
                continue
            opened   = pos.get("opened_at","")
            pos_type = pos.get("type","stock")
            hold_days = CONFIG["options_hold_days"] if pos_type == "option" else CONFIG["stock_hold_days"]
            try:
                age_hrs = (datetime.now(ET) - datetime.fromisoformat(opened).astimezone(ET)).total_seconds() / 3600
                if age_hrs < hold_days * 24:
                    continue
            except:
                continue

            ticker    = pos.get("ticker","").replace("_OPT","")
            direction = pos.get("direction","LONG")
            entry     = pos.get("entry_price",0)
            cost      = pos.get("cost",0)

            if pos_type == "option":
                # v4: Realistic options settlement using theta decay + intrinsic value.
                # Uses strike, expiry, DTE stored on the position at entry.
                strategy   = direction  # "CSP" or "CALL"
                live_stock = live_prices.get(ticker) or self.market.get_live_price(ticker)
                strike     = pos.get("strike", 0)

                if not live_stock or not strike:
                    # No price data — confidence-weighted fallback
                    conf = pos.get("ai_confidence", 0.60)
                    won  = random.random() < conf
                    pnl  = round(cost * 0.5 if won else -cost * 0.5, 4)
                else:
                    # Theta ratio: how much of the option's life has elapsed
                    try:
                        opened_dt    = datetime.fromisoformat(pos.get("opened_at","")).astimezone(ET)
                        elapsed_days = max(1, (datetime.now(ET) - opened_dt).days)
                        original_dte = max(pos.get("dte", 14), 1)
                        theta_ratio  = min(elapsed_days / original_dte, 1.0)
                    except:
                        theta_ratio = 0.5

                    # IV scaling — higher IV means more extrinsic value in play
                    iv_scale = min(max(pos.get("iv", 30) / 30.0, 0.5), 2.0)

                    if strategy == "CSP":
                        # Short put: we SOLD the put, collecting premium.
                        # Win: stock stays above strike → keep theta-decayed premium.
                        # Loss: stock falls below strike → assignment = buy at strike.
                        if live_stock >= strike:
                            # OTM: keep portion of premium proportional to theta elapsed
                            # (theta decay is faster near expiry, simplified as linear here)
                            pnl = round(cost * theta_ratio * 0.88, 4)
                        else:
                            # ITM: net loss = (strike - stock) offset by premium collected
                            itm_pct    = (strike - live_stock) / strike
                            raw_loss   = itm_pct * iv_scale         # leverage to ITM depth
                            premium_offset = pos.get("premium_pct", 1.5) / 100  # premium as % of strike
                            net_loss   = max(0.0, raw_loss - premium_offset)     # premium softens blow
                            pnl        = round(-cost * min(net_loss * 3.0, 1.2), 4)

                    elif strategy == "CALL":
                        # Long call: we BOUGHT the call, paying premium.
                        # Win: stock rises above strike → intrinsic value + remaining time value.
                        # Loss: stock stays below → theta erodes premium to zero.
                        if live_stock > strike:
                            # ITM: intrinsic value × delta (approx) + remaining time value
                            intrinsic   = (live_stock - strike) / strike
                            # Delta ≈ 0.5 ATM, higher deeper ITM (capped at 0.85)
                            moneyness   = (live_stock - strike) / (strike * max(pos.get("iv", 30) / 100, 0.1))
                            delta       = min(0.5 + moneyness * 0.3, 0.85)
                            time_value  = max(0, (1 - theta_ratio) * 0.4)  # erodes linearly
                            pnl         = round(cost * (intrinsic * delta + time_value), 4)
                        else:
                            # OTM: theta eats the premium — total loss at expiry
                            pnl = round(-cost * theta_ratio * 0.90, 4)
                    else:
                        pnl = 0.0
            else:
                # Stock: use real current price
                live = live_prices.get(ticker) or self.market.get_live_price(ticker)
                if live and entry:
                    move = (live - entry) / entry
                    pnl  = round(cost * (move if direction == "LONG" else -move), 4)
                else:
                    # No price available — skip, try next scan
                    log.debug(f"  [SETTLE] No price for {ticker} — skipping")
                    continue

            settled = self.wallet.close(pos_id, pnl)
            if settled:
                State.save(self.wallet)
                sign = "+" if pnl >= 0 else ""
                log.info("  [SETTLED] %s %s | P&L: %s$%.2f | Cash: $%.2f",
                         ticker, direction, sign, pnl, self.wallet.cash)
                self._log_csv(pos, pnl=pnl)

    def _write_dashboard(self, mood, scan_results, pending_bets, live_prices):
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
                ticker   = pos.get("ticker","").replace("_OPT","")
                live     = live_prices.get(ticker)
                if live:
                    enriched["live_price"] = round(live, 2)
                    entry = pos.get("entry_price", 0)
                    if entry and pos.get("type","stock") != "option":
                        diff     = live - entry
                        diff_pct = diff / entry * 100 if entry else 0
                        enriched["live_diff"]     = round(diff, 2)
                        enriched["live_diff_pct"] = round(diff_pct, 2)
                open_list.append(enriched)

            with open(CONFIG["dashboard_file"], "w") as f:
                json.dump({
                    "lastScan":       datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "mode":           "PAPER" if CONFIG["dry_run"] else "LIVE",
                    "mood":           mood,
                    "quote":          random.choice(QUOTES.get(mood, QUOTES["watching"])),
                    "art":            random.choice(ART.get(mood, ART["watching"])),
                    "wallet":         round(self.wallet.cash, 2),
                    "portfolioValue": portfolio_val,
                    "openValue":      round(portfolio_val - self.wallet.cash, 2),
                    "startingBudget": CONFIG["paper_budget"],
                    "totalPnl":       round(self.wallet.total_pnl, 4),
                    "dailyPnl":       round(self.wallet.daily_pnl, 4),
                    "winRate":        self.wallet.win_rate(),
                    "roi":            self.wallet.roi(),
                    "tradesTotal":    len(self.wallet.trade_history),
                    "openCount":      len(self.wallet.open_positions),
                    "scanCount":      self.scan_count,
                    "rulesEvals":     self.rules_evals,
                    "engineMode":     "rules",
                    "regime":         getattr(self, "current_regime", "neutral"),
                    "regimeSpy5":     round(getattr(self, "regime_spy5", 0), 2),
                    "regimeQqq5":     round(getattr(self, "regime_qqq5", 0), 2),
                    "aiCostEstimate": 0,
                    "pendingCount":   len(pending_bets),
                    "nextScanAt":     self.next_scan_at,
                    "scanResults":    scan_results[:20],
                    "openPositions":  open_list,
                    "recentTrades":   self.wallet.trade_history[-20:],
                    "pendingTrades":  pending_bets,
                    "walletHistory":  self.wallet.wallet_history[-500:],
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Dashboard write failed: {e}")

    def run_once(self, premarket=False):
        self.scan_count += 1
        self.wallet.reset_daily_if_needed()

        log.info("=" * 60)
        log.info("  JACOB v4 #%d | %s%s | %s",
                 self.scan_count,
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 " [PRE-MARKET]" if premarket else "",
                 datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"))
        log.info("  %s", self.wallet.status())
        log.info("=" * 60)

        # ── Step 1: Get live prices for open positions ──
        live_prices = self._get_live_prices()

        # ── Step 2: Settle old positions (uses real prices) ──
        self._settle_positions(live_prices)

        # ── Step 3: Stop loss check ──
        live_prices = self._get_live_prices()  # refresh after settlements
        self._check_stop_losses(live_prices)

        # ── Step 3b: Market regime ──
        regime, spy5, qqq5 = self.regime_engine.get()
        self.current_regime = regime
        self.regime_spy5    = spy5
        self.regime_qqq5    = qqq5
        high_vol = self.regime_engine.high_vol_day

        # ── Step 4: Process approved pending ──
        for trade in self.pending.get_approved():
            log.info(f"  [HUMAN APPROVED] {trade['ticker']} {trade.get('direction','?')}")
            self._place_trade(trade, source="HUMAN", premarket=premarket)

        # ── Step 5: Expire stale pending ──
        current_pending = self.pending.expire_old()
        pending_tickers = {p.get("ticker") for p in current_pending}

        # ── Step 6: Scan watchlist ──
        scan_results  = []
        trades_placed = 0
        pending_added = 0

        for ticker in CONFIG["watchlist"]:
            log.info(f"  Scanning {ticker}...")
            quote  = self.market.get_quote(ticker)
            signal = self.scorer.score(quote)
            live_prices[ticker] = quote["price"]  # collect prices as we scan

            result = {
                "ticker":       ticker,
                "price":        quote["price"],
                "change_pct":   quote["change_pct"],
                "rsi":          quote["rsi"],
                "momentum_5d":  quote["momentum_5d"],
                "volume_ratio": quote["volume_ratio"],
                "direction":    signal["direction"],
                "score":        signal["score"],
                "signals":      signal["signals"],
                "ai_confidence":0.0,
                "ai_reasoning": "",
                "action":       "skip",
            }

            if signal["score"] < CONFIG["score_threshold"]:
                result["skip_reason"] = f"Score {signal['score']} < {CONFIG['score_threshold']}"
                scan_results.append(result)
                continue

            # v3/v4: Regime gate
            sig_dir = signal["direction"]
            if regime == "bear" and sig_dir == "LONG":
                result["action"]      = "skip"
                result["skip_reason"] = f"BEAR market — longs blocked (SPY 5d={spy5:+.1f}%)"
                scan_results.append(result)
                continue
            if regime == "bull" and sig_dir == "SHORT":
                result["action"]      = "skip"
                result["skip_reason"] = f"BULL market — shorts blocked (SPY 5d={spy5:+.1f}%)"
                scan_results.append(result)
                continue

            ok, reason = self.wallet.can_trade(ticker, signal["direction"])
            if not ok:
                result["action"]      = "blocked"
                result["skip_reason"] = reason
                scan_results.append(result)
                continue

            # v4.1: Sector concentration guard
            _sector = self.rules.PROFILES.get(ticker, {}).get("sector", "unknown")
            _sector_count = sum(
                1 for _p in self.wallet.open_positions.values()
                if self.rules.PROFILES.get(
                    _p.get("ticker","").replace("_OPT",""), {}
                ).get("sector","") == _sector
            )
            if _sector_count >= CONFIG["max_per_sector"]:
                result["action"]      = "skip"
                result["skip_reason"] = f"Sector cap: {_sector_count}/{CONFIG["max_per_sector"]} {_sector} positions"
                scan_results.append(result)
                continue

            confirmed, confidence, reasoning = self.rules.analyse_stock(quote, signal)
            self.rules_evals += 1

            # v4: Regime confidence adjustment — regime tail/headwinds
            if regime == "bear" and sig_dir == "SHORT":
                confidence = min(1.0, confidence + 0.07)
                reasoning += " | Bear regime: short tailwind"
            elif regime == "bull" and sig_dir == "LONG":
                confidence = min(1.0, confidence + 0.05)
                reasoning += " | Bull regime: long tailwind"
            elif regime == "neutral" and sig_dir == "SHORT":
                # Shorting into neutral tape: slight penalty — needs strong signal
                confidence = max(0.0, confidence - 0.04)

            result["ai_confidence"] = round(confidence, 2)
            result["ai_reasoning"]  = reasoning

            # v3: adjust size by regime and volatility
            size = self.wallet.position_size()
            if regime == "bull":
                size = min(size * 1.2, CONFIG["max_position_size_usd"])
            elif regime == "bear":
                size = size * 0.8
            if high_vol:
                size = size * 0.7   # reduce on high-vol days
            size = round(max(CONFIG["min_position_size_usd"], size), 2)
            trade = {
                "ticker":        ticker,
                "type":          "stock",
                "direction":     signal["direction"],
                "price":         quote["price"],
                "entry_price":   quote["price"],
                "cost":          size,
                "ai_confidence": round(confidence, 2),
                "ai_reasoning":  reasoning,
                "score":         signal["score"],
                "signals":       signal["signals"],
            }

            if confirmed and confidence >= CONFIG["auto_trade_confidence"]:
                placed = self._place_trade(trade, source="AUTO", premarket=premarket)
                result["action"] = "trade" if placed else "blocked"
                if placed:
                    trades_placed += 1
            elif confidence >= CONFIG["pending_confidence"] and ticker not in pending_tickers:
                trade["pos_id"] = self._pos_id(ticker, "stock")
                self.pending.add(trade)
                pending_tickers.add(ticker)
                result["action"] = "pending"
                pending_added += 1
            else:
                result["action"]      = "skip"
                result["skip_reason"] = f"Low confidence ({confidence:.0%})"

            # Options scan
            if (CONFIG["options_enabled"] and
                    signal["score"] >= CONFIG["options_score_min"] and
                    confidence >= CONFIG["options_conf_min"]):
                self._scan_options(ticker, quote, signal, confidence,
                                   scan_results, pending_tickers, premarket)

            scan_results.append(result)
            time.sleep(0.4)

        # ── Step 7: Save state & dashboard ──
        State.save(self.wallet)
        current_pending = self.pending.load().get("pending", [])

        if trades_placed > 0:  mood = "hunting"
        elif pending_added > 0: mood = "pending"
        elif any(r["score"] >= 55 for r in scan_results): mood = "watching"
        else: mood = "sleeping"

        log.info("\nScan done | tickers=%d | trades=%d | pending=%d | %s\n",
                 len(scan_results), trades_placed, pending_added, self.wallet.status())

        self._write_dashboard(mood, scan_results, current_pending, live_prices)

    def _scan_options(self, ticker, quote, signal, stock_conf,
                      scan_results, pending_tickers, premarket):
        opts = self.market.get_options_chain(ticker)
        if not opts:
            return
        best = opts[0]
        if best["premium_pct"] < CONFIG["csp_min_premium_pct"]:
            return

        confirmed, confidence, reasoning = self.rules.analyse_option(best, quote, signal)
        self.rules_evals += 1

        size    = max(CONFIG["min_position_size_usd"],
                      min(self.wallet.position_size() * 0.5, 25.0))
        opt_key = f"{ticker}_OPT"
        trade   = {
            "ticker":        opt_key,
            "type":          "option",
            "direction":     best["strategy"],
            "price":         quote["price"],
            "entry_price":   best["mid"],
            "strike":        best["strike"],
            "expiry":        best["expiry"],
            "dte":           best["dte"],
            "iv":            best["iv"],
            "premium_pct":   best["premium_pct"],
            "cost":          round(size, 2),
            "ai_confidence": round(confidence, 2),
            "ai_reasoning":  reasoning,
        }

        opt_result = {
            "ticker":       f"{ticker} OPT",
            "price":        quote["price"],
            "change_pct":   quote["change_pct"],
            "rsi":          quote["rsi"],
            "direction":    best["strategy"],
            "score":        signal["score"],
            "signals":      [f"{best['strategy']}: ${best['strike']} {best['expiry']} | "
                             f"{best['premium_pct']:.2f}% prem | IV {best['iv']:.0f}%"],
            "ai_confidence":round(confidence, 2),
            "ai_reasoning": reasoning,
            "action":       "skip",
        }

        ok, reason = self.wallet.can_trade(opt_key, best["strategy"])
        if not ok:
            opt_result["action"] = "blocked"
            scan_results.append(opt_result)
            return

        if confirmed and confidence >= CONFIG["auto_trade_confidence"]:
            placed = self._place_trade(trade, source="AUTO", premarket=premarket)
            opt_result["action"] = "trade" if placed else "blocked"
        elif confidence >= CONFIG["pending_confidence"] and opt_key not in pending_tickers:
            trade["pos_id"] = self._pos_id(opt_key, "option")
            self.pending.add(trade)
            pending_tickers.add(opt_key)
            opt_result["action"] = "pending"

        scan_results.append(opt_result)

    # ── Market hours helpers ──────────────────────────────────────────────────

    def _market_open(self):
        now = datetime.now(ET)
        if now.weekday() >= 5: return False
        return now.replace(hour=9,minute=30,second=0,microsecond=0) <= now < \
               now.replace(hour=16,minute=0, second=0,microsecond=0)

    def _in_premarket(self):
        now = datetime.now(ET)
        if now.weekday() >= 5: return False
        return now.replace(hour=9,minute=0,second=0,microsecond=0) <= now < \
               now.replace(hour=9,minute=30,second=0,microsecond=0)

    def _secs_until_premarket(self):
        now = datetime.now(ET)
        candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= candidate:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return max(0, int((candidate - now).total_seconds()))

    def run_loop(self):
        log.info("Jacob v4 starting | %s | $%.0f budget | %d tickers | rules engine",
                 "PAPER" if CONFIG["dry_run"] else "LIVE",
                 CONFIG["paper_budget"], len(CONFIG["watchlist"]))
        log.info("Scan interval: %ds | Stop loss: %.0f%% | Daily loss cap: $%.0f",
                 CONFIG["scan_interval_sec"], CONFIG["stop_loss_pct"]*100, DAILY_LOSS_CAP)

        trigger_path = os.path.join(os.path.dirname(CONFIG["state_file"]), "jacob_trigger.json")
        interval     = CONFIG["scan_interval_sec"]

        while True:
            try:
                now = datetime.now(ET)
                if self._market_open():
                    self.run_once(premarket=False)
                    self.next_scan_at = (datetime.now(ET) + timedelta(seconds=interval)).isoformat()
                    log.info("Next scan in %ds...\n", interval)
                    elapsed = 0
                    while elapsed < interval:
                        time.sleep(10); elapsed += 10
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
                    log.info("Market closed — sleeping until %s (%dm)\n", wake, secs//60)
                    self.next_scan_at = (datetime.now(ET) + timedelta(seconds=secs)).isoformat()
                    elapsed = 0
                    while elapsed < secs:
                        time.sleep(60); elapsed += 60
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
