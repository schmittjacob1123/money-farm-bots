#!/usr/bin/env python3
import os, json, time, requests
from datetime import datetime

TOKEN = os.environ.get("GITHUB_TOKEN", "")
GIST_ID_FILE = "/home/ubuntu/farm_gist_id.txt"
DATA_FILES = {
    "loachy_data.json":    "/home/ubuntu/loachy_data.json",
    "seraphina_data.json": "/home/ubuntu/seraphina_data.json",
    "jacob_data.json":     "/home/ubuntu/jacob_data.json",
    "loachy_state.json":   "/home/ubuntu/loachy_state.json",
    "loachy_pending.json": "/home/ubuntu/loachy_pending.json",
}
LOG_FILE = "/home/ubuntu/loachy.log"
INTERVAL = 300

def read_file(path, lines=None):
    try:
        with open(path) as f:
            if lines:
                all_lines = f.readlines()
                return "".join(all_lines[-lines:])
            return f.read()
    except:
        return f"(not found: {path})"

def get_screen_status():
    import subprocess
    try:
        r = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        return r.stdout.strip()
    except:
        return "unknown"

def build_gist_content():
    files = {}
    summary = {"updated_at": datetime.utcnow().isoformat() + "Z", "screens": get_screen_status()}
    for key, path in DATA_FILES.items():
        try:
            with open(path) as f:
                summary[key.replace(".json","")] = json.load(f)
        except:
            summary[key.replace(".json","")] = None
    files["farm_status.json"] = {"content": json.dumps(summary, indent=2)}
    files["loachy.log"] = {"content": read_file(LOG_FILE, lines=80) or "(empty)"}
    return files

def load_gist_id():
    try:
        with open(GIST_ID_FILE) as f:
            return f.read().strip()
    except:
        return None

def save_gist_id(gid):
    with open(GIST_ID_FILE, "w") as f:
        f.write(gid)

def create_gist(files):
    r = requests.post("https://api.github.com/gists",
        headers={"Authorization": f"token {TOKEN}"},
        json={"description": "Jacob's Money Farm live status", "public": False, "files": files},
        timeout=15)
    r.raise_for_status()
    data = r.json()
    gid = data["id"]
    print(f"[gist] Created! Raw URL: https://gist.githubusercontent.com/schmittjacob1123/{gid}/raw/farm_status.json")
    return gid

def update_gist(gist_id, files):
    r = requests.patch(f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {TOKEN}"},
        json={"files": files}, timeout=15)
    r.raise_for_status()

def push():
    files = build_gist_content()
    gist_id = load_gist_id()
    if not gist_id:
        gist_id = create_gist(files)
        save_gist_id(gist_id)
    else:
        update_gist(gist_id, files)
    print(f"[gist] Pushed at {datetime.utcnow().isoformat()}Z")

print("[gist] Farm Gist pusher started")
while True:
    try:
        push()
    except Exception as e:
        print(f"[gist] Error: {e}")
    time.sleep(300)
