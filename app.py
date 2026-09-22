"""A small, beginner-friendly backend for the Online Pet Store."""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from itertools import count as _count

from flask import Flask, g, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from database import get_connection, initialize_database
from metrics import (
    petstore_http_request_duration_seconds,
    petstore_order_processing_seconds,
    petstore_orders_total,
    update_pending_orders_gauge,
)


class JsonLogFormatter(logging.Formatter):
    """Render application events as one searchable JSON object per line."""

    def format(self, record):
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "service": "pet-store",
            "severity": record.levelname,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        for field in ("method", "route", "status_code", "duration_ms"):
            value = getattr(record, field, None)
            if value is not None:
                event[field] = value
        return json.dumps(event, separators=(",", ":"))


app = Flask(__name__)
log_handler = logging.StreamHandler()
log_handler.setFormatter(JsonLogFormatter())
app.logger.handlers.clear()
app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)
app.logger.propagate = False
initialize_database()
update_pending_orders_gauge()


def normalized_route():
    """Return the Flask route template instead of a value-specific path."""
    if request.url_rule is not None:
        return request.url_rule.rule
    return request.path


# --- Part E.1 anomaly experiment: controlled, reversible delay injection ---
# Off by default. Enable by setting PETSTORE_INJECT_DELAY=true (see docker-compose.yml).
# When enabled, every 5th request sleeps for ~500ms before being handled, to simulate a
# slow dependency. NOTE: the start-time capture happens BEFORE the sleep so the injected
# delay is actually included in the measured duration (an earlier version of this code had
# the timer start AFTER the sleep, which silently hid the delay from the histogram/metrics --
# see the Part E report for that finding).
_INJECT_DELAY = os.environ.get("PETSTORE_INJECT_DELAY", "false").lower() == "true"
_request_counter = _count(1)


@app.before_request
def record_request_duration_start():
    """Set correlation data and store request start time."""
    g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request._petstore_start_time = time.perf_counter()
    if _INJECT_DELAY and next(_request_counter) % 5 == 0:
        time.sleep(0.5)


@app.after_request
def record_request_duration(response):
    """Record metrics and emit one structured event for every HTTP request."""
    route = normalized_route()
    status_code = response.status_code
    duration = time.perf_counter() - request._petstore_start_time
    petstore_http_request_duration_seconds.labels(
        method=request.method,
        route=route,
        status_code=str(status_code),
    ).observe(duration)
    severity = logging.INFO
    if status_code >= 500:
        severity = logging.ERROR
    elif status_code >= 400:
        severity = logging.WARNING
    app.logger.log(
        severity,
        "HTTP request completed",
        extra={
            "request_id": g.request_id,
            "method": request.method,
            "route": route,
            "status_code": status_code,
            "duration_ms": round(duration * 1000, 3),
        },
    )
    response.headers["X-Request-ID"] = g.request_id
    return response


@app.get("/health")
def health():
    """Report whether the Pet Store service is running."""
    return jsonify({"service": "pet-store", "status": "healthy"})


@app.get("/metrics")
def metrics():
    """Expose the application metrics in Prometheus text format."""
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.get("/products")
def get_products():
    """Return all products and their current stock."""
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, name, category, price, stock FROM products ORDER BY id"
        ).fetchall()
        return jsonify([dict(row) for row in rows])
    finally:
        connection.close()


@app.post("/orders")
def create_order():
    """Create a pending order and reduce the selected product's stock."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be valid JSON."}), 400

    product_id = data.get("product_id")
    quantity = data.get("quantity")

    # bool is a kind of int in Python, so reject True and False explicitly.
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        return jsonify({"error": "product_id must be an integer."}), 400

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return jsonify({"error": "quantity must be a positive integer."}), 400

    connection = get_connection()
    start_time = time.perf_counter()
    try:
        # BEGIN IMMEDIATE prevents another order from changing stock until this
        # transaction has either committed or rolled back.
        connection.execute("BEGIN IMMEDIATE")
        product = connection.execute(
            "SELECT id, name, price, stock FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()

        if product is None:
            connection.rollback()
            return jsonify({"error": "Product not found."}), 404

        if quantity > product["stock"]:
            connection.rollback()
            return jsonify(
                {
                    "error": "Not enough stock available.",
                    "available_stock": product["stock"],
                }
            ), 409

        total_price = round(product["price"] * quantity, 2)
        cursor = connection.execute(
            """
            INSERT INTO orders
                (product_id, quantity, unit_price, total_price, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            """,
            (product_id, quantity, product["price"], total_price),
        )
        connection.execute(
            "UPDATE products SET stock = stock - ? WHERE id = ?",
            (quantity, product_id),
        )
        connection.commit()

        order = connection.execute(
            """
            SELECT orders.*, products.name AS product_name
            FROM orders
            JOIN products ON products.id = orders.product_id
            WHERE orders.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        petstore_orders_total.inc()
        update_pending_orders_gauge()
        petstore_order_processing_seconds.observe(time.perf_counter() - start_time)
        return jsonify(dict(order)), 201
    finally:
        connection.close()


@app.get("/orders")
def get_orders():
    """Return all stored orders, including each product's name."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT orders.*, products.name AS product_name
            FROM orders
            JOIN products ON products.id = orders.product_id
            ORDER BY orders.id
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])
    finally:
        connection.close()


@app.post("/orders/<int:order_id>/complete")
def complete_order(order_id):
    """Change an existing pending order to completed."""
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT id, status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()

        if order is None:
            connection.rollback()
            return jsonify({"error": "Order not found."}), 404

        if order["status"] == "completed":
            connection.rollback()
            return jsonify({"error": "Order is already completed."}), 409

        connection.execute(
            "UPDATE orders SET status = 'completed' WHERE id = ?", (order_id,)
        )
        connection.commit()
        update_pending_orders_gauge()

        completed_order = connection.execute(
            """
            SELECT orders.*, products.name AS product_name
            FROM orders
            JOIN products ON products.id = orders.product_id
            WHERE orders.id = ?
            """,
            (order_id,),
        ).fetchone()
        return jsonify(dict(completed_order))
    finally:
        connection.close()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)