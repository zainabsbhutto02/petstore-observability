"""A short concurrent burst, used to make the in-flight-calls panel show something.

    python client/grpc_burst.py [host:port] [threads] [seconds] [max_real_orders]
    python client/grpc_burst.py localhost:50051 16 30 10

Many threads call the service at the same time for a few seconds, so several calls overlap
and the in-flight gauge rises above zero. (With one call at a time, each lasting ~15 ms, the
gauge is always 0 whenever Prometheus samples it.) This is not a load test: it is a few
seconds of overlap. To protect stock, at most MAX_REAL_ORDERS real orders are placed in
total; after that the order calls are quick rejections (quantity 0).
"""
import os
import sys
import threading
import time

import grpc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("PETSTORE_GRPC_GEN", os.path.join(ROOT, "grpc_gen")))
import petstore_pb2 as pb              # noqa: E402
import petstore_pb2_grpc as pb_grpc    # noqa: E402

target = sys.argv[1] if len(sys.argv) > 1 else "localhost:50051"
threads = int(sys.argv[2]) if len(sys.argv) > 2 else 16
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 30
MAX_REAL_ORDERS = int(sys.argv[4]) if len(sys.argv) > 4 else 10

channel = grpc.insecure_channel(target)
stub = pb_grpc.PetStoreStub(channel)
lock = threading.Lock()
stats = {"calls": 0, "real_orders": 0, "errors": 0, "now": 0, "peak": 0, "codes": {}}
stop_at = time.monotonic() + seconds


def worker(index):
    while time.monotonic() < stop_at:
        with lock:
            stats["now"] += 1
            stats["peak"] = max(stats["peak"], stats["now"])
            place_real = index % 4 == 0 and stats["real_orders"] < MAX_REAL_ORDERS
            if place_real:
                stats["real_orders"] += 1
        try:
            if place_real:
                # spread the orders over popular products, one item each
                stub.CreateOrder(pb.CreateOrderRequest(product_id=1 + (stats["real_orders"] % 3), quantity=1), timeout=10)
            else:
                stub.ListProducts(pb.ListProductsRequest(), timeout=10)
        except grpc.RpcError as err:
            with lock:
                stats["errors"] += 1
                stats["codes"][err.code().name] = stats["codes"].get(err.code().name, 0) + 1
        finally:
            with lock:
                stats["now"] -= 1
                stats["calls"] += 1


print(f"burst: {threads} threads for {seconds:g}s against {target} (at most {MAX_REAL_ORDERS} real orders)")
started = time.monotonic()
pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
for t in pool:
    t.start()
for t in pool:
    t.join()
elapsed = time.monotonic() - started
print(f"done: {stats['calls']} calls in {elapsed:.1f}s ({stats['calls'] / elapsed:.0f}/s), "
      f"{stats['real_orders']} real orders, errors: {stats['errors']} {stats['codes'] or ''}, "
      f"peak calls in flight seen by this client: {stats['peak']}")