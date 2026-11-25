"""
CI test of the self-update cycle with real executables.

    python tests/update_test.py OLD_EXE NEW_EXE

Serves NEW_EXE from a stand-in GitHub releases API, starts OLD_EXE pointed at it, asks it
to update through the same HTTP API the browser uses, and checks that it swapped itself
for NEW_EXE, restarted, and now reports the new version.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLD, NEW = sys.argv[1], sys.argv[2]
NEW_VERSION = "0.0.2"
APP, MOCK = 8785, 8796
work = tempfile.mkdtemp(prefix="onlyscans-update-")
exe = os.path.join(work, "OnlyScans.exe")
shutil.copy(OLD, exe)
new_bytes = open(NEW, "rb").read()


class GitHub(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/latest":
            body = json.dumps({
                "tag_name": "v" + NEW_VERSION, "draft": False, "prerelease": False,
                "html_url": "https://example.invalid/release", "body": "test release",
                "assets": [{"name": "OnlyScans.exe", "size": len(new_bytes),
                            "digest": "sha256:" + hashlib.sha256(new_bytes).hexdigest(),
                            "browser_download_url": f"http://127.0.0.1:{MOCK}/OnlyScans.exe"}]}).encode()
        elif self.path == "/OnlyScans.exe":
            body = new_bytes
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", MOCK), GitHub).serve_forever, daemon=True).start()

env = dict(os.environ, ONLYSCANS_UPDATE_URL=f"http://127.0.0.1:{MOCK}/latest",
           LOCALAPPDATA=os.path.join(work, "local"), APPDATA=os.path.join(work, "roaming"),
           XDG_CONFIG_HOME=os.path.join(work, "roaming"))
log = open(os.path.join(work, "console.txt"), "w")
flags = 0x00000008 if os.name == "nt" else 0   # DETACHED_PROCESS: no console to wait on
subprocess.Popen([exe, "--no-browser", "--http-port", str(APP)], env=env, stdout=log,
                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)


def page():
    for _ in range(120):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{APP}/", timeout=2).read().decode()
        except Exception:
            time.sleep(0.5)
    sys.exit("FAIL: OnlyScans never started")


def api(tok, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{APP}{path}", headers={"X-OnlyScans-Token": tok},
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def cleanup():
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/IM", "OnlyScans.exe"], capture_output=True)


try:
    tok = re.search(r'const TOKEN = "([^"]+)"', page()).group(1)
    before = api(tok, "/api/config")["data"]["version"]
    update = None
    for _ in range(40):
        update = api(tok, "/api/update")["data"]
        if update:
            break
        time.sleep(0.5)
    print("running", before, "| offered", update and update["version"])
    assert update and update["version"] == NEW_VERSION and update["can_install"], update
    result = api(tok, "/api/update/install", {})
    print("install:", result)
    assert "data" in result, result
    time.sleep(3)
    tok2 = re.search(r'const TOKEN = "([^"]+)"', page()).group(1)
    after = api(tok2, "/api/config")["data"]
    print("after restart:", after["version"], "| update offered:", after["update"])
    assert after["version"] == NEW_VERSION, after["version"]
    assert after["update"] is None
    assert open(exe, "rb").read() == new_bytes, "exe on disk is not the new build"
    for _ in range(20):
        if not os.path.exists(exe + ".old"):
            break
        time.sleep(0.5)
    assert not os.path.exists(exe + ".old"), "old exe was not cleaned up"
    print("UPDATE CYCLE OK")
finally:
    cleanup()
    log.close()
    print("--- console ---")
    print(open(os.path.join(work, "console.txt"), errors="ignore").read())
