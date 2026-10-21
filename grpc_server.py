"""gRPC front door for the Pet Store (Assignment 2, Part B).

Runs as its own process/container. It uses the same SQLite database and the same
business rules as the Flask REST API (stock check, idempotency keys), and exposes
Prometheus metrics on its own port.

Metrics (all labels are bounded - no user, request or trace IDs):
  grpc_server_handled_total{grpc_service,grpc_method,grpc_code}            Counter
  grpc_server_handling_seconds{grpc_service,grpc_method,grpc_code}         Histogram (server-side)
  grpc_server_in_flight_requests{grpc_service,grpc_method}                 Gauge
  petstore_grpc_orders_rejected_total{reason}                              Counter
  petstore_grpc_orders_created_total                                       Counter (gRPC counterpart of A1 petstore_orders_total)
"""
import hashlib
import json
import logging
import os
import sys
import time
from concurrent import futures

import grpc
from prometheus_client import Counter, Gauge, Histogram, start_http_server

sys.path.insert(0, os.environ.get("PETSTORE_GRPC_GEN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "grpc_gen")))
import petstore_pb2 as pb              # noqa: E402  (generated from proto/petstore.proto)
import petstore_pb2_grpc as pb_grpc    # noqa: E402

from database import get_connection, initialize_database  # noqa: E402

SERVICE = "petstore.v1.PetStore"
KNOWN_METHODS = {"ListProducts", "CreateOrder"}   # anything else is labelled "unknown"

handled_total = Counter(
    "grpc_server_handled_total",
    "RPCs completed on the server, by method and gRPC status code.",
    ["grpc_service", "grpc_method", "grpc_code"],
)
handling_seconds = Histogram(
    "grpc_server_handling_seconds",
    "Server-side time spent handling an RPC (excludes network and client time).",
    ["grpc_service", "grpc_method", "grpc_code"],
    # Observed server times are about 2-45 ms, so the buckets are dense there (a coarse 10-25 ms
    # bucket made p50 and p95 both collapse to the same bucket) and sparse above 100 ms.
    buckets=(0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
in_flight = Gauge(
    "grpc_server_in_flight_requests",
    "RPCs currently being handled.",
    ["grpc_service", "grpc_method"],
)
# The A1 counter petstore_orders_total only counts orders created through REST. This is its gRPC
# counterpart (new orders only, not idempotent replays); the dashboard adds the two together.
petstore_grpc_orders_created_total = Counter(
    "petstore_grpc_orders_created_total",
    "Orders successfully created through the gRPC service.",
)
orders_rejected = Counter(
    "petstore_grpc_orders_rejected_total",
    "CreateOrder calls rejected, by reason.",
    ["reason"],
)

log = logging.getLogger("petstore-grpc")


def method_label(full_method):
    """'/petstore.v1.PetStore/CreateOrder' -> 'CreateOrder'. Unknown names are collapsed so
    a caller cannot create unlimited label values by calling made-up methods."""
    name = full_method.rsplit("/", 1)[-1]
    return name if name in KNOWN_METHODS else "unknown"


class MetricsInterceptor(grpc.ServerInterceptor):
    """Measures every unary RPC in one place: count by status, duration, in-flight."""

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        method = method_label(handler_call_details.method)
        inner = handler.unary_unary

        def wrapped(request, context):
            gauge = in_flight.labels(SERVICE, method)
            gauge.inc()
            started = time.perf_counter()
            code = grpc.StatusCode.OK
            try:
                return inner(request, context)
            except Exception:
                code = context.code() or grpc.StatusCode.UNKNOWN   # set by context.abort()
                raise
            finally:
                elapsed = time.perf_counter() - started
                gauge.dec()
                handled_total.labels(SERVICE, method, code.name).inc()
                handling_seconds.labels(SERVICE, method, code.name).observe(elapsed)
                log.info(json.dumps({"service": "pet-store-grpc", "grpc_method": method,
                                     "grpc_code": code.name, "duration_ms": round(elapsed * 1000, 3)}))

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


ORDER_SELECT = """
    SELECT orders.*, products.name AS product_name
    FROM orders JOIN products ON products.id = orders.product_id
"""


def to_order(row):
    return pb.Order(id=row["id"], product_id=row["product_id"], product_name=row["product_name"],
                    quantity=row["quantity"], unit_price=row["unit_price"],
                    total_price=row["total_price"], status=row["status"],
                    created_at=row["created_at"])


def reject(context, reason, code, message):
    orders_rejected.labels(reason).inc()
    context.abort(code, message)


class PetStoreServicer(pb_grpc.PetStoreServicer):
    def ListProducts(self, request, context):
        connection = get_connection()
        try:
            rows = connection.execute(
                "SELECT id, name, category, price, stock FROM products ORDER BY id").fetchall()
        finally:
            connection.close()
        return pb.ListProductsResponse(products=[pb.Product(**dict(r)) for r in rows])

    def CreateOrder(self, request, context):
        product_id, quantity, key = request.product_id, request.quantity, request.idempotency_key
        if product_id <= 0:
            reject(context, "invalid_argument", grpc.StatusCode.INVALID_ARGUMENT, "product_id must be a positive integer.")
        if quantity <= 0:
            reject(context, "invalid_argument", grpc.StatusCode.INVALID_ARGUMENT, "quantity must be a positive integer.")
        if key and len(key) > 128:
            reject(context, "invalid_argument", grpc.StatusCode.INVALID_ARGUMENT, "idempotency_key must be 1 to 128 characters.")

        # Same hashing as the REST API, so a key means the same thing on both doors.
        request_hash = hashlib.sha256(
            json.dumps({"product_id": product_id, "quantity": quantity}, sort_keys=True).encode()
        ).hexdigest()

        connection = get_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")      # one writer at a time, as in the REST API
            if key:
                saved = connection.execute(
                    "SELECT request_hash, order_id FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
                if saved is not None:
                    if saved["request_hash"] != request_hash:
                        connection.rollback()
                        reject(context, "key_conflict", grpc.StatusCode.ALREADY_EXISTS,
                               "idempotency_key was already used with a different request.")
                    replayed = connection.execute(ORDER_SELECT + " WHERE orders.id = ?", (saved["order_id"],)).fetchone()
                    connection.rollback()
                    context.set_trailing_metadata((("idempotent-replayed", "true"),))
                    return to_order(replayed)

            product = connection.execute(
                "SELECT id, price, stock FROM products WHERE id = ?", (product_id,)).fetchone()
            if product is None:
                connection.rollback()
                reject(context, "product_not_found", grpc.StatusCode.NOT_FOUND, "Product not found.")
            if quantity > product["stock"]:
                connection.rollback()
                reject(context, "insufficient_stock", grpc.StatusCode.FAILED_PRECONDITION,
                       f"Not enough stock available ({product['stock']} left).")

            cursor = connection.execute(
                """INSERT INTO orders (product_id, quantity, unit_price, total_price, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
                (product_id, quantity, product["price"], round(product["price"] * quantity, 2)))
            connection.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (quantity, product_id))
            if key:
                connection.execute(
                    "INSERT INTO idempotency_keys (key, request_hash, order_id) VALUES (?, ?, ?)",
                    (key, request_hash, cursor.lastrowid))
            connection.commit()
            petstore_grpc_orders_created_total.inc()   # new orders only; replays return earlier
            return to_order(connection.execute(ORDER_SELECT + " WHERE orders.id = ?", (cursor.lastrowid,)).fetchone())
        finally:
            connection.close()


def build_server(port=50051, max_workers=10):
    """Create (but do not start) the server. Returns (server, bound_port)."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers),
                         interceptors=[MetricsInterceptor()])
    pb_grpc.add_PetStoreServicer_to_server(PetStoreServicer(), server)
    return server, server.add_insecure_port(f"[::]:{port}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    initialize_database()
    metrics_port = int(os.environ.get("GRPC_METRICS_PORT", "8001"))
    start_http_server(metrics_port)
    server, port = build_server(int(os.environ.get("GRPC_PORT", "50051")))
    server.start()
    log.info("gRPC server listening on :%s, metrics on :%s", port, metrics_port)
    server.wait_for_termination()


if __name__ == "__main__":
    main()