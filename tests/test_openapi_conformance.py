"""Check that the running Flask app behaves exactly as openapi.yaml says.

Run from the project root:
    pip install pytest pyyaml jsonschema openapi-spec-validator
    python -m pytest tests -v

The tests use a temporary SQLite file, so your real data is never touched.
"""
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import validate
from openapi_spec_validator import validate as validate_spec

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEC = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import database

    # Point the app at a throwaway database before it is initialised.
    monkeypatch.setattr(database, "DATA_DIRECTORY", tmp_path)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "test.db")
    import app as app_module

    app_module._rate_hits.clear()          # every test starts with a fresh rate-limit budget
    database.initialize_database()
    app_module.update_pending_orders_gauge()
    return app_module.app.test_client()


def check_schema(data, name):
    validate(data, {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]})


def test_spec_is_valid_openapi():
    validate_spec(SPEC)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    check_schema(r.get_json(), "Health")
    assert "X-Request-ID" in r.headers


def test_products(client):
    r = client.get("/products")
    assert r.status_code == 200
    for item in r.get_json():
        check_schema(item, "Product")


def test_create_order_valid(client):
    r = client.post("/orders", json={"product_id": 1, "quantity": 2})
    assert r.status_code == 201
    check_schema(r.get_json(), "Order")
    assert r.get_json()["status"] == "pending"


@pytest.mark.parametrize(
    "payload",
    [
        {"product_id": 1, "quantity": 0},
        {"product_id": True, "quantity": 1},
        {"product_id": 1},
        {"quantity": 1},
    ],
)
def test_create_order_invalid_input_is_400(client, payload):
    r = client.post("/orders", json=payload)
    assert r.status_code == 400
    check_schema(r.get_json(), "Error")


def test_create_order_non_json_body_is_400(client):
    r = client.post("/orders", data="nope", content_type="text/plain")
    assert r.status_code == 400


def test_create_order_unknown_product_is_404(client):
    r = client.post("/orders", json={"product_id": 999, "quantity": 1})
    assert r.status_code == 404
    check_schema(r.get_json(), "Error")


def test_create_order_insufficient_stock_is_409(client):
    r = client.post("/orders", json={"product_id": 1, "quantity": 9999})
    assert r.status_code == 409
    check_schema(r.get_json(), "InsufficientStock")


def test_list_orders(client):
    client.post("/orders", json={"product_id": 1, "quantity": 1})
    r = client.get("/orders")
    assert r.status_code == 200
    assert len(r.get_json()) >= 1
    for item in r.get_json():
        check_schema(item, "Order")


def test_complete_order_flow(client):
    order_id = client.post("/orders", json={"product_id": 1, "quantity": 1}).get_json()["id"]

    r = client.post(f"/orders/{order_id}/complete")
    assert r.status_code == 200
    assert r.get_json()["status"] == "completed"

    again = client.post(f"/orders/{order_id}/complete")
    assert again.status_code == 409
    check_schema(again.get_json(), "Error")


def test_complete_missing_order_is_404(client):
    r = client.post("/orders/99999/complete")
    assert r.status_code == 404
    check_schema(r.get_json(), "Error")


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"petstore_orders_total" in r.data


# ---------------------------------------------------------------- idempotency
def stock_of(client, product_id):
    return next(p["stock"] for p in client.get("/products").get_json() if p["id"] == product_id)


def test_idempotent_repeat_returns_same_order_and_stock_drops_once(client):
    headers = {"Idempotency-Key": "key-abc-1"}
    before = stock_of(client, 1)

    first = client.post("/orders", json={"product_id": 1, "quantity": 2}, headers=headers)
    second = client.post("/orders", json={"product_id": 1, "quantity": 2}, headers=headers)

    assert first.status_code == 201 and second.status_code == 201
    assert first.get_json() == second.get_json()          # same order id
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert stock_of(client, 1) == before - 2               # reduced once, not twice
    assert len(client.get("/orders").get_json()) == 1      # one order, not two


def test_idempotency_key_reused_with_different_body_is_422(client):
    headers = {"Idempotency-Key": "key-abc-2"}
    client.post("/orders", json={"product_id": 1, "quantity": 1}, headers=headers)
    r = client.post("/orders", json={"product_id": 1, "quantity": 3}, headers=headers)
    assert r.status_code == 422
    check_schema(r.get_json(), "Error")


def test_different_keys_create_different_orders(client):
    a = client.post("/orders", json={"product_id": 1, "quantity": 1}, headers={"Idempotency-Key": "k1"})
    b = client.post("/orders", json={"product_id": 1, "quantity": 1}, headers={"Idempotency-Key": "k2"})
    assert a.get_json()["id"] != b.get_json()["id"]


def test_no_key_still_works_as_before(client):
    a = client.post("/orders", json={"product_id": 1, "quantity": 1})
    b = client.post("/orders", json={"product_id": 1, "quantity": 1})
    assert a.get_json()["id"] != b.get_json()["id"]


def test_overlong_idempotency_key_is_400(client):
    r = client.post("/orders", json={"product_id": 1, "quantity": 1},
                    headers={"Idempotency-Key": "x" * 200})
    assert r.status_code == 400


def test_failed_request_does_not_burn_the_key(client):
    headers = {"Idempotency-Key": "key-retry"}
    bad = client.post("/orders", json={"product_id": 1, "quantity": 9999}, headers=headers)
    assert bad.status_code == 409
    ok = client.post("/orders", json={"product_id": 1, "quantity": 1}, headers=headers)
    assert ok.status_code == 201


# ---------------------------------------------------------------- rate limit
def test_rate_limit_returns_429_with_retry_after(client):
    import app as app_module

    for _ in range(app_module.RATE_LIMIT_MAX):
        assert client.post("/orders", json={"product_id": 3, "quantity": 1}).status_code == 201

    r = client.post("/orders", json={"product_id": 3, "quantity": 1})
    assert r.status_code == 429
    check_schema(r.get_json(), "RateLimited")
    assert 1 <= int(r.headers["Retry-After"]) <= app_module.RATE_LIMIT_WINDOW_SECONDS
    assert r.get_json()["limit"] == app_module.RATE_LIMIT_MAX


def test_rate_limited_request_does_not_create_an_order(client):
    import app as app_module

    for _ in range(app_module.RATE_LIMIT_MAX):
        client.post("/orders", json={"product_id": 3, "quantity": 1})
    client.post("/orders", json={"product_id": 3, "quantity": 1})   # 429
    assert len(client.get("/orders").get_json()) == app_module.RATE_LIMIT_MAX


def test_rate_limit_is_per_client_ip(client):
    import app as app_module

    for _ in range(app_module.RATE_LIMIT_MAX):
        client.post("/orders", json={"product_id": 3, "quantity": 1}, headers={"X-Real-IP": "1.1.1.1"})
    blocked = client.post("/orders", json={"product_id": 3, "quantity": 1}, headers={"X-Real-IP": "1.1.1.1"})
    other = client.post("/orders", json={"product_id": 3, "quantity": 1}, headers={"X-Real-IP": "2.2.2.2"})
    assert blocked.status_code == 429
    assert other.status_code == 201


def test_rate_limit_only_applies_to_post_orders(client):
    import app as app_module

    for _ in range(app_module.RATE_LIMIT_MAX + 1):
        client.post("/orders", json={"product_id": 3, "quantity": 1})
    assert client.get("/orders").status_code == 200
    assert client.get("/products").status_code == 200
    assert client.get("/health").status_code == 200


def test_allowed_again_after_window(client):
    import app as app_module

    ip = "9.9.9.9"
    for _ in range(app_module.RATE_LIMIT_MAX):
        assert app_module.check_rate_limit(ip, now=100.0) == 0
    wait = app_module.check_rate_limit(ip, now=110.0)
    assert wait == app_module.RATE_LIMIT_WINDOW_SECONDS - 10
    assert app_module.check_rate_limit(ip, now=100.0 + app_module.RATE_LIMIT_WINDOW_SECONDS) == 0


def test_pending_gauge_is_refreshed_on_scrape(client):
    """An order written by another process (the gRPC service) must show up in /metrics."""
    import database

    connection = database.get_connection()
    connection.execute(
        "INSERT INTO orders (product_id, quantity, unit_price, total_price, status, created_at) "
        "VALUES (1, 1, 24.99, 24.99, 'pending', CURRENT_TIMESTAMP)")
    connection.commit()
    connection.close()
    body = client.get("/metrics").get_data(as_text=True)
    assert "petstore_pending_orders 1.0" in body