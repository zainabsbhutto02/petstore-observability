"""Build the Assignment 3 results table from the saved loadgen runs (results/*.json).

    docker compose run --rm --no-deps --entrypoint python loadgen client/summarize_runs.py

For every run it combines three independent sources for the run's own time window:
  * the client summary (rate, sent, errors, client p95, replica seen in x-replica);
  * Prometheus (scraped directly from each replica): server-side p95 and mean / max active
    requests per replica;
  * Envoy's JSON access log in Elasticsearch: requests per replica as Envoy recorded them
    (the cross-check of the distribution against the proxy's own logs).

Writes results/summary.md and prints it.
"""
import glob
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROM = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ES = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
REPLICAS = ("A", "B", "C")

POLICY = {
    "baseline-A-only": ("Envoy -> A only", "none"),
    "rr-equal": ("Round robin, equal weights", "none"),
    "rr-grpc-tcp-proxy": ("Round robin, gRPC as plain TCP (comparison)", "none"),
    "d1-weighted-2-1-1": ("Weighted RR, A:B:C = 2:1:1", "none"),
    "d2-rr-delayB200": ("Round robin, equal weights", "B +200 ms"),
    "d2-leastreq-delayB200": ("Least request (P2C), equal weights", "B +200 ms"),
    "d3-failure-http": ("Round robin + active health checks", "B stopped +20 s, restarted +50 s"),
    "d3-failure-grpc": ("Round robin + active health checks", "B stopped +20 s, restarted +50 s"),
}
TARGET = {
    "baseline-A-only": "100 / 0 / 0", "rr-equal": "33 / 33 / 33", "rr-grpc-tcp-proxy": "33 / 33 / 33",
    "d1-weighted-2-1-1": "50 / 25 / 25", "d2-rr-delayB200": "33 / 33 / 33",
    "d2-leastreq-delayB200": "fewer to B", "d3-failure-http": "33 / 33 / 33 while B is up",
    "d3-failure-grpc": "33 / 33 / 33 while B is up",
}


def prom(query, at):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": at})
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.load(response)["data"]["result"]
    return {r["metric"].get("replica", ""): float(r["value"][1]) for r in data}


def envoy_counts(run_id):
    body = json.dumps({
        "size": 0,
        "query": {"bool": {"filter": [{"term": {"service.keyword": "envoy"}},
                                      {"prefix": {"request_id.keyword": run_id + "-"}}]}},
        "aggs": {"r": {"terms": {"field": "replica.keyword", "missing": "none", "size": 10}}},
    }).encode()
    request = urllib.request.Request(f"{ES}/petstore-final/_search", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
    except OSError:
        return None
    return {b["key"]: b["doc_count"] for b in result["aggregations"]["r"]["buckets"]}


def fmt(values, digits=1, scale=1.0):
    return " / ".join("-" if values.get(r) is None or values.get(r) != values.get(r)
                      else f"{values[r] * scale:.{digits}f}" for r in REPLICAS)


def main():
    rows = []
    for path in sorted(glob.glob(os.path.join(ROOT, "results", "*.json"))):
        if path.endswith("-timeline.json"):
            continue
        with open(path, encoding="utf-8") as handle:
            run = json.load(handle)
        start = datetime.fromisoformat(run["started_utc"])
        end = datetime.fromisoformat(run["ended_utc"])
        window = int((end - start).total_seconds()) + 3
        at = end.timestamp() + 3
        interface = run["interface"]
        if interface == "http":
            sel = 'job="petstore-app", route!~"/metrics|/health"'
            p95 = prom(f"histogram_quantile(0.95, sum by (replica, le) (increase(petstore_http_request_duration_seconds_bucket{{{sel}}}[{window}s])))", at)
            active = prom(f'avg_over_time(petstore_http_requests_in_flight{{job="petstore-app"}}[{window}s])', at)
            active_max = prom(f'max_over_time(petstore_http_requests_in_flight{{job="petstore-app"}}[{window}s])', at)
        else:
            p95 = prom(f'histogram_quantile(0.95, sum by (replica, le) (increase(grpc_server_handling_seconds_bucket{{job="petstore-app"}}[{window}s])))', at)
            active = prom(f'sum by (replica) (avg_over_time(grpc_server_in_flight_requests{{job="petstore-app"}}[{window}s]))', at)
            active_max = prom(f'sum by (replica) (max_over_time(grpc_server_in_flight_requests{{job="petstore-app"}}[{window}s]))', at)
        tcp_proxy = run["target"].endswith(":50052")
        envoy = None if tcp_proxy else envoy_counts(run["run_id"])
        label = run["label"]
        policy, fault = POLICY.get(label, (label, "?"))
        shares = run.get("replica_share_percent", {})
        counts = run.get("replica_counts", {})
        rows.append({
            "run": f"{label} / {interface}" + (" (TCP proxy)" if tcp_proxy else ""),
            "policy": policy, "fault": fault,
            "rate": f"{run['target_rate_per_s']:g} / {run['achieved_send_rate_per_s']:g}",
            "target_share": TARGET.get(label, "?"),
            "shares": " / ".join(f"{shares.get(r, 0):.1f}" for r in REPLICAS),
            "counts": " / ".join(str(counts.get(r, 0)) for r in REPLICAS),
            "envoy": ("TCP: 1 connection log line (to A), no per-call lines" if tcp_proxy
                      else "n/a" if envoy is None else (" / ".join(str(envoy.get(r, 0)) for r in REPLICAS)
                                                 + (f" (+{envoy['none']} no replica)" if envoy.get("none") else ""))),
            "p95": run["client_latency_ms"]["p95"], "p99": run["client_latency_ms"]["p99"],
            "errors": f"{run['error_rate_percent']:.2f} % ({run['failures']})",
            "server_p95": fmt(p95, 1, 1000.0), "active": fmt(active, 2), "active_max": fmt(active_max, 0),
            "late": run.get("late_sends", 0), "window": f"{start:%H:%M:%S}-{end:%H:%M:%S} UTC",
        })

    lines = [
        "# Assignment 3 - results of all runs",
        "",
        "Generated by `client/summarize_runs.py` from `results/*.json`, Prometheus and Envoy's access log",
        "in Elasticsearch, each for the run's own time window. Workload for every run: fixed rate 20/s for",
        "90 s (1800 requests), open loop, timeout 2 s, no client retries, no circuit breaker. HTTP =",
        "GET /products, gRPC = PetStore.ListProducts (one channel). Values per replica are A / B / C.",
        "",
        "## Summary table",
        "",
        "| Run / interface | Policy and fault | Target / actual req/s | A/B/C traffic shares % (target) | Client p95 ms | Error rate | Explanation |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['run']} | {r['policy']}; fault: {r['fault']} | {r['rate']} | {r['shares']} ({r['target_share']}) "
                     f"| {r['p95']:.1f} | {r['errors']} | _see report_ |")
    lines += [
        "",
        "## Detail per run",
        "",
        "| Run / interface | Window | Client A/B/C count | Envoy access log A/B/C | Server p95 ms A/B/C | Mean active A/B/C | Max active A/B/C | Client p99 ms | Late sends |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['run']} | {r['window']} | {r['counts']} | {r['envoy']} | {r['server_p95']} | {r['active']} "
                     f"| {r['active_max']} | {r['p99']:.1f} | {r['late']} |")
    lines += [
        "",
        "Server p95 comes from histogram buckets (lowest bucket 5 ms), so values at or below 5 ms only mean",
        "\"within the first bucket\". Active requests are sampled by Prometheus every 5 s.",
        "",
    ]
    text = "\n".join(lines)
    with open(os.path.join(ROOT, "results", "summary.md"), "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text)


if __name__ == "__main__":
    main()
