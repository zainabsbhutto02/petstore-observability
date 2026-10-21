"""Realistic, repeatable order traffic over HTTP or gRPC, with a correctness check.

    python client/workload.py http localhost:5000  --count 45  --per-minute 9
    python client/workload.py grpc localhost:50051 --count 300 --per-minute 60

Behaviour modelled on real customers (fixed --seed, so a run can be repeated exactly):
  * popular products are ordered more often (Dog Food and Cat Food most),
  * most orders are for 1 item, some for 2-3, a few bulk orders of 5,
  * about 8% of requests are retries of the previous order with the SAME idempotency key,
  * about 10% are mistakes: quantity 0, an unknown product id, or an absurd quantity.

After the run it RECONCILES: stock before - stock after must equal the quantity of the
orders that were really created (a retried key counts once), and every order total must be
unit price x quantity. It prints PASS or FAIL, so the traffic that fills the dashboards is
also a correctness test.

HTTP note: POST /orders is rate limited to 10 requests per minute per client IP, so the
default HTTP pace (9 per minute) stays just under it. Use --per-minute 30 to see 429s too.
"""
import argparse
import collections
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

CATALOG = {1: ("Dog Food", 24.99), 2: ("Cat Food", 19.99), 3: ("Pet Toy", 9.99),
           4: ("Dog Treats", 7.49), 5: ("Dog Collar", 14.99), 6: ("Cat Toy", 6.99)}
PRODUCT_WEIGHTS = {1: 25, 2: 25, 3: 15, 4: 15, 5: 10, 6: 10}
QUANTITY_WEIGHTS = {1: 60, 2: 25, 3: 10, 5: 5}


def pick(rng, weights):
    return rng.choices(list(weights), weights=list(weights.values()))[0]


class HttpTransport:
    def __init__(self, target):
        from order_client import OrderClient
        self.client = OrderClient("http://" + target, max_attempts=1, timeout=5)

    def products(self):
        return {p["id"]: p["stock"] for p in self.client.list_products().body}

    def create(self, product_id, quantity, key):
        r = self.client.create_order(product_id, quantity, idempotency_key=key)
        body = r.body if isinstance(r.body, dict) else {}
        order = body if r.status == 201 else None
        return str(r.status), order


class GrpcTransport:
    def __init__(self, target):
        import grpc
        sys.path.insert(0, os.environ.get("PETSTORE_GRPC_GEN", os.path.join(ROOT, "grpc_gen")))
        import petstore_pb2 as pb
        import petstore_pb2_grpc as pb_grpc
        self.grpc, self.pb = grpc, pb
        self.stub = pb_grpc.PetStoreStub(grpc.insecure_channel(target))

    def products(self):
        reply = self.stub.ListProducts(self.pb.ListProductsRequest(), timeout=5)
        return {p.id: p.stock for p in reply.products}

    def create(self, product_id, quantity, key):
        try:
            o = self.stub.CreateOrder(self.pb.CreateOrderRequest(
                product_id=product_id, quantity=quantity, idempotency_key=key), timeout=5)
            return "OK", {"id": o.id, "product_id": o.product_id, "quantity": o.quantity,
                          "unit_price": o.unit_price, "total_price": o.total_price}
        except self.grpc.RpcError as err:
            return err.code().name, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transport", choices=["http", "grpc"])
    ap.add_argument("target")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--per-minute", type=float, default=None)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--run-id", default=None,
                    help="part of every idempotency key; default is the current time, so repeating a "
                         "run (same seed = same traffic pattern) creates NEW orders instead of replays")
    args = ap.parse_args()
    per_minute = args.per_minute or (9 if args.transport == "http" else 60)
    transport = (HttpTransport if args.transport == "http" else GrpcTransport)(args.target)
    rng = random.Random(args.seed)
    run_id = args.run_id or time.strftime("%H%M%S")

    stock_before = transport.products()
    print(f"{args.transport.upper()} workload: {args.count} requests at {per_minute:g}/min, seed {args.seed}")
    print(f"stock before: { {CATALOG[i][0]: s for i, s in stock_before.items()} }\n")

    outcomes = collections.Counter()
    created = {}                       # idempotency key -> order (a retried key counts once)
    last = None
    problems = []
    for n in range(1, args.count + 1):
        roll = rng.random()
        if roll < 0.08 and last is not None:
            product_id, quantity, key, kind = *last, "retry"          # same key again
        else:
            key = f"wl-{args.transport}-{run_id}-{n}-{rng.getrandbits(32):08x}"
            if roll < 0.12:
                product_id, quantity, kind = pick(rng, PRODUCT_WEIGHTS), 0, "bad quantity"
            elif roll < 0.15:
                product_id, quantity, kind = 9999, 1, "unknown product"
            elif roll < 0.18:
                product_id, quantity, kind = pick(rng, PRODUCT_WEIGHTS), 5000, "too many"
            else:
                product_id, quantity, kind = pick(rng, PRODUCT_WEIGHTS), pick(rng, QUANTITY_WEIGHTS), "order"
                last = (product_id, quantity, key)

        started = time.perf_counter()
        status, order = transport.create(product_id, quantity, key)
        ms = (time.perf_counter() - started) * 1000
        outcomes[(kind, status)] += 1
        if order is not None:
            if abs(order["total_price"] - round(CATALOG[order["product_id"]][1] * order["quantity"], 2)) > 0.001:
                problems.append(f"wrong total on order {order['id']}")
            if key in created and created[key]["id"] != order["id"]:
                problems.append(f"retry of {key} created a second order")
            created.setdefault(key, order)
        print(f"{n:4d} {kind:<16} product={product_id:<5} qty={quantity:<5} -> {status:<20} {ms:6.1f} ms")
        time.sleep(60.0 / per_minute)

    stock_after = transport.products()
    expected = collections.Counter()
    for order in created.values():
        expected[order["product_id"]] += order["quantity"]

    print("\noutcomes (kind, status): count")
    for (kind, status), count in sorted(outcomes.items()):
        print(f"  {kind:<16} {status:<20} {count}")

    print("\nreconciliation (stock before - stock after == quantity of orders actually created):")
    for pid, (name, _) in CATALOG.items():
        dropped = stock_before[pid] - stock_after[pid]
        ok = dropped == expected[pid]
        if not ok:
            problems.append(f"stock mismatch for {name}")
        print(f"  {name:<11} stock {stock_before[pid]:>4} -> {stock_after[pid]:>4}  dropped {dropped:>3}  "
              f"orders created {expected[pid]:>3}  {'OK' if ok else 'MISMATCH'}")
    print(f"\nRESULT: {'PASS' if not problems else 'FAIL - ' + '; '.join(problems)}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()