"""Python caller for the Paws & Cart API (Assignment 2, Part A).

Reliability features, using only the standard library:
  * timeout          every HTTP call gives up after `timeout` seconds
  * limited retry    at most `max_attempts` tries, exponential backoff + jitter,
                     and ONLY for operations that are safe to repeat
  * circuit breaker  after `failure_threshold` failed calls in a row, stop calling
                     the server for `reset_timeout` seconds, then allow one trial

Which operations are retried?
  GET /products, GET /orders          safe by nature                -> retried
  POST /orders                        safe ONLY because we send an
                                      Idempotency-Key               -> retried
  POST /orders/{id}/complete          second call returns 409       -> NOT retried
"""
import json
import random
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


class ServiceUnavailable(Exception):
    """The call failed even after the allowed retries."""


class CircuitOpenError(Exception):
    """The breaker is open: the call was rejected without contacting the server."""


@dataclass
class Response:
    status: int
    body: object
    headers: dict = field(default_factory=dict)


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half-open"

    def __init__(self, failure_threshold=3, reset_timeout=10.0, clock=time.monotonic, log=print):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._log = log
        self.state = self.CLOSED
        self.failures = 0
        self._opened_at = 0.0

    def allow(self):
        if self.state == self.OPEN:
            if self._clock() - self._opened_at >= self.reset_timeout:
                self.state = self.HALF_OPEN          # let exactly one trial call through
                self._log("[breaker] reset timeout over -> HALF-OPEN (one trial call allowed)")
                return True
            return False
        return True

    def record_success(self):
        if self.state != self.CLOSED:
            self._log("[breaker] trial call succeeded -> CLOSED (back to normal)")
        self.state = self.CLOSED
        self.failures = 0

    def record_failure(self):
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state = self.OPEN
            self._opened_at = self._clock()
            self._log(f"[breaker] {self.failures} failed call(s) -> OPEN for {self.reset_timeout}s")


class OrderClient:
    def __init__(self, base_url="http://localhost:5000", timeout=2.0, max_attempts=3,
                 backoff_base=0.5, backoff_cap=4.0, max_retry_after=10.0, breaker=None,
                 sleep=time.sleep, log=print):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_retry_after = max_retry_after
        self.breaker = breaker or CircuitBreaker(log=log)
        self._sleep = sleep
        self._log = log

    # ----------------------------------------------------------- public API
    def list_products(self):
        return self._call("GET", "/products", retry_safe=True)

    def list_orders(self):
        return self._call("GET", "/orders", retry_safe=True)

    def create_order(self, product_id, quantity, idempotency_key=None):
        # One key per logical order, reused by every retry of it.
        key = idempotency_key or str(uuid.uuid4())
        return self._call(
            "POST", "/orders",
            body={"product_id": product_id, "quantity": quantity},
            headers={"Idempotency-Key": key},
            retry_safe=True,
        )

    def complete_order(self, order_id):
        return self._call("POST", f"/orders/{order_id}/complete", retry_safe=False)

    # ----------------------------------------------------------- internals
    def _backoff(self, attempt):
        delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        return delay * random.uniform(0.5, 1.0)          # jitter so clients do not retry in lockstep

    def _call(self, method, path, body=None, headers=None, retry_safe=False):
        if not self.breaker.allow():
            self._log(f"[client] {method} {path} rejected: circuit is OPEN (server not contacted)")
            raise CircuitOpenError(f"circuit open, not calling {path}")

        attempts = self.max_attempts if retry_safe else 1
        last_problem = "unknown"
        for attempt in range(1, attempts + 1):
            wait = None
            try:
                response = self._send(method, path, body, headers)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                last_problem = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
            else:
                if response.status == 429:
                    # The server is healthy, just busy: wait as told, and do not trip the breaker.
                    wait = min(float(response.headers.get("Retry-After", 1)), self.max_retry_after)
                    last_problem = "HTTP 429 (rate limited)"
                    if attempt == attempts:
                        self.breaker.record_success()
                        return response
                elif response.status >= 500:
                    last_problem = f"HTTP {response.status}"
                else:
                    self.breaker.record_success()
                    return response                     # 2xx or a normal 4xx answer

            if attempt < attempts:
                delay = wait if wait is not None else self._backoff(attempt)
                self._log(f"[client] {method} {path} attempt {attempt}/{attempts} failed "
                          f"({last_problem}); retrying in {delay:.2f}s")
                self._sleep(delay)
            else:
                self._log(f"[client] {method} {path} attempt {attempt}/{attempts} failed "
                          f"({last_problem}); giving up")

        self.breaker.record_failure()
        raise ServiceUnavailable(f"{method} {path} failed: {last_problem}")

    def _send(self, method, path, body, headers):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as raw:
                return self._wrap(raw.status, raw.read(), raw.headers)
        except urllib.error.HTTPError as err:           # 4xx / 5xx still carry a body
            return self._wrap(err.code, err.read(), err.headers)

    @staticmethod
    def _wrap(status, raw_body, headers):
        try:
            body = json.loads(raw_body) if raw_body else None
        except ValueError:
            body = raw_body.decode(errors="replace")
        return Response(status, body, dict(headers.items()))