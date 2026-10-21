"""Run against the REAL stack (docker compose up):  python client/demo_rate_limit.py

Sends 12 orders quickly. The first 10 succeed, the next get HTTP 429 with Retry-After;
then the script waits as told and succeeds again. Rate limiting is shown here on its own,
separately from the failure/retry demo.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from order_client import OrderClient                                 # noqa: E402

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000"
client = OrderClient(base, max_attempts=1)          # no automatic retry, so the raw 429 is visible
retry_after = 1

# Pick the product with the most stock so the first 10 orders are real 201s.
products = client.list_products().body
best = max(products, key=lambda p: p["stock"])
print(f"ordering '{best['name']}' (id {best['id']}, stock {best['stock']}), quantity 1 each\n")
if best["stock"] < 12:
    print("NOTE: low stock, so some orders will return 409 (out of stock). A 409 still counts\n"
          "toward the rate limit, but 201s make a cleaner screenshot.\n")
product_id = best["id"]

for n in range(1, 13):
    r = client.create_order(product_id, 1)
    note = ""
    if r.status == 429:
        retry_after = int(r.headers.get("Retry-After", 1))
        note = f"  Retry-After={retry_after}s  body={r.body}"
    print(f"request {n:2d}: HTTP {r.status}{note}")

print(f"\nwaiting {retry_after}s as the server asked ...")
time.sleep(retry_after + 0.5)
r = client.create_order(product_id, 1)
print(f"after waiting: HTTP {r.status}")