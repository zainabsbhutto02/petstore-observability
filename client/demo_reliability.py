"""Run:  python client/demo_reliability.py      (needs no real backend)

Shows, in order: success, retry after a server error, retry after a timeout,
circuit breaker opening, fail-fast while open, and recovery.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_server import FakeServer                                   # noqa: E402
from order_client import CircuitBreaker, CircuitOpenError, OrderClient, ServiceUnavailable  # noqa: E402

server = FakeServer(slow_seconds=1.0)
breaker = CircuitBreaker(failure_threshold=3, reset_timeout=3.0)
client = OrderClient(server.url, timeout=0.3, max_attempts=3, backoff_base=0.2, backoff_cap=1.0,
                     breaker=breaker)
print("SETTINGS: timeout=0.3s, max_attempts=3, backoff=0.2s*2^n with jitter (cap 1s), "
      "breaker opens after 3 failed calls, reset after 3s\n")

print("1) SUCCESS")
r = client.create_order(1, 2)
print(f"   -> HTTP {r.status}, order #{r.body['id']}\n")

print("2) RETRY after server errors (first 2 requests return 503, third works)")
server.fail_next = 2
r = client.create_order(1, 1, idempotency_key="demo-key-1")
print(f"   -> HTTP {r.status}, order #{r.body['id']}; server saw 1 order for this key: "
      f"{sum(1 for k in server.orders if k == 'demo-key-1') == 1}\n")

print("3) RETRY after a TIMEOUT (first request is slower than the 0.3s timeout)")
server.mode = "slow"
server.fail_next = 0
import threading                                                     # noqa: E402
threading.Timer(0.5, lambda: setattr(server, "mode", "ok")).start()  # server speeds up again
r = client.create_order(2, 1, idempotency_key="demo-key-2")
print(f"   -> HTTP {r.status}, order #{r.body['id']}\n")

print("4) SERVER DOWN: 3 failed calls in a row open the breaker")
server.mode = "error"
for n in range(1, 4):
    try:
        client.list_products()
    except ServiceUnavailable as exc:
        print(f"   call {n}: gave up ({exc})")
print(f"   breaker state: {breaker.state.upper()}\n")

print("5) BREAKER OPEN: next call fails fast, server is not contacted")
before = server.requests
try:
    client.list_products()
except CircuitOpenError as exc:
    print(f"   -> rejected immediately ({exc}); requests that reached server: {server.requests - before}\n")

print("6) RECOVERY: server comes back, wait for the reset timeout, trial call closes the breaker")
server.mode = "ok"
time.sleep(3.1)
r = client.list_products()
print(f"   -> HTTP {r.status}; breaker state: {breaker.state.upper()}")
server.close()