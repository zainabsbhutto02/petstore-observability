"""One application replica = the Flask REST API and the gRPC service in one process.

Assignment 3, Part A: every replica (A, B, C) must expose the same HTTP endpoints and gRPC
methods, so the two A2 servers now run side by side here:

  * HTTP  on :5000  (Flask app from app.py, threaded server)
  * gRPC  on :50051 (PetStore service from grpc_server.py)
  * /metrics on :5000 shows BOTH the HTTP and the gRPC metrics, because they share the
    default prometheus_client registry. Prometheus scrapes each replica there directly.

Both servers use the same SQLite file on the shared Docker volume, so data, stock and
idempotency keys are shared by all replicas. The POST /orders rate limiter is in memory,
so each replica keeps its own counters (see the A3 report).
"""
import logging
import os
import signal

from werkzeug.serving import make_server

import grpc_server
from app import app
from database import initialize_database
from replica_identity import REPLICA_ID


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # werkzeug's own access line is plain text; app.py already logs one JSON event per request.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log = logging.getLogger("petstore-replica")

    initialize_database()
    grpc_port = int(os.environ.get("GRPC_PORT", "50051"))
    http_port = int(os.environ.get("HTTP_PORT", "5000"))
    workers = int(os.environ.get("GRPC_MAX_WORKERS", "20"))

    server, bound = grpc_server.build_server(grpc_port, max_workers=workers)
    server.start()
    http = make_server("0.0.0.0", http_port, app, threaded=True)
    log.info('{"service":"pet-store-replica","replica":"%s","message":"replica started",'
             '"http_port":%d,"grpc_port":%d}', REPLICA_ID, http_port, bound)

    # A3 Part D: "docker stop" sends SIGTERM. Python running as PID 1 would otherwise ignore it and
    # Docker would SIGKILL the replica only after 10 s, so B would keep serving during the failure
    # experiment. Here SIGTERM ends serve_forever() at once; the gRPC health status flips to
    # NOT_SERVING and in-flight gRPC calls get up to 2 s to finish.
    def on_sigterm(signum, _frame):
        log.info('{"service":"pet-store-replica","replica":"%s","message":"SIGTERM received, stopping"}',
                 REPLICA_ID)
        grpc_server.mark_not_serving()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        http.serve_forever()
    finally:
        server.stop(grace=2).wait(3)


if __name__ == "__main__":
    main()
