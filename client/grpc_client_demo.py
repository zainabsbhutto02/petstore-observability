"""Repeatable gRPC calls: successes and failures, with client-side timing.

Usage (after generating the gRPC code, see README.md):
    python client/grpc_client_demo.py [host:port] [rounds]
    python client/grpc_client_demo.py localhost:50051 5

Client-side time (printed here) is longer than server-side time (Grafana p50/p95):
it also includes serialising the request, the network hop and the reply.
"""
import os
import statistics
import sys
import time

import grpc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("PETSTORE_GRPC_GEN", os.path.join(ROOT, "grpc_gen")))
import petstore_pb2 as pb              # noqa: E402
import petstore_pb2_grpc as pb_grpc    # noqa: E402

target = sys.argv[1] if len(sys.argv) > 1 else "localhost:50051"
rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5

channel = grpc.insecure_channel(target)
stub = pb_grpc.PetStoreStub(channel)
timings = {}


def call(label, fn):
    started = time.perf_counter()
    try:
        fn()
        code = "OK"
    except grpc.RpcError as err:
        code = err.code().name
    ms = (time.perf_counter() - started) * 1000
    timings.setdefault(label, []).append(ms)
    print(f"{label:<28} -> {code:<20} {ms:7.2f} ms (client-side)")


best = max(stub.ListProducts(pb.ListProductsRequest(), timeout=5).products, key=lambda p: p.stock)
print(f"target={target} rounds={rounds}; ordering '{best.name}' (id {best.id}, stock {best.stock})\n")

for n in range(rounds):
    call("ListProducts", lambda: stub.ListProducts(pb.ListProductsRequest(), timeout=5))
    call("CreateOrder (success)", lambda: stub.CreateOrder(pb.CreateOrderRequest(product_id=best.id, quantity=1), timeout=5))
    call("CreateOrder (bad quantity)", lambda: stub.CreateOrder(pb.CreateOrderRequest(product_id=best.id, quantity=0), timeout=5))
    call("CreateOrder (no such product)", lambda: stub.CreateOrder(pb.CreateOrderRequest(product_id=9999, quantity=1), timeout=5))
    call("CreateOrder (too much stock)", lambda: stub.CreateOrder(pb.CreateOrderRequest(product_id=best.id, quantity=100000), timeout=5))
    key = f"demo-{time.time_ns()}"
    request = pb.CreateOrderRequest(product_id=best.id, quantity=1, idempotency_key=key)
    call("CreateOrder (key, first)", lambda: stub.CreateOrder(request, timeout=5))
    call("CreateOrder (key, repeat)", lambda: stub.CreateOrder(request, timeout=5))
    print()

print("client-side median per call type:")
for label, values in timings.items():
    print(f"  {label:<30} {statistics.median(values):7.2f} ms over {len(values)} calls")