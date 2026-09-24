#!/usr/bin/env python3
"""
One public port in front of the two demo servers.

Railway (like most container hosts) exposes a single port. This demo has two
servers that both have to be reachable from a browser:

    auth/server.py   the sign-in page and the MicroDMS portal, which mints the
                     PluG session token server-side with the DevRev AAT
    api/server.py    the mock OnlineDMS API and its data viewer

So this process starts both on loopback and proxies by path:

    /OnlineSalesAPI/*   ->  api/server.py   (127.0.0.1:8900)
    /db                 ->  api/server.py   its SQL table browser
    everything else     ->  auth/server.py  (127.0.0.1:8899)

Neither child is modified in any way that changes its behaviour - the point of
a proxy rather than a merge is that the demo logic stays byte-identical to what
was tested locally.
"""
import json, os, signal, subprocess, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE      = os.path.dirname(os.path.abspath(__file__))
PORT      = int(os.environ.get("PORT", "8080"))
API_PORT  = int(os.environ.get("MOCK_DMS_PORT", "8900"))
AUTH_PORT = int(os.environ.get("AUTH_PORT", "8899"))
# Paths the DMS API owns. /db is its SQL table browser — the screen that shows the
# data behind every answer, which is half the point of the demo.
API_PATHS = ("/OnlineSalesAPI", "/db")
WEBHOOK_PATH = "/devrev-webhook"
WEBHOOK_LOG  = os.path.join(HERE, "webhook.log")

# Headers that belong to the hop, not the message. Forwarding these corrupts the
# response - a Content-Length copied from the child fights the one we write.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"}


def child(script, env_extra):
    env = {**os.environ, **env_extra}
    env.setdefault("PYTHONUNBUFFERED", "1")
    p = subprocess.Popen([sys.executable, script], cwd=HERE, env=env)
    print(f"[router] started {script} pid={p.pid} {env_extra}", flush=True)
    return p


def wait_for(port, name, timeout=40):
    """A child that is not listening yet would give the first visitor a 502."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            print(f"[router] {name} is up on {port}", flush=True); return True
        except urllib.error.HTTPError:
            print(f"[router] {name} is up on {port}", flush=True); return True
        except Exception:
            time.sleep(0.4)
    print(f"[router] WARNING {name} did not come up on {port}", flush=True)
    return False


class Router(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TVSDealerBotDemo/1.0"

    def _target(self):
        return API_PORT if self.path.startswith(API_PATHS) else AUTH_PORT

    # ── DevRev webhook sink ──────────────────────────────────────────────────
    # ai-agents.events.execute-async delivers its result to a webhook rather than
    # returning it, so a public endpoint is required to observe what an agent
    # invoked on an existing conversation can actually see. POST appends the raw
    # body to a log; GET returns the log. DevRev verifies a new webhook by posting
    # a challenge that must be echoed back, which the first branch handles.
    def _webhook(self):
        if self.command == "GET":
            try:
                body = open(WEBHOOK_LOG, "rb").read()
            except FileNotFoundError:
                body = b"(nothing received yet)"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return

        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        with open(WEBHOOK_LOG, "ab") as fh:
            fh.write(b"\n===== " + time.strftime("%H:%M:%S").encode() + b" =====\n" + raw)
        out = b"{}"
        try:
            payload = json.loads(raw or b"{}")
            if payload.get("type") == "verify" or "challenge" in payload:
                out = json.dumps({"challenge": payload.get("challenge")}).encode()
        except Exception:                                        # noqa: BLE001
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)

    def _proxy(self):
        if self.path.split("?")[0] == WEBHOOK_PATH:
            return self._webhook()

        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        url = f"http://127.0.0.1:{self._target()}{self.path}"
        req = urllib.request.Request(url, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in HOP:
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload, status, headers = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            payload, status, headers = e.read(), e.code, e.headers
        except Exception as e:                                   # noqa: BLE001
            payload = f"upstream unavailable: {e}".encode()
            status, headers = 502, {}
        self.send_response(status)
        for k, v in (headers.items() if headers else []):
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy

    def log_message(self, fmt, *a):
        sys.stderr.write("[router] %s\n" % (fmt % a))


def main():
    procs = [
        child(os.path.join("api", "server.py"),  {"MOCK_DMS_PORT": str(API_PORT)}),
        child(os.path.join("auth", "server.py"), {"AUTH_PORT": str(AUTH_PORT),
                                                  "PORTAL_HTML": os.path.join(HERE, "portal.html"),
                                                  "DMS_DB": os.path.join(HERE, "db", "onlinedms.db")}),
    ]

    def stop(*_):
        for p in procs:
            p.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threading.Thread(target=wait_for, args=(API_PORT, "mock DMS"), daemon=True).start()
    threading.Thread(target=wait_for, args=(AUTH_PORT, "auth/portal"), daemon=True).start()

    print(f"[router] listening on 0.0.0.0:{PORT}", flush=True)
    print(f"[router]   {'  '.join(API_PATHS)}  -> mock DMS  :{API_PORT}", flush=True)
    print(f"[router]   /*              -> portal    :{AUTH_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Router).serve_forever()


if __name__ == "__main__":
    main()
