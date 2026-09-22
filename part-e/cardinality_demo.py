"""
Standalone cardinality-explosion demo for Part E.2.

This does NOT touch the pet-store app or its Prometheus job. It runs its own
tiny metrics server on port 8010, which we point a second, temporary
Prometheus scrape job at. This keeps the experiment fully isolated and easy
to remove afterwards.

Run with the label (BAD_LABEL=true) first, then again without it
(BAD_LABEL unset/false) for the "after removing the label" comparison.
"""
import os
import time
import uuid

from prometheus_client import Counter, start_http_server

BAD_LABEL = os.environ.get("BAD_LABEL", "false").lower() == "true"

if BAD_LABEL:
    # BAD: request_id as a label creates one new time series per unique value.
    demo_requests_total = Counter(
        "demo_requests_total",
        "Demo counter with a high-cardinality label (for the assignment's cardinality experiment)",
        ["request_id"],
    )
else:
    # GOOD: no request_id label. Still counts every request, just as ONE series.
    demo_requests_total = Counter(
        "demo_requests_total",
        "Demo counter without a high-cardinality label",
    )

if __name__ == "__main__":
    start_http_server(8010)
    print(f"Cardinality demo running on :8010 (BAD_LABEL={BAD_LABEL})")
    count = 0
    while count < 100:
        request_id = str(uuid.uuid4())
        if BAD_LABEL:
            demo_requests_total.labels(request_id=request_id).inc()
        else:
            demo_requests_total.inc()
        count += 1
        print(f"  sent request {count}/100 (id={request_id if BAD_LABEL else 'n/a'})")
        time.sleep(0.5)
    print("Done. Leave this running a little longer so Prometheus can scrape the final state.")
    time.sleep(30)