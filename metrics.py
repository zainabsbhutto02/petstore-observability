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
    buckets=(
        0.01,
        0.025,
        0.05,
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
