"""
Jacob's Money Farm — Control API
Runs on port 5000. Lets the homepage start/stop/reset bots.

SETUP:  pip install flask flask-cors
RUN:    screen -S farmapi -> python3 farm_api.py -> Ctrl+A D
"""

import subprocess, os, json, time, secrets, hashlib, base64
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app, supports_credentials=True)

# ── SESSION AUTH ──
FARM_PASSWORD = os.environ.get("FARM_PASSWORD", "kickrocks!")
SESSION_SECRET = "d4f2861dc96539bb657aef8e80e37e590eb214de23308c69a85c5deefefe0ea6"
SESSIONS = set()  # in-memory valid tokens

# ── API TOKEN AUTH (for Claude / programmatic access) ──
# Set FARM_API_TOKEN in .env to a long random string.
# Pass as ?token=YOUR_TOKEN on /debug and /apply-update.
FARM_API_TOKEN = os.environ.get("FARM_API_TOKEN", "")

def valid_api_token(req):
    """Check ?token= query param against FARM_API_TOKEN env var."""
    if not FARM_API_TOKEN:
        return False  # token not configured — deny all
    return req.args.get("token", "") == FARM_API_TOKEN

def authorized(req):
    """Accept either a valid browser session OR a valid API token."""
    return valid_session(req) or valid_api_token(req)

def make_token():
    return secrets.token_hex(32)

def valid_session(req):
    token = req.cookies.get("farm_session")
    return token and token in SESSIONS

WORK_DIR = "/home/ubuntu"

BOTS = {
    "jacob":     {"script": "jacob_bot.py",  "screen": "jacob", "state": ["jacob_state.json", "jacob_trades.csv", "jacob_data.json"]},
    "seraphina": {"script": "seraphina_bot.py",   "screen": "seraphina",  "state": ["seraphina_state.json", "seraphina_trades.csv", "seraphina_data.json"]},
    "loachy":    {"script": "loachy_bot.py",      "screen": "loachy",     "state": ["loachy_state.json", "loachy_trades.csv", "loachy_data.json", "loachy_pending.json"]},
}

FRESH_STATE = {
    "positions.json":       {"positions": [], "total_pnl": 0, "daily_pnl": 0},
    "pnl_history.json":     {"history": []},
    "seraphina_state.json": {"wallet": 50.0, "total_pnl": 0, "daily_pnl": 0, "open_positions": {}, "bet_history": [], "daily_bets": 0, "last_reset_date": ""},
    "loachy_state.json":    {"wallet": 50.0, "total_pnl": 0, "daily_pnl": 0, "open_bets": {}, "bet_history": [], "daily_bets": 0, "last_reset_date": ""},
    "loachy_pending.json":  {"pending": [], "approved": [], "rejected": []},
}

CSV_HEADERS = {
    "trades.csv":           "timestamp,market,action,price,size,edge,pnl\n",
    "seraphina_trades.csv": "timestamp,market,side,price,size,pnl,source\n",
    "loachy_trades.csv":    "timestamp,sport,game,bet,odds,size,result,pnl,source\n",
}

DELETE_ON_RESET = ["live_data.json", "seraphina_data.json", "loachy_data.json"]


def get_screen_pid(screen_name):
    try:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if f".{screen_name}" in line:
                pid = line.strip().split(".")[0].strip()
                if pid.isdigit():
                    return int(pid)
    except Exception as e:
        print(f"screen -ls error: {e}")
    return None


def is_running(screen_name):
    return get_screen_pid(screen_name) is not None


def kill_all_screens(screen_name):
    """Kill every screen session matching this name (prevents duplicate instances)."""
    try:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        killed = 0
        for line in result.stdout.splitlines():
            if f".{screen_name}" in line:
                pid = line.strip().split(".")[0].strip()
                if pid.isdigit():
                    subprocess.run(["screen", "-X", "-S", f"{pid}.{screen_name}", "quit"],
                                   capture_output=True)
                    killed += 1
        return killed
    except Exception as e:
        print(f"kill_all_screens error: {e}")
        return 0


@app.route("/status", methods=["GET"])
def status():
    result = {}
    for name, cfg in BOTS.items():
        result[name] = {"running": is_running(cfg["screen"]), "screen": cfg["screen"], "script": cfg["script"]}
    return jsonify(result)


@app.route("/bot/<botname>/start", methods=["POST"])
def start_bot(botname):
    if botname not in BOTS:
        return jsonify({"ok": False, "error": f"Unknown bot: {botname}"}), 404
    cfg = BOTS[botname]
    if is_running(cfg["screen"]):
        return jsonify({"ok": True, "message": f"{botname} already running"})
    try:
        subprocess.Popen(
            ["screen", "-dmS", cfg["screen"], "python3", os.path.join(WORK_DIR, cfg["script"])],
            cwd=WORK_DIR
        )
        return jsonify({"ok": True, "message": f"{botname} started"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/bot/<botname>/stop", methods=["POST"])
def stop_bot(botname):
    if botname not in BOTS:
        return jsonify({"ok": False, "error": f"Unknown bot: {botname}"}), 404
    cfg = BOTS[botname]
    if get_screen_pid(cfg["screen"]) is None:
        return jsonify({"ok": True, "message": f"{botname} already stopped"})
    try:
        killed = kill_all_screens(cfg["screen"])
        return jsonify({"ok": True, "message": f"{botname} stopped ({killed} instance(s) killed)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/bot/<botname>/reset", methods=["POST"])
def reset_bot(botname):
    if botname not in BOTS:
        return jsonify({"ok": False, "error": f"Unknown bot: {botname}"}), 404
    cfg = BOTS[botname]

    if is_running(cfg["screen"]):
        kill_all_screens(cfg["screen"])
        time.sleep(1)

    errors = []
    for fname in cfg["state"]:
        fpath = os.path.join(WORK_DIR, fname)
        try:
            if fname in DELETE_ON_RESET:
                if os.path.exists(fpath):
                    os.remove(fpath)
            elif fname in CSV_HEADERS:
                with open(fpath, "w") as f:
                    f.write(CSV_HEADERS[fname])
            elif fname in FRESH_STATE:
                with open(fpath, "w") as f:
                    json.dump(FRESH_STATE[fname], f, indent=2)
            else:
                with open(fpath, "w") as f:
                    json.dump({}, f)
        except Exception as e:
            errors.append(f"{fname}: {e}")

    if errors:
        return jsonify({"ok": False, "error": "Some files failed: " + ", ".join(errors)})
    return jsonify({"ok": True, "message": f"{botname} reset to fresh $50 simulation"})


@app.route("/sports-config", methods=["GET", "POST"])
def sports_config():
    fpath = os.path.join(WORK_DIR, "loachy_sports_config.json")
    if request.method == "POST":
        try:
            data = request.get_json()
            with open(fpath, "w") as f:
                json.dump(data, f, indent=2)
            return jsonify({"ok": True, "message": "Sports config saved"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        try:
            with open(fpath) as f:
                return jsonify(json.load(f))
        except:
            # Default all on
            default = {
                "basketball_ncaab": True, "basketball_nba": True,
                "icehockey_nhl": True, "baseball_mlb": True,
                "americanfootball_nfl": True, "americanfootball_ncaaf": True,
                "mma_mixed_martial_arts": True, "soccer_epl": True,
                "soccer_usa_mls": True,
            }
            return jsonify(default)



@app.route("/pending-approve", methods=["POST"])
def pending_approve():
    """Approve or reject a pending bet from the dashboard."""
    try:
        body    = request.get_json()
        game_id = body.get("game_id")
        action  = body.get("action")  # "approve" or "reject"
        if not game_id or action not in ("approve", "reject"):
            return jsonify({"ok": False, "error": "game_id and action required"}), 400

        fpath = os.path.join(WORK_DIR, "loachy_pending.json")
        try:
            with open(fpath) as f:
                data = json.load(f)
        except:
            data = {"pending": [], "approved": [], "rejected": []}

        bet = next((p for p in data["pending"] if p["game_id"] == game_id), None)
        if not bet:
            return jsonify({"ok": False, "error": "Bet not found in pending"}), 404

        data["pending"] = [p for p in data["pending"] if p["game_id"] != game_id]

        if action == "approve":
            bet["status"] = "APPROVED"
            data.setdefault("approved", []).append(bet)
            msg = f"Bet approved: {bet.get('outcome_name','')} {bet.get('best_price','')}"
        else:
            bet["status"] = "REJECTED"
            data.setdefault("rejected", []).append(bet)
            msg = f"Bet rejected: {bet.get('outcome_name','')}"

        with open(fpath, "w") as f:
            json.dump(data, f, indent=2)

        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/set-odds-key", methods=["POST"])
def set_odds_key():
    """Update ODDS_API_KEY in .env and restart Loachy. Requires browser session."""
    if not valid_session(request):
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    try:
        body    = request.get_json() or {}
        new_key = body.get("key", "").strip()
        restart = body.get("restart", True)

        if not new_key:
            return jsonify({"ok": False, "error": "key is required"}), 400

        env_path = os.path.join(WORK_DIR, ".env")
        try:
            with open(env_path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        found, new_lines = False, []
        for line in lines:
            if line.startswith("ODDS_API_KEY="):
                new_lines.append(f"ODDS_API_KEY={new_key}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"ODDS_API_KEY={new_key}\n")

        with open(env_path, "w") as f:
            f.writelines(new_lines)

        restarted = False
        if restart:
            cfg = BOTS["loachy"]
            kill_all_screens(cfg["screen"])
            time.sleep(1)
            subprocess.Popen(
                ["screen", "-dmS", cfg["screen"], "python3",
                 os.path.join(WORK_DIR, cfg["script"])],
                cwd=WORK_DIR
            )
            restarted = True
            print(f"[SET-ODDS-KEY] Key updated + Loachy restarted")

        return jsonify({"ok": True, "restarted": restarted,
                        "message": "ODDS_API_KEY updated" + (" · Loachy restarted" if restarted else "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/auth/login", methods=["POST"])
def login():
    body = request.get_json() or {}
    pw   = body.get("password", "")
    if pw == FARM_PASSWORD:
        token = make_token()
        SESSIONS.add(token)
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("farm_session", token, httponly=True, samesite="Lax", max_age=60*60*24*30)
        return resp
    return jsonify({"ok": False, "error": "wrong password"}), 401


@app.route("/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get("farm_session")
    SESSIONS.discard(token)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("farm_session")
    return resp


@app.route("/auth/check", methods=["GET"])
def auth_check():
    return jsonify({"ok": valid_session(request)})



@app.route("/debug", methods=["GET"])
def debug():
    """
    Returns a full snapshot of all bot data in one JSON blob.
    Claude can web_fetch this URL directly — no more copy-pasting logs.
    Auth: browser session cookie OR ?token=FARM_API_TOKEN
    """
    if not authorized(request):
        return jsonify({"ok": False, "error": "Not authenticated"}), 401

    def read_file(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            return None

    def read_log(path, lines=80):
        try:
            with open(path) as f:
                return f.readlines()[-lines:]
        except:
            return []

    # Screen status
    screens = {}
    try:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "Socket" in line or "There" in line:
                continue
            screens[line] = True
    except:
        pass

    bots_debug = {}
    for name, cfg in BOTS.items():
        log_file   = os.path.join(WORK_DIR, f"{cfg['screen']}.log")
        state_file = os.path.join(WORK_DIR, cfg["state"][0])
        data_file  = os.path.join(WORK_DIR, cfg["state"][2] if len(cfg["state"]) > 2 else "")

        bots_debug[name] = {
            "running":   is_running(cfg["screen"]),
            "log_tail":  read_log(log_file, lines=80),
            "state":     read_file(state_file),
            "dashboard": read_file(data_file) if data_file else None,
        }

    loachy_pending = read_file(os.path.join(WORK_DIR, "loachy_pending.json"))

    return jsonify({
        "ok":             True,
        "timestamp":      __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "screens":        list(screens.keys()),
        "bots":           bots_debug,
        "loachy_pending": loachy_pending,
    })


# ── ALLOWED FILES FOR apply-update (whitelist for safety) ──
UPDATABLE_FILES = {
    "seraphina_bot.py",
    "jacob_bot.py",
    "loachy_bot.py",
    "farm_api.py",
    "farm_alerts.py",
    "seraphina_dashboard.html",
    "jacob_dashboard.html",
    "loachy_dashboard.html",
    "index.html",
    "login.html",
}

@app.route("/apply-update", methods=["POST"])
def apply_update():
    """
    Accepts a file upload from Claude (or any authorized caller).
    Body: { "filename": "seraphina_bot.py", "content_b64": "<base64>" }
    Auth: ?token=FARM_API_TOKEN

    After upload, optionally restarts the affected bot:
    Body can also include: { "restart": true }

    This is how Claude deploys updates without needing SCP.
    """
    if not valid_api_token(request):
        return jsonify({"ok": False, "error": "Invalid or missing token"}), 401

    try:
        body     = request.get_json()
        filename = body.get("filename", "").strip()
        b64      = body.get("content_b64", "")
        restart  = body.get("restart", False)

        if not filename:
            return jsonify({"ok": False, "error": "filename required"}), 400
        if filename not in UPDATABLE_FILES:
            return jsonify({"ok": False, "error": f"{filename} not in allowed file list"}), 403
        if not b64:
            return jsonify({"ok": False, "error": "content_b64 required"}), 400

        # Decode and write
        content  = base64.b64decode(b64)
        fpath    = os.path.join(WORK_DIR, filename)

        # Backup old file first
        backup = fpath + ".bak"
        if os.path.exists(fpath):
            os.replace(fpath, backup)

        with open(fpath, "wb") as f:
            f.write(content)

        log_msg = f"[APPLY-UPDATE] {filename} written ({len(content)} bytes)"
        print(log_msg)

        # Optionally restart the affected bot
        restarted = None
        if restart:
            for name, cfg in BOTS.items():
                if cfg["script"] == filename:
                    kill_all_screens(cfg["screen"])
                    time.sleep(1)
                    subprocess.Popen(
                        ["screen", "-dmS", cfg["screen"], "python3",
                         os.path.join(WORK_DIR, cfg["script"])],
                        cwd=WORK_DIR
                    )
                    restarted = name
                    print(f"[APPLY-UPDATE] Restarted {name}")
                    break

        return jsonify({
            "ok":       True,
            "filename": filename,
            "bytes":    len(content),
            "backup":   backup,
            "restarted": restarted,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print("Jacob's Money Farm API starting on port 5000...")
    app.run(host="0.0.0.0", port=5000, debug=False)
