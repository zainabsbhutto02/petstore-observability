"""Prometheus metrics used by the Pet Store backend."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Summary

from database import get_connection


petstore_orders_total = Counter(
    "petstore_orders_total",
    "Total number of successfully created orders.",
)

petstore_pending_orders = Gauge(
    "petstore_pending_orders",
    "Current number of pending orders in SQLite.",
)

petstore_http_request_duration_seconds = Histogram(
    "petstore_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("method", "route", "status_code"),
    # A2: made denser between 10 and 100 ms (observed POST /orders times are about 10-45 ms) so
    # p50/p95 do not collapse into one wide bucket. The 0.5 s .. 10 s edges used by the A1
    # delay experiment are unchanged.
    buckets=(
        0.005,
        0.01,
        0.015,
        0.02,
        0.03,
        0.04,
        0.05,
        0.075,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    ),
)

petstore_order_processing_seconds = Summary(
    "petstore_order_processing_seconds",
    "Processing time for successful order creation requests.",
)


def update_pending_orders_gauge() -> None:
    """Synchronize the gauge to the current number of pending orders in SQLite."""
    connection = get_connection()
    try:
        pending_count = connection.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE status = 'pending'"
        ).fetchone()["count"]
        petstore_pending_orders.set(pending_count)
    finally:
        connection.close()