#!/usr/bin/env python3
import subprocess, time, os, requests, hashlib

TOKEN = os.environ.get("GITHUB_TOKEN")
REPO = "schmittjacob1123/money-farm-bots"
BRANCH = "claude/check-farm-bot-data-zkzuE"
HEADERS = {"Authorization": f"token {TOKEN}"}
BASE = f"https://api.github.com/repos/{REPO}/contents"

BOT_SCREENS = {
    "jacob_bot.py": "jacob",
    "seraphina_bot.py": "seraphina",
    "loachy_bot.py": "loachy",
    "farm_api.py": "farmapi",
    "farm_alerts.py": "alerts",
}

STATIC_FILES = ["index.html", "login.html", "jacob_dashboard.html", "seraphina_dashboard.html", "loachy_dashboard.html"]

def get_remote_sha(filename):
    r = requests.get(f"{BASE}/{filename}?ref={BRANCH}", headers=HEADERS, timeout=10)
    if r.status_code == 200:
        return r.json().get("sha")
    return None

def get_remote_content(filename):
    r = requests.get(f"{BASE}/{filename}?ref={BRANCH}", headers=HEADERS, timeout=10)
    if r.status_code == 200:
        import base64
        return base64.b64decode(r.json()["content"]).decode()
    return None

def local_sha(filepath):
    # Use git's blob SHA format
    try:
        result = subprocess.run(["git", "hash-object", filepath], capture_output=True, text=True, cwd="/home/ubuntu")
        return result.stdout.strip()
    except:
        return None

def restart_screen(screen_name, script):
    env_str = "env $(cat /home/ubuntu/.env | xargs)"
    subprocess.run(f"screen -S {screen_name} -X quit", shell=True)
    time.sleep(1)
    subprocess.run(f"{env_str} screen -dmS {screen_name} python3 /home/ubuntu/{script}", shell=True)
    print(f"[deploy] Restarted {screen_name}")

ALL_FILES = list(BOT_SCREENS.keys()) + STATIC_FILES

print("[deploy] Farm deploy watcher started")
while True:
    try:
        for filename in ALL_FILES:
            filepath = f"/home/ubuntu/{filename}"
            remote_sha = get_remote_sha(filename)
            local = local_sha(filepath)
            if remote_sha and local and remote_sha != local:
                print(f"[deploy] Change detected: {filename}")
                content = get_remote_content(filename)
                if content:
                    with open(filepath, "w") as f:
                        f.write(content)
                    subprocess.run(["git", "add", filename], cwd="/home/ubuntu")
                    subprocess.run(["git", "commit", "-m", f"auto-deploy: {filename}"], cwd="/home/ubuntu")
                    if filename in BOT_SCREENS:
                        restart_screen(BOT_SCREENS[filename], filename)
                    else:
                        print(f"[deploy] Updated static file: {filename}")
    except Exception as e:
        print(f"[deploy] Error: {e}")
    time.sleep(30)
