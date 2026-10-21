"""Failure tests for the Python caller (timeout, retry, circuit breaker, rate limit).

They use client/fake_server.py, so no real backend is needed.
Run:  python -m pytest tests -v
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from fake_server import FakeServer                                                  # noqa: E402
from order_client import (CircuitBreaker, CircuitOpenError, OrderClient,            # noqa: E402
                          ServiceUnavailable)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture()
def server():
    s = FakeServer(slow_seconds=0.6)
    yield s
    s.close()


def make_client(server, clock=None, **kw):
    sleeps = []
    breaker = CircuitBreaker(3, 10.0, clock=clock or FakeClock(), log=lambda *_: None)
    client = OrderClient(server.url, timeout=0.2, max_attempts=3, breaker=breaker,
                         sleep=sleeps.append, log=lambda *_: None, **kw)
    return client, breaker, sleeps


def test_success_first_try(server):
    client, _, sleeps = make_client(server)
    r = client.create_order(1, 1)
    assert r.status == 201 and sleeps == []


def test_server_error_is_retried_then_succeeds(server):
    client, _, sleeps = make_client(server)
    server.fail_next = 2
    r = client.create_order(1, 1)
    assert r.status == 201
    assert len(sleeps) == 2                      # two waits = two retries
    assert sleeps[1] > 0 and sleeps[1] <= 4.0    # backoff stays within the cap
    assert server.requests == 3


def test_timeout_is_retried_and_order_is_not_duplicated(server):
    client, _, _ = make_client(server)
    server.mode = "slow"                          # slower than the 0.2s timeout
    server.fail_next = 0
    with pytest.raises(ServiceUnavailable):
        client.create_order(1, 1, idempotency_key="same-key")
    # Every attempt carried the same key, so the server holds exactly one order.
    assert len(server.orders) == 1


def test_gives_up_after_max_attempts(server):
    client, _, _ = make_client(server)
    server.mode = "error"
    with pytest.raises(ServiceUnavailable):
        client.list_products()
    assert server.requests == 3                   # exactly max_attempts, not forever


def test_non_idempotent_call_is_never_retried(server):
    client, _, sleeps = make_client(server)
    server.mode = "error"
    with pytest.raises(ServiceUnavailable):
        client.complete_order(1)
    assert server.requests == 1 and sleeps == []


def test_breaker_opens_fails_fast_then_recovers(server):
    clock = FakeClock()
    client, breaker, _ = make_client(server, clock=clock)
    server.mode = "error"
    for _ in range(3):
        with pytest.raises(ServiceUnavailable):
            client.list_products()
    assert breaker.state == CircuitBreaker.OPEN

    seen = server.requests
    with pytest.raises(CircuitOpenError):
        client.list_products()
    assert server.requests == seen                # nothing reached the server

    server.mode = "ok"
    clock.now += 10.0                             # reset timeout passes
    assert client.list_products().status == 200   # half-open trial succeeds
    assert breaker.state == CircuitBreaker.CLOSED


def test_failed_trial_reopens_breaker(server):
    clock = FakeClock()
    client, breaker, _ = make_client(server, clock=clock)
    server.mode = "error"
    for _ in range(3):
        with pytest.raises(ServiceUnavailable):
            client.list_products()
    clock.now += 10.0
    with pytest.raises(ServiceUnavailable):       # trial fails
        client.list_products()
    assert breaker.state == CircuitBreaker.OPEN


def test_rate_limit_429_honours_retry_after_and_does_not_trip_breaker(server):
    client, breaker, sleeps = make_client(server)
    server.mode = "limited"
    r = client.create_order(1, 1)                 # still 429 after all attempts
    assert r.status == 429 and r.headers["Retry-After"] == "1"
    assert sleeps == [1.0, 1.0]                   # waited exactly what the server asked
    assert breaker.state == CircuitBreaker.CLOSED  # a busy server is not a broken server