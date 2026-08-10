"""A Palworld dedicated server that misbehaves the way real ones do.

Run as a subprocess by fake_service.py:

    python fake_palworld.py <port> <mode-file>

The mode file is read on every request, so a test can change the server's
behaviour mid-flight without restarting anything:

  ok    — answers normally.
  slow  — answers, but after a delay long enough to trip a short client timeout.
  hang  — accepts the TCP connection and NEVER responds. This is the important
          one and the reason a mock isn't enough: a wedged PalServer is not a
          refused connection. The client blocks until its own timeout, which is
          a different code path from "dead" in every layer above it, and it is
          the shape of the original "the server is hanging" report.
  dead  — the process exits; connections are refused.

Memory climbs with uptime so the leak watchdog has something to watch.
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
MODE_FILE = sys.argv[2]
STARTED = time.time()


def mode() -> str:
    try:
        with open(MODE_FILE) as f:
            return f.read().strip() or "ok"
    except OSError:
        return "ok"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _should_answer(self) -> bool:
        m = mode()
        if m == "slow":
            time.sleep(8.0)
        elif m == "hang":
            # Hold the connection open and never reply, until a test lets go.
            while mode() == "hang":
                time.sleep(0.2)
            return False
        return True

    def do_GET(self):
        if not self._should_answer():
            return
        if self.path.endswith("/metrics"):
            uptime = int(time.time() - STARTED)
            body = (
                '{"serverfps":58,"currentplayernum":1,"serverframetime":17.2,'
                f'"maxplayernum":32,"uptime":{uptime},"basecampnum":4,"days":12}}'
            )
        elif self.path.endswith("/players"):
            body = (
                '{"players":[{"name":"Ari","accountName":"ari","playerId":"p1",'
                '"userId":"u1","ip":"10.0.0.5","ping":42.0,"location_x":1.0,'
                '"location_y":2.0,"level":31,"building_count":88}]}'
            )
        elif self.path.endswith("/info"):
            body = '{"version":"v0.6.1","servername":"sim","description":""}'
        else:
            body = "{}"
        self._send(body)

    def do_POST(self):
        if not self._should_answer():
            return
        self._send("{}")

    def _send(self, body: str):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(HTTPServer):
    allow_reuse_address = True

    # One stuck request must not block the others, or the test would be
    # measuring this toy's threading model instead of palctl's behaviour.
    def process_request(self, request, client_address):
        threading.Thread(
            target=self._handle, args=(request, client_address), daemon=True
        ).start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass
        finally:
            try:
                self.shutdown_request(request)
            except Exception:
                pass


def main() -> None:
    while mode() == "dead":
        time.sleep(0.2)
    srv = Server(("127.0.0.1", PORT), Handler)

    def watch_for_death():
        while True:
            if mode() == "dead":
                os._exit(0)  # the process really goes away, like a crash
            time.sleep(0.2)

    threading.Thread(target=watch_for_death, daemon=True).start()
    srv.serve_forever()


main()
