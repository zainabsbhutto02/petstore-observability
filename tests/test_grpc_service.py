"""Tests for the gRPC service and its Prometheus metrics (in-process server, temp database).

The tests generate the Python gRPC code from proto/petstore.proto themselves, using the
same protoc command as README.md, so no generated files need to be committed.
"""
import os
import subprocess
import sys
from pathlib import Path

import grpc
import pytest
from prometheus_client import REGISTRY

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def pb_modules(tmp_path_factory):
    out = tmp_path_factory.mktemp("grpc_gen")
    subprocess.run(
        [sys.executable, "-m", "grpc_tools.protoc", "-I", str(ROOT / "proto"),
         f"--python_out={out}", f"--grpc_python_out={out}", str(ROOT / "proto" / "petstore.proto")],
        check=True)
    os.environ["PETSTORE_GRPC_GEN"] = str(out)
    import grpc_server
    return grpc_server


@pytest.fixture()
def stub(pb_modules, tmp_path, monkeypatch):
    import database

    monkeypatch.setattr(database, "DATA_DIRECTORY", tmp_path)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "grpc.db")
    database.initialize_database()
    server, port = pb_modules.build_server(port=0)
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    yield pb_modules.pb_grpc.PetStoreStub(channel)
    channel.close()
    server.stop(None)


def sample(name, **labels):
    base = {"grpc_service": "petstore.v1.PetStore"}
    return REGISTRY.get_sample_value(name, {**base, **labels}) or 0.0


def calls(method, code):
    return sample("grpc_server_handled_total", grpc_method=method, grpc_code=code)


def test_list_products(stub, pb_modules):
    reply = stub.ListProducts(pb_modules.pb.ListProductsRequest())
    assert len(reply.products) == 6 and reply.products[0].name == "Dog Food"


def test_create_order_ok(stub, pb_modules):
    before = calls("CreateOrder", "OK")
    order = stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=1, quantity=2))
    assert order.status == "pending" and order.total_price == pytest.approx(49.98)
    assert calls("CreateOrder", "OK") == before + 1


@pytest.mark.parametrize("product_id,quantity", [(0, 1), (1, 0), (1, -3)])
def test_invalid_arguments(stub, pb_modules, product_id, quantity):
    with pytest.raises(grpc.RpcError) as err:
        stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=product_id, quantity=quantity))
    assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_unknown_product_and_stock_errors(stub, pb_modules):
    with pytest.raises(grpc.RpcError) as missing:
        stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=999, quantity=1))
    assert missing.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.RpcError) as stock:
        stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=1, quantity=9999))
    assert stock.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_idempotent_replay_and_key_conflict(stub, pb_modules):
    req = pb_modules.pb.CreateOrderRequest(product_id=1, quantity=1, idempotency_key="g-key-1")
    first, call1 = stub.CreateOrder.with_call(req)
    second, call2 = stub.CreateOrder.with_call(req)
    assert first.id == second.id
    assert ("idempotent-replayed", "true") in call2.trailing_metadata()
    assert ("idempotent-replayed", "true") not in call1.trailing_metadata()
    products = {p.id: p.stock for p in stub.ListProducts(pb_modules.pb.ListProductsRequest()).products}
    assert products[1] == 19                                    # 20 - 1, reduced once

    with pytest.raises(grpc.RpcError) as err:
        stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=1, quantity=5, idempotency_key="g-key-1"))
    assert err.value.code() == grpc.StatusCode.ALREADY_EXISTS


def test_metrics_count_codes_and_record_duration(stub, pb_modules):
    ok_before = calls("CreateOrder", "OK")
    nf_before = calls("CreateOrder", "NOT_FOUND")
    dur_before = sample("grpc_server_handling_seconds_count", grpc_method="CreateOrder", grpc_code="NOT_FOUND")
    rej_before = REGISTRY.get_sample_value("petstore_grpc_orders_rejected_total", {"reason": "product_not_found"}) or 0

    stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=2, quantity=1))
    with pytest.raises(grpc.RpcError):
        stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=999, quantity=1))

    assert calls("CreateOrder", "OK") == ok_before + 1
    assert calls("CreateOrder", "NOT_FOUND") == nf_before + 1
    assert sample("grpc_server_handling_seconds_count", grpc_method="CreateOrder", grpc_code="NOT_FOUND") == dur_before + 1
    assert REGISTRY.get_sample_value("petstore_grpc_orders_rejected_total", {"reason": "product_not_found"}) == rej_before + 1
    assert sample("grpc_server_in_flight_requests", grpc_method="CreateOrder") == 0   # back to zero when idle


def test_method_labels_are_bounded(pb_modules):
    assert pb_modules.method_label("/petstore.v1.PetStore/CreateOrder") == "CreateOrder"
    assert pb_modules.method_label("/petstore.v1.PetStore/made-up-id-12345") == "unknown"


def test_grpc_orders_created_counts_new_orders_but_not_replays(stub, pb_modules):
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value("petstore_grpc_orders_created_total") or 0
    req = pb_modules.pb.CreateOrderRequest(product_id=3, quantity=1, idempotency_key="total-key")
    stub.CreateOrder(req)
    stub.CreateOrder(req)                                  # replay: must not count again
    stub.CreateOrder(pb_modules.pb.CreateOrderRequest(product_id=3, quantity=1))
    assert REGISTRY.get_sample_value("petstore_grpc_orders_created_total") == before + 2