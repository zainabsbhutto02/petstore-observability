"""Work out which replica (A, B, C, ...) this process is (Assignment 3, Part A).

Docker Compose scaling starts identical containers named <project>-<service>-<n>
(petstore-observability-app-1, -2, -3). They get no per-replica environment variables, so each
replica finds its own number by asking Docker's DNS which of those names points at its own IP
address, then maps 1 -> A, 2 -> B, 3 -> C.

Order of precedence:
  1. REPLICA_ID environment variable (useful outside Docker, e.g. REPLICA_ID=local);
  2. the Docker DNS lookup described above, when REPLICA_NAME_PREFIX is set
     (docker-compose.yml sets it to petstore-observability-app);
  3. the container hostname, so a replica is never unlabelled.

The value is a single letter, so it is safe to use in logs, headers and metric labels
(bounded: one value per replica).
"""
import os
import socket
import string
import time

MAX_REPLICAS = 9


def _own_addresses():
    try:
        return set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        return set()


def _lookup_from_docker_dns(prefix, attempts=5, pause=1.0):
    for _ in range(attempts):
        mine = _own_addresses()
        if mine:
            for number in range(1, MAX_REPLICAS + 1):
                try:
                    theirs = set(socket.gethostbyname_ex(f"{prefix}-{number}")[2])
                except OSError:
                    continue
                if mine & theirs:
                    return string.ascii_uppercase[number - 1]
        time.sleep(pause)   # the container's DNS entry can appear a moment after start-up
    return None


def detect_replica_id():
    explicit = os.environ.get("REPLICA_ID")
    if explicit:
        return explicit
    prefix = os.environ.get("REPLICA_NAME_PREFIX")
    if prefix:  # only inside Docker Compose; tests and local runs skip the DNS wait
        found = _lookup_from_docker_dns(prefix)
        if found:
            return found
    return socket.gethostname() or "unknown"


REPLICA_ID = detect_replica_id()


# --- A3 Part D: controlled, reversible slowdown of ONE replica ---------------------------------
# All replicas share one environment (Compose scaling), so the fault names its target:
#   PETSTORE_DELAY_REPLICA=B  PETSTORE_DELAY_MS=200  -> only replica B sleeps 200 ms per request.
# Applied to business requests only (GET /products etc. and the PetStore gRPC methods), never to
# /health, /metrics or the gRPC health service, so a slow replica still passes health checks.
# Empty / 0 (the default) means no delay anywhere.
def _injected_delay_seconds():
    target = os.environ.get("PETSTORE_DELAY_REPLICA", "").strip()
    try:
        delay_ms = float(os.environ.get("PETSTORE_DELAY_MS", "0") or 0)
    except ValueError:
        delay_ms = 0.0
    return delay_ms / 1000.0 if target and target == REPLICA_ID and delay_ms > 0 else 0.0


INJECTED_DELAY_SECONDS = _injected_delay_seconds()
