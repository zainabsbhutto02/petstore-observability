"""A tiny controllable server used to demonstrate and test failures on demand.

Set `.mode` to: "ok", "error" (HTTP 503), "slow" (sleeps longer than the client timeout),
or "limited" (HTTP 429 + Retry-After).  `.fail_next = N` fails only the next N requests.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    def __init__(self, slow_seconds=1.0):
        self.mode = "ok"
        self.fail_next = 0
        self.slow_seconds = slow_seconds
        self.requests = 0
        self.orders = {}               # idempotency key -> order (shows retries never duplicate)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload, extra=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass                      # the client timed out and hung up: expected

            def _handle(self):
                outer.requests += 1
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                mode = outer.mode
                if outer.fail_next > 0:
                    outer.fail_next -= 1
                    mode = "error"
                if mode == "slow":
                    time.sleep(outer.slow_seconds)
                if mode == "error":
                    return self._reply(503, {"error": "Service unavailable."})
                if mode == "limited":
                    return self._reply(429, {"error": "Too many requests."}, {"Retry-After": "1"})
                if self.command == "GET":
                    return self._reply(200, [{"id": 1, "name": "Dog Food"}])
                key = self.headers.get("Idempotency-Key")
                if key in outer.orders:
                    return self._reply(201, outer.orders[key], {"Idempotent-Replayed": "true"})
                order = {"id": len(outer.orders) + 1, **(body or {}), "status": "pending"}
                outer.orders[key] = order
                return self._reply(201, order)

            do_GET = do_POST = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()