"""
╔══════════════════════════════════════════════════════════════╗
║              JACOB'S MONEY FARM — ALERT SYSTEM              ║
║  Morning report: 8am EST daily                              ║
║  Urgent alerts: loss cap · injury on open bet · pending     ║
║  Powered by Twilio SMS                                       ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, json, time, logging, schedule
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo('America/New_York')
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════
# CONFIG — fill in after Twilio setup
# ══════════════════════════════════════════════════════════════
TWILIO_SID        = os.getenv("TWILIO_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM       = os.getenv("TWILIO_FROM", "")   # your Twilio number e.g. +12015551234
ALERT_TO          = os.getenv("ALERT_TO", "")      # your personal number e.g. +17185559876

WORK_DIR = "/home/ubuntu"

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(WORK_DIR, "farm_alerts.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("alerts")

# ══════════════════════════════════════════════════════════════
# SMS SENDER
# ══════════════════════════════════════════════════════════════
def send_sms(message):
    """Send an SMS via Twilio. Logs if credentials missing."""
    if not all([TWILIO_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, ALERT_TO]):
        log.warning(f"[SMS] Credentials not set — would have sent:\n{message}")
        return False
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            body=message,
            from_=TWILIO_FROM,
            to=ALERT_TO
        )
        log.info(f"[SMS] Sent: {msg.sid} | {message[:60]}...")
        return True
    except Exception as e:
        log.error(f"[SMS] Failed: {e}")
        return False

# ══════════════════════════════════════════════════════════════
# DATA READERS
# ══════════════════════════════════════════════════════════════
def read_json(filename, default=None):
    try:
        with open(os.path.join(WORK_DIR, filename)) as f:
            return json.load(f)
    except:
        return default or {}

def read_jacob():
    return read_json("jacob_data.json")

def read_seraphina():
    return read_json("seraphina_data.json")

def read_loachy():
    return read_json("loachy_data.json")

def read_loachy_state():
    return read_json("loachy_state.json")

def read_pending():
    return read_json("loachy_pending.json", {"pending": []})

# ══════════════════════════════════════════════════════════════
# MORNING REPORT — 8am EST
# ══════════════════════════════════════════════════════════════
def morning_report():
    log.info("[REPORT] Building morning report...")

    jacob = read_jacob()
    sera  = read_seraphina()
    loach = read_loachy()
    state = read_loachy_state()

    # Jacob stats
    j_wallet  = jacob.get("wallet", 500) if jacob else 500
    j_daily   = jacob.get("dailyPnl", 0) if jacob else 0
    j_open    = jacob.get("openCount", 0) if jacob else 0
    j_wr      = jacob.get("winRate", 0) if jacob else 0

    # Seraphina stats
    s_wallet = sera.get("wallet", 1000) if sera else 1000
    s_daily  = sera.get("dailyPnl", 0) if sera else 0
    s_pnl    = sera.get("totalPnl", 0) if sera else 0
    s_open   = sera.get("openPositions", 0) if sera else 0

    # Loachy stats
    l_wallet  = loach.get("wallet", 50) if loach else 50
    l_daily   = loach.get("dailyPnl", 0) if loach else 0
    l_wr      = loach.get("winRate", 0) if loach else 0
    l_pending = loach.get("pendingCount", 0) if loach else 0

    # Yesterday bets
    history   = state.get("bet_history", []) if state else []
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    yest_bets = [b for b in history if b.get("settled_at","")[:10] == yesterday]
    yest_won  = sum(1 for b in yest_bets if b.get("won"))
    yest_pnl  = sum(b.get("pnl",0) for b in yest_bets)

    pending_line = f" !! {l_pending} pending" if l_pending > 0 else ""
    yest_line    = f" | yday {yest_won}/{len(yest_bets)} ${yest_pnl:+.2f}" if yest_bets else ""

    message = (
        f"🌾 Good morning!\n"
        f"🦎 Jacob: ${j_wallet:.2f} | {j_daily:+.2f} today | {j_open} open | {j_wr:.0f}% WR\n"
        f"🐱 Sera: ${s_wallet:.2f} | {s_daily:+.4f} today | {s_open} open\n"
        f"🐟 Loach: ${l_wallet:.2f} | {l_daily:+.2f} today | {l_wr:.0f}% WR{yest_line}{pending_line}"
    )
    send_sms(message)
    log.info("[REPORT] Morning report sent")

# ══════════════════════════════════════════════════════════════
# URGENT: DAILY LOSS CAP
# ══════════════════════════════════════════════════════════════
_loss_cap_alerted_today = None

def check_loss_cap():
    global _loss_cap_alerted_today
    today = datetime.now(ET).date()

    # Reset alert flag on new day
    if _loss_cap_alerted_today != today:
        _loss_cap_alerted_today = None

    loach = read_loachy()
    if not loach:
        return

    daily_pnl  = loach.get("dailyPnl", 0)
    loss_cap   = -20.0  # matches CONFIG in loachy_bot.py

    if daily_pnl <= loss_cap and _loss_cap_alerted_today != today:
        _loss_cap_alerted_today = today
        msg = (
            f"📉 LOSS CAP HIT\n"
            f"  ><(((º>  zzz\n"
            f"  ~ ~ ~ ~\n"
            f"daily: ${daily_pnl:.2f} / -${abs(loss_cap):.0f} cap\n"
            f"loachy is done for today."
        )
        send_sms(msg)
        log.info("[ALERT] Daily loss cap SMS sent")

# ══════════════════════════════════════════════════════════════
# URGENT: INJURY ON OPEN BET
# ══════════════════════════════════════════════════════════════
_injury_alerts_sent = set()

def check_injuries_on_open_bets():
    loach = read_loachy()
    state = read_loachy_state()
    if not loach or not state:
        return

    open_bets = state.get("open_bets", {})
    if not open_bets:
        return

    candidates = loach.get("candidates", [])

    for bet_id, bet in open_bets.items():
        # Find matching candidate with injury context
        for cand in candidates:
            if cand.get("game_id") == bet_id and cand.get("star_out"):
                alert_key = f"injury_{bet_id}"
                if alert_key not in _injury_alerts_sent:
                    _injury_alerts_sent.add(alert_key)
                    matchup  = f"{bet.get('away','?')} @ {bet.get('home','?')}"
                    injury   = cand.get("injury_context", "Key player out")
                    outcome  = bet.get("outcome_name", "?")
                    odds     = bet.get("best_price", 0)
                    size     = bet.get("bet_size", 0)
                    msg = (
                        f"🚑 INJURY ALERT\n"
                        f"  ><(((!! \n"
                        f"  ~ ~ ~ ~\n"
                        f"{matchup}\n"
                        f"bet: {outcome} {odds:+d} (${size:.2f})\n"
                        f"{str(injury)[:100]}"
                    )
                    send_sms(msg)
                    log.info(f"[ALERT] Injury alert sent for {matchup}")

# ══════════════════════════════════════════════════════════════
# URGENT: PENDING BETS NEEDING APPROVAL
# ══════════════════════════════════════════════════════════════
_pending_alerts_sent = set()

def check_pending_bets():
    pending_data = read_pending()
    pending = pending_data.get("pending", [])
    if not pending:
        return

    now = datetime.now(ET)
    for bet in pending:
        bet_id = bet.get("game_id", "")
        alert_key = f"pending_{bet_id}"
        if alert_key in _pending_alerts_sent:
            continue

        # Only alert if expiring within 25 minutes (so they have time to act)
        try:
            expires = datetime.fromisoformat(bet.get("expires_at", ""))
            mins_left = (expires - now).total_seconds() / 60
            if mins_left > 25 or mins_left < 0:
                continue
        except:
            continue

        _pending_alerts_sent.add(alert_key)

        matchup  = f"{bet.get('away','?')} @ {bet.get('home','?')}"
        outcome  = bet.get("outcome_name", "?")
        odds     = bet.get("best_price", 0)
        conf     = bet.get("ai_confidence", 0)
        reason   = bet.get("ai_reasoning", "")
        is_long  = bet.get("longshot", False)
        tier     = "🎰 LONGSHOT" if is_long else "🐟 BORDERLINE"
        mins_int = int(mins_left)

        msg = (
            f"{tier}\n"
            f"  ><(((º? \n"
            f"  ? ? ? ?\n"
            f"{matchup}\n"
            f"{outcome} {odds:+d} | {conf*100:.0f}% conf\n"
            f'"{reason[:80]}"\n'
            f"expires ~{mins_int}min — check dashboard"
        )
        send_sms(msg)
        log.info(f"[ALERT] Pending bet alert sent for {matchup}")

# ══════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════
def run_urgent_checks():
    """Urgent checks disabled until going live — see roadmap."""
    pass

if __name__ == "__main__":
    log.info("🌾 Farm Alert System starting...")
    log.info(f"   SMS to: {ALERT_TO or '(not set)'}")
    log.info(f"   From:   {TWILIO_FROM or '(not set)'}")

    # Morning report at 8am EST (13:00 UTC)
    schedule.every().day.at("13:00").do(morning_report)

    log.info("   Scheduled: morning report 8am EST only")
    log.info("   Running...")

    while True:
        schedule.run_pending()
        time.sleep(30)
