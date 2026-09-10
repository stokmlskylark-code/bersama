import json, urllib.request

TOKEN = "ptlc_1KAvRG2KnUyCaAsInn7iLiOAfnq8RZMCVaDSykKG6ET"
BASE = "https://qwertyuiopaikal.private-panel.web.id/api/client/servers/34244783"

def cmd(command):
    req = urllib.request.Request(
        f"{BASE}/command",
        data=json.dumps({"command": command}).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "Application/vnd.pterodactyl.v1+json"
        },
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"CMD: {command[:50]} -> {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"CMD: {command[:50]} -> {e.code}")
    except Exception as e:
        print(f"CMD: {command[:50]} -> {e}")

def status():
    req = urllib.request.Request(
        BASE,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "Application/vnd.pterodactyl.v1+json"
        }
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        a = data["attributes"]
        print(f"Status: {a['status']} | Installing: {a['is_installing']}")
    except Exception as e:
        print(f"Status: {e}")

status()
cmd("env | grep SERVER_PORT")
cmd("env | grep WEB_PORT")
cmd("ps aux")
