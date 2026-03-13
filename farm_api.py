"""
Jacob's Money Farm — Control API
Runs on port 5000. Lets the homepage start/stop/reset bots.

SETUP:  pip install flask flask-cors
RUN:    screen -S farmapi -> python3 farm_api.py -> Ctrl+A D
"""

import subprocess, os, json, time, secrets, hashlib
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app, supports_credentials=True)

# ── SESSION AUTH ──
FARM_PASSWORD = os.environ.get("FARM_PASSWORD", "kickrocks!")
SESSION_SECRET = "d4f2861dc96539bb657aef8e80e37e590eb214de23308c69a85c5deefefe0ea6"
SESSIONS = set()  # in-memory valid tokens

def make_token():
    return secrets.token_hex(32)

def valid_session(req):
    token = req.cookies.get("farm_session")
    return token and token in SESSIONS

WORK_DIR = "/home/ubuntu"

BOTS = {
    "jacob":     {"script": "polymarket_bot.py",  "screen": "polymarket", "state": ["positions.json", "trades.csv", "pnl_history.json", "live_data.json"]},
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
        subprocess.run(["screen", "-X", "-S", cfg["screen"], "quit"], capture_output=True)
        return jsonify({"ok": True, "message": f"{botname} stopped"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/bot/<botname>/reset", methods=["POST"])
def reset_bot(botname):
    if botname not in BOTS:
        return jsonify({"ok": False, "error": f"Unknown bot: {botname}"}), 404
    cfg = BOTS[botname]

    if is_running(cfg["screen"]):
        subprocess.run(["screen", "-X", "-S", cfg["screen"], "quit"], capture_output=True)
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



FARM_API_TOKEN = os.environ.get("FARM_API_TOKEN", "")

# Whitelist of files Claude is allowed to read remotely
READABLE_FILES = [
    "seraphina_bot.py", "jacob_bot.py", "loachy_bot.py",
    "farm_api.py", "farm_deploy.py", "farm_alerts.py", "farm_gist.py",
    "seraphina_dashboard.html", "jacob_dashboard.html", "loachy_dashboard.html",
    "index.html", "login.html",
    "seraphina_data.json", "seraphina_state.json", "seraphina_daily.json",
    "loachy_data.json", "loachy_state.json", "loachy_pending.json",
    "jacob_data.json",
]

def valid_api_token(req):
    token = req.args.get("token") or req.headers.get("X-API-Token")
    return FARM_API_TOKEN and token == FARM_API_TOKEN


@app.route("/farm-data", methods=["GET"])
def farm_data():
    """Live farm status for Claude — token protected."""
    if not valid_api_token(request):
        return jsonify({"error": "unauthorized"}), 401
    try:
        screens = subprocess.run(["screen", "-ls"], capture_output=True, text=True).stdout.strip()

        def read_json(fname):
            try:
                with open(os.path.join(WORK_DIR, fname)) as f:
                    return json.load(f)
            except:
                return None

        return jsonify({
            "seraphina_data": read_json("seraphina_data.json"),
            "jacob_data":     read_json("jacob_data.json"),
            "loachy_data":    read_json("loachy_data.json"),
            "loachy_state":   read_json("loachy_state.json"),
            "loachy_pending": read_json("loachy_pending.json"),
            "screens":        screens,
            "updated_at":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/file", methods=["GET"])
def read_file():
    """Return contents of a whitelisted file — token protected."""
    if not valid_api_token(request):
        return jsonify({"error": "unauthorized"}), 401
    name = request.args.get("name", "")
    if name not in READABLE_FILES:
        return jsonify({"error": f"file not in whitelist: {name}"}), 403
    fpath = os.path.join(WORK_DIR, name)
    try:
        with open(fpath) as f:
            content = f.read()
        return jsonify({"name": name, "content": content, "size": len(content)})
    except FileNotFoundError:
        return jsonify({"error": f"{name} not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("Jacob's Money Farm API starting on port 5000...")
    app.run(host="0.0.0.0", port=5000, debug=False)
