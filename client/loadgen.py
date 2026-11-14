"""Fixed-rate (open-loop) test client for Assignment 3.

Sends requests at a FIXED RATE for a FIXED DURATION and does NOT wait for one response before
sending the next: request i is sent at start + i / rate, whatever happened to earlier ones. A
slow replica therefore cannot slow the client down and hide the problem (closed-loop clients do
that, so their latency numbers look better than what users would see).

    # inside the Docker network (recommended; see the loadgen service in docker-compose.yml)
    docker compose run --rm loadgen http --rate 20 --duration 60 --label baseline
    docker compose run --rm loadgen grpc --rate 20 --duration 60 --label baseline

    http  -> GET /products               on http://envoy:5000   (read-only, so stock is not used up)
    grpc  -> PetStore.ListProducts       on envoy:50051, ONE channel = one HTTP/2 connection

No retries and no circuit breaker: every failure is counted exactly once (the brief requires
client retries and circuit breakers to be off during the tests).

At the end it prints a summary (rate, sent, successes, failures by reason, client p50/p95/p99,
replica shares seen in the x-replica response header) and saves it as JSON in results/.
"""
import argparse
import http.client
import json
import math
import os
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def percentile(sorted_values, q):
    """Nearest-rank percentile of an already sorted list (q between 0 and 100)."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(q / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


class Results:
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies_ms = []          # successful requests only
        self.outcomes = Counter()       # "ok", "http_503", "grpc_UNAVAILABLE", "timeout", ...
        self.replicas = Counter()       # from the x-replica header / metadata (successes only)

    def record(self, outcome, latency_ms, replica=None):
        with self.lock:
            self.outcomes[outcome] += 1
            if outcome == "ok":
                self.latencies_ms.append(latency_ms)
                self.replicas[replica or "unknown"] += 1


# --- HTTP: one keep-alive connection per worker thread -------------------------------------
class HttpSender:
    def __init__(self, target, timeout, run_id, workers):
        self.host, _, port = target.partition(":")
        self.port = int(port or 80)
        self.timeout = timeout
        self.run_id = run_id
        self.local = threading.local()
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.futures = []

    def submit(self, number, results):
        """Hand the request to a worker thread and return at once (open loop)."""
        self.futures.append(self.pool.submit(self.send, number, results))

    def finish(self, results, deadline):
        """Wait for outstanding requests, but never past the overall deadline."""
        wait(self.futures, timeout=max(0.0, deadline - time.perf_counter()))
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _connection(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            self.local.conn = conn
        return conn

    def send(self, number, results):
        started = time.perf_counter()
        headers = {"X-Request-ID": f"{self.run_id}-{number:06d}", "Connection": "keep-alive"}
        try:
            conn = self._connection()
            conn.request("GET", "/products", headers=headers)
            response = conn.getresponse()
            response.read()
            elapsed = (time.perf_counter() - started) * 1000
            if response.status == 200:
                results.record("ok", elapsed, response.getheader("x-replica"))
            else:
                results.record(f"http_{response.status}", elapsed)
        except TimeoutError:
            self._reset()
            results.record("timeout", (time.perf_counter() - started) * 1000)
        except (OSError, http.client.HTTPException) as error:
            self._reset()
            results.record(f"error_{type(error).__name__}", (time.perf_counter() - started) * 1000)

    def _reset(self):
        conn = getattr(self.local, "conn", None)
        if conn is not None:
            conn.close()
        self.local.conn = None

    def close(self):
        pass


# --- gRPC: one channel, non-blocking futures ----------------------------------------------
class GrpcSender:
    def __init__(self, target, timeout, run_id):
        import grpc
        sys.path.insert(0, os.environ.get("PETSTORE_GRPC_GEN", os.path.join(ROOT, "grpc_gen")))
        import petstore_pb2 as pb
        import petstore_pb2_grpc as pb_grpc

        self.grpc = grpc
        self.pb = pb
        self.timeout = timeout
        self.run_id = run_id
        self.channel = grpc.insecure_channel(target)     # ONE connection for the whole run
        self.stub = pb_grpc.PetStoreStub(self.channel)
        grpc.channel_ready_future(self.channel).result(timeout=10)
        self.calls = []        # (number, start time, future)
        self.finished_at = {}  # number -> completion time, written by the callback

    def submit(self, number, results):
        """Start the call without waiting for it (open loop)."""
        started = time.perf_counter()
        metadata = (("x-request-id", f"{self.run_id}-{number:06d}"),)
        future = self.stub.ListProducts.future(self.pb.ListProductsRequest(), timeout=self.timeout,
                                               metadata=metadata)
        self.calls.append((number, started, future))
        # The callback runs on gRPC's own thread. It ONLY notes the time: calling back into
        # gRPC there (e.g. initial_metadata()) can deadlock that thread and stall the whole run.
        future.add_done_callback(lambda _call, n=number: self.finished_at.__setitem__(n, time.perf_counter()))

    def finish(self, results, deadline):
        """Wait for the calls (bounded by the overall deadline), then read their results here,
        on the main thread. Calls that have not finished by then are counted as unfinished."""
        while len(self.finished_at) < len(self.calls) and time.perf_counter() < deadline:
            time.sleep(0.05)
        for number, started, future in self.calls:
            ended = self.finished_at.get(number)
            if ended is None:
                future.cancel()
                continue
            elapsed = (ended - started) * 1000
            code = future.code()
            if code == self.grpc.StatusCode.OK:
                replica = dict(future.initial_metadata() or ()).get("x-replica")
                results.record("ok", elapsed, replica)
            elif code == self.grpc.StatusCode.DEADLINE_EXCEEDED:
                results.record("timeout", elapsed)
            else:
                results.record(f"grpc_{code.name}", elapsed)

    def close(self):
        self.channel.close()


def run(args):
    run_id = f"{args.label}-{args.interface}-{uuid.uuid4().hex[:6]}"
    target = args.target or ("envoy:5000" if args.interface == "http" else "envoy:50051")
    if args.interface == "http":
        sender = HttpSender(target, args.timeout, run_id, args.workers)
    else:
        sender = GrpcSender(target, args.timeout, run_id)
    results = Results()
    total = int(round(args.rate * args.duration))
    interval = 1.0 / args.rate
    late = 0

    print(f"[{run_id}] {args.interface} -> {target}: {args.rate}/s for {args.duration}s "
          f"= {total} requests, open loop, timeout {args.timeout}s, no retries", flush=True)
    started_utc = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    for i in range(total):
        due = t0 + i * interval
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        elif delay < -interval:
            late += 1                                   # the client itself fell behind schedule
        sender.submit(i, results)
    send_seconds = time.perf_counter() - t0

    # One overall deadline for the stragglers: every request has its own timeout, so anything
    # still open timeout + 5 s after the last send is reported as unfinished (a failure).
    sender.finish(results, deadline=time.perf_counter() + args.timeout + 5)
    sender.close()
    ended_utc = datetime.now(timezone.utc)

    with results.lock:   # snapshot: late HTTP stragglers must not change the numbers below
        latencies = sorted(results.latencies_ms)
        outcomes = Counter(results.outcomes)
        replicas = Counter(results.replicas)
    ok = outcomes.get("ok", 0)
    unfinished = total - sum(outcomes.values())
    if unfinished:
        outcomes["unfinished"] = unfinished    # no answer within timeout + 5 s: a failure
    failures = total - ok
    summary = {
        "run_id": run_id,
        "label": args.label,
        "interface": args.interface,
        "operation": "GET /products" if args.interface == "http" else "PetStore.ListProducts",
        "target": target,
        "target_rate_per_s": args.rate,
        "duration_s": args.duration,
        # total sends span (total - 1) intervals, so add one interval back for the rate
        "achieved_send_rate_per_s": round(total / (send_seconds + interval), 2),
        "sent": total,
        "successes": ok,
        "failures": failures,
        "error_rate_percent": round(100 * failures / total, 2) if total else 0,
        "failures_by_reason": {k: v for k, v in sorted(outcomes.items()) if k != "ok"},
        "client_latency_ms": {
            "p50": round(percentile(latencies, 50), 2) if latencies else None,
            "p95": round(percentile(latencies, 95), 2) if latencies else None,
            "p99": round(percentile(latencies, 99), 2) if latencies else None,
            "max": round(latencies[-1], 2) if latencies else None,
        },
        "replica_counts": dict(sorted(replicas.items())),
        "replica_share_percent": {k: round(100 * v / ok, 1) for k, v in sorted(replicas.items())} if ok else {},
        "late_sends": late,
        "started_utc": started_utc.isoformat(timespec="seconds"),
        "ended_utc": ended_utc.isoformat(timespec="seconds"),
    }
    print(json.dumps(summary, indent=2), flush=True)

    out_dir = os.path.join(ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{started_utc:%Y%m%dT%H%M%S}-{run_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    print(f"saved {os.path.relpath(path, ROOT)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("interface", choices=["http", "grpc"])
    parser.add_argument("--target", help="host:port (default envoy:5000 for http, envoy:50051 for grpc)")
    parser.add_argument("--rate", type=float, default=20, help="requests per second (default 20)")
    parser.add_argument("--duration", type=float, default=60, help="seconds (default 60)")
    parser.add_argument("--timeout", type=float, default=2.0, help="per-request timeout in seconds (default 2)")
    parser.add_argument("--workers", type=int, default=64, help="HTTP sender threads (default 64)")
    parser.add_argument("--label", default="run", help="name for this run, e.g. baseline, rr, wrr")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
