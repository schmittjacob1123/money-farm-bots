import os, json, time, logging, schedule, subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
from dotenv import load_dotenv
load_dotenv()

TWILIO_SID        = os.getenv("TWILIO_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM       = os.getenv("TWILIO_FROM", "")
ALERT_TO          = os.getenv("ALERT_TO", "")
WORK_DIR = "/home/ubuntu"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(WORK_DIR, "farm_alerts.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("alerts")


def send_sms(message):
    if not all([TWILIO_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, ALERT_TO]):
        log.warning(f"[SMS] Credentials not set — would have sent:\n{message}")
        return False
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_FROM, to=ALERT_TO)
        log.info(f"[SMS] Sent: {msg.sid} | {message[:60]}...")
        return True
    except Exception as e:
        log.error(f"[SMS] Failed: {e}")
        return False


def read_json(filename, default=None):
    try:
        with open(os.path.join(WORK_DIR, filename)) as f:
            return json.load(f)
    except:
        return default or {}


def read_jacob():        return read_json("jacob_data.json")
def read_seraphina():    return read_json("seraphina_data.json")
def read_loachy():       return read_json("loachy_data.json")
def read_loachy_state(): return read_json("loachy_state.json")
def read_pending():      return read_json("loachy_pending.json", {"pending": []})


# ══════════════════════════════════════════════════════════════
# MORNING REPORT — 8am EST
# ══════════════════════════════════════════════════════════════
def morning_report():
    log.info("[REPORT] Building morning report...")

    jacob = read_jacob()
    sera  = read_seraphina()
    loach = read_loachy()
    state = read_loachy_state()

    j_port   = jacob.get("portfolioValue", 500) if jacob else 500
    j_daily  = jacob.get("dailyPnl", 0) if jacob else 0
    j_open   = jacob.get("openCount", 0) if jacob else 0
    j_wr     = jacob.get("winRate", 0) if jacob else 0

    s_port   = sera.get("portfolioValue", 1000) if sera else 1000
    s_daily  = sera.get("dailyPnl", 0) if sera else 0
    s_open   = sum(g.get("openBuys", 0) for g in sera.get("grids", [])) if sera else 0
    s_wr     = sera.get("winRate", 0) if sera else 0
    s_streak = sera.get("winStreak", 0) if sera else 0

    l_wallet  = loach.get("wallet", 50) if loach else 50
    l_daily   = loach.get("dailyPnl", 0) if loach else 0
    l_wr      = loach.get("winRate", 0) if loach else 0
    l_pending = loach.get("pendingCount", 0) if loach else 0

    history   = state.get("bet_history", []) if state else []
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    yest_bets = [b for b in history if b.get("settled_at", "")[:10] == yesterday]
    yest_won  = sum(1 for b in yest_bets if b.get("won"))
    yest_pnl  = sum(b.get("pnl", 0) for b in yest_bets)

    pending_line = f" !! {l_pending} pending" if l_pending > 0 else ""
    yest_line    = f" | yday {yest_won}/{len(yest_bets)} ${yest_pnl:+.2f}" if yest_bets else ""

    message = (
        f"Good morning Jacob!\n"
        f"Sera: ${s_port:.2f} | {s_daily:+.2f} today | {s_open} open | {s_wr:.0f}% WR | {s_streak}W streak\n"
        f"Jacob: ${j_port:.2f} | {j_daily:+.2f} today | {j_open} open | {j_wr:.0f}% WR\n"
        f"Loach: ${l_wallet:.2f} | {l_daily:+.2f} today | {l_wr:.0f}% WR{yest_line}{pending_line}"
    )
    send_sms(message)
    log.info("[REPORT] Morning report sent")


# ══════════════════════════════════════════════════════════════
# URGENT: SERAPHINA CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════
_circuit_breaker_alerted = False

def check_circuit_breaker():
    global _circuit_breaker_alerted
    sera = read_seraphina()
    if not sera:
        return
    active = sera.get("circuitBreaker", False)
    if active and not _circuit_breaker_alerted:
        _circuit_breaker_alerted = True
        drawdown  = sera.get("drawdownPct", 0)
        portfolio = sera.get("portfolioValue", 0)
        msg = (
            f"CIRCUIT BREAKER TRIPPED\n"
            f"Seraphina halted all buys.\n"
            f"Drawdown: {drawdown:.1f}% | Portfolio: ${portfolio:.2f}\n"
            f"Resumes automatically at 8% recovery.\n"
            f"Reply PAUSE SERAPHINA to halt entirely."
        )
        send_sms(msg)
        log.info("[ALERT] Circuit breaker SMS sent")
    elif not active:
        _circuit_breaker_alerted = False


# ══════════════════════════════════════════════════════════════
# URGENT: BOT HEALTH (crash detection)
# ══════════════════════════════════════════════════════════════
_bot_down_alerted = {}
_MONITORED_BOTS = {"seraphina": "seraphina", "jacob": "jacob"}

def is_screen_running(name):
    try:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        return f".{name}" in result.stdout
    except:
        return True  # assume running if check fails

def check_bot_health():
    for bot_name, screen_name in _MONITORED_BOTS.items():
        running     = is_screen_running(screen_name)
        was_alerted = _bot_down_alerted.get(bot_name, False)
        if not running and not was_alerted:
            _bot_down_alerted[bot_name] = True
            msg = (
                f"BOT DOWN: {bot_name.upper()}\n"
                f"Screen '{screen_name}' not found.\n"
                f"Reply RESUME {bot_name.upper()} to restart it."
            )
            send_sms(msg)
            log.info(f"[ALERT] Bot down SMS sent for {bot_name}")
        elif running and was_alerted:
            _bot_down_alerted[bot_name] = False
            log.info(f"[ALERT] {bot_name} is back up - alert cleared")


# ══════════════════════════════════════════════════════════════
# URGENT: JACOB DAILY LOSS WARNING (fires at -$15, before $20 hard stop)
# ══════════════════════════════════════════════════════════════
_jacob_loss_alerted_today = None

def check_jacob_loss():
    global _jacob_loss_alerted_today
    today = datetime.now(ET).date()
    if _jacob_loss_alerted_today == today:
        return
    jacob = read_jacob()
    if not jacob:
        return
    daily_pnl = jacob.get("dailyPnl", 0)
    if daily_pnl <= -15.0:
        _jacob_loss_alerted_today = today
        port       = jacob.get("portfolioValue", 500)
        open_count = jacob.get("openCount", 0)
        msg = (
            f"JACOB DAILY LOSS WARNING\n"
            f"Daily P&L: ${daily_pnl:.2f} (hard stop at -$20)\n"
            f"Portfolio: ${port:.2f} | {open_count} positions open.\n"
            f"Reply PAUSE JACOB to halt."
        )
        send_sms(msg)
        log.info("[ALERT] Jacob daily loss SMS sent")


# ══════════════════════════════════════════════════════════════
# URGENT: LOACHY DAILY LOSS CAP
# ══════════════════════════════════════════════════════════════
_loss_cap_alerted_today = None

def check_loss_cap():
    global _loss_cap_alerted_today
    today = datetime.now(ET).date()
    if _loss_cap_alerted_today == today:
        return
    loach = read_loachy()
    if not loach:
        return
    daily_pnl = loach.get("dailyPnl", 0)
    if daily_pnl <= -20.0:
        _loss_cap_alerted_today = today
        msg = (
            f"LOACHY LOSS CAP HIT\n"
            f"Daily: ${daily_pnl:.2f} / -$20 cap.\n"
            f"Loachy is done for today."
        )
        send_sms(msg)
        log.info("[ALERT] Loachy loss cap SMS sent")


# ══════════════════════════════════════════════════════════════
# URGENT: LOACHY INJURY ON OPEN BET
# ══════════════════════════════════════════════════════════════
_injury_alerts_sent = set()

def check_injuries_on_open_bets():
    loach = read_loachy()
    state = read_loachy_state()
    if not loach or not state:
        return
    open_bets  = state.get("open_bets", {})
    candidates = loach.get("candidates", [])
    for bet_id, bet in open_bets.items():
        for cand in candidates:
            if cand.get("game_id") == bet_id and cand.get("star_out"):
                alert_key = f"injury_{bet_id}"
                if alert_key not in _injury_alerts_sent:
                    _injury_alerts_sent.add(alert_key)
                    matchup = f"{bet.get('away','?')} @ {bet.get('home','?')}"
                    injury  = cand.get("injury_context", "Key player out")
                    outcome = bet.get("outcome_name", "?")
                    odds    = bet.get("best_price", 0)
                    size    = bet.get("bet_size", 0)
                    msg = (
                        f"INJURY ALERT\n"
                        f"{matchup}\n"
                        f"Bet: {outcome} {odds:+d} (${size:.2f})\n"
                        f"{str(injury)[:120]}"
                    )
                    send_sms(msg)
                    log.info(f"[ALERT] Injury alert sent for {matchup}")


# ══════════════════════════════════════════════════════════════
# URGENT: LOACHY PENDING BET (needs approval within 25 min)
# ══════════════════════════════════════════════════════════════
_pending_alerts_sent = set()

def check_pending_bets():
    pending_data = read_pending()
    pending = pending_data.get("pending", [])
    if not pending:
        return
    now = datetime.now(ET)
    for bet in pending:
        bet_id    = bet.get("game_id", "")
        alert_key = f"pending_{bet_id}"
        if alert_key in _pending_alerts_sent:
            continue
        try:
            expires   = datetime.fromisoformat(bet.get("expires_at", ""))
            mins_left = (expires - now).total_seconds() / 60
            if mins_left > 25 or mins_left < 0:
                continue
        except:
            continue

        _pending_alerts_sent.add(alert_key)

        # Save last bet ID so two-way SMS reply knows which bet to act on
        try:
            with open(os.path.join(WORK_DIR, "last_alerted_bet.json"), "w") as f:
                json.dump({"game_id": bet_id}, f)
        except:
            pass

        matchup  = f"{bet.get('away','?')} @ {bet.get('home','?')}"
        outcome  = bet.get("outcome_name", "?")
        odds     = bet.get("best_price", 0)
        conf     = bet.get("ai_confidence", 0)
        reason   = bet.get("ai_reasoning", "")
        is_long  = bet.get("longshot", False)
        tier     = "LONGSHOT" if is_long else "BORDERLINE"
        mins_int = int(mins_left)

        msg = (
            f"LOACHY: {tier}\n"
            f"{matchup}\n"
            f"{outcome} {odds:+d} | {conf*100:.0f}% conf\n"
            f'"{reason[:80]}"\n'
            f"~{mins_int}min left -- Reply APPROVE or REJECT"
        )
        send_sms(msg)
        log.info(f"[ALERT] Pending bet alert sent for {matchup}")


# ══════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════
def run_urgent_checks():
    check_circuit_breaker()
    check_bot_health()
    check_jacob_loss()
    check_loss_cap()
    check_injuries_on_open_bets()
    check_pending_bets()


if __name__ == "__main__":
    log.info("Farm Alert System starting...")
    log.info(f"   SMS to: {ALERT_TO or '(not set)'}")
    log.info(f"   From:   {TWILIO_FROM or '(not set)'}")

    schedule.every().day.at("13:00").do(morning_report)
    schedule.every(60).seconds.do(run_urgent_checks)

    log.info("   Scheduled: morning report 8am EST | urgent checks every 60s")
    log.info("   Running...")

    while True:
        schedule.run_pending()
        time.sleep(30)
