<#
Assignment 3, Part D.3 - failure and recovery of replica B under load.

  .\experiments\failure-recovery.ps1 -Interface http
  .\experiments\failure-recovery.ps1 -Interface grpc

What it does (round robin, no delay, active health checks from envoy/part-d-health-checks.yaml):
  1. switches Envoy to part-d-health-checks.yaml (replicas are not touched: --no-deps);
  2. starts the fixed-rate workload in the background (default 20/s for 90 s, no retries);
  3. at -StopAt seconds:  docker stop  petstore-observability-app-2   (replica B);
  4. at -StartAt seconds: docker start petstore-observability-app-2;
  5. polls Envoy's /clusters every 0.5 s the whole time and records when B was marked unhealthy
     (removed from the rotation) and when it was marked healthy again, per cluster;
  6. prints the timeline and the client summary, saves the timeline to results/, and restores
     the normal round-robin config (envoy/envoy.yaml).

Health-check settings (part-d-health-checks.yaml): interval 2 s, timeout 1 s,
unhealthy_threshold 2, healthy_threshold 2.
#>
param(
    [ValidateSet("http", "grpc")][string]$Interface = "http",
    [double]$Rate = 20,
    [int]$Duration = 90,
    [int]$StopAt = 20,
    [int]$StartAt = 50,
    [string]$Replica = "petstore-observability-app-2",
    [string]$ReplicaName = "B"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$cluster = if ($Interface -eq "http") { "http_replicas" } else { "grpc_replicas" }

function Now { [DateTimeOffset]::UtcNow }
function State-Of-Replica {
    # "healthy", "unhealthy (...flags)" or "absent" for the named replica in each cluster
    $result = @{}
    try {
        $json = curl.exe -s -m 1 "http://localhost:9901/clusters?format=json" | ConvertFrom-Json
    } catch { return @{ http_replicas = "admin-unreachable"; grpc_replicas = "admin-unreachable" } }
    foreach ($c in $json.cluster_statuses | Where-Object { $_.name -in "http_replicas", "grpc_replicas" }) {
        $hostStatus = $c.host_statuses | Where-Object { $_.hostname -eq $ReplicaName } | Select-Object -First 1
        if (-not $hostStatus) { $result[$c.name] = "absent"; continue }
        $flags = @()
        foreach ($p in $hostStatus.health_status.PSObject.Properties) {
            if ($p.Value -eq $true) { $flags += $p.Name }
        }
        $result[$c.name] = if ($flags.Count) { "unhealthy (" + ($flags -join ",") + ")" } else { "healthy" }
    }
    return $result
}

Write-Host "== Part D.3 failure/recovery, $Interface, B stopped at +${StopAt}s, started at +${StartAt}s"
$env:ENVOY_CONFIG = "part-d-health-checks.yaml"
docker compose up -d --no-deps envoy 2>&1 | Out-Null
Start-Sleep 6   # let the first health-check rounds pass
$initial = State-Of-Replica
Write-Host "   before: $cluster B = $($initial[$cluster])"

$label = "d3-failure-$Interface"
$out = Join-Path $env:TEMP "a3-$label.out"
$proc = Start-Process -FilePath docker -PassThru -NoNewWindow -RedirectStandardOutput $out -RedirectStandardError "$out.err" `
    -ArgumentList @("compose", "run", "--rm", "--no-deps", "loadgen", $Interface, "--rate", "$Rate", "--duration", "$Duration", "--label", $label)
# docker compose run needs a moment to create the container; take t0 when the client starts sending
$wait = 0
while (-not (Select-String -Path $out -Pattern "open loop" -Quiet -ErrorAction SilentlyContinue) -and $wait -lt 300) { Start-Sleep -Milliseconds 100; $wait++ }
$t0 = Now
Write-Host "   workload started at $($t0.ToString('HH:mm:ss.fff')) UTC"

$events = New-Object System.Collections.Generic.List[object]
function Add-Event($what) { $e = [pscustomobject]@{ t_s = [math]::Round(((Now) - $t0).TotalSeconds, 2); utc = (Now).ToString("HH:mm:ss.fff"); event = $what }; $events.Add($e); Write-Host ("   +{0,6:N2}s  {1}" -f $e.t_s, $what) }

$last = @{}
$stopped = $false; $started = $false
while (-not $proc.HasExited -or ((Now) - $t0).TotalSeconds -lt $Duration) {
    $elapsed = ((Now) - $t0).TotalSeconds
    if (-not $stopped -and $elapsed -ge $StopAt) {
        Add-Event "docker stop $Replica (issued)"; docker stop $Replica | Out-Null; Add-Event "docker stop returned (B is down)"; $stopped = $true
    }
    if (-not $started -and $elapsed -ge $StartAt) {
        Add-Event "docker start $Replica (issued)"; docker start $Replica | Out-Null; Add-Event "docker start returned (B process starting)"; $started = $true
    }
    $state = State-Of-Replica
    foreach ($k in "http_replicas", "grpc_replicas") {
        if ($state[$k] -ne $last[$k]) { Add-Event "Envoy $k : B is $($state[$k])"; $last[$k] = $state[$k] }
    }
    if ($elapsed -gt $Duration + 30) { break }
    Start-Sleep -Milliseconds 500
}
$proc.WaitForExit()

$summaryText = Get-Content $out -Raw
$jsonStart = $summaryText.IndexOf("{"); $jsonEnd = $summaryText.LastIndexOf("}")
$summary = $summaryText.Substring($jsonStart, $jsonEnd - $jsonStart + 1) | ConvertFrom-Json
Write-Host "`n== client summary"
$summary | Select-Object run_id, sent, successes, failures, error_rate_percent | Format-List | Out-String | Write-Host
Write-Host ("   failures by reason: " + ($summary.failures_by_reason | ConvertTo-Json -Compress))
Write-Host ("   replica counts:     " + ($summary.replica_counts | ConvertTo-Json -Compress))
Write-Host ("   client p95 / p99:   {0} / {1} ms" -f $summary.client_latency_ms.p95, $summary.client_latency_ms.p99)

$timeline = [pscustomobject]@{
    run_id = $summary.run_id; interface = $Interface; cluster = $cluster
    health_check = "interval 2s, timeout 1s, unhealthy_threshold 2, healthy_threshold 2"
    workload_started_utc = $t0.ToString("o"); stop_at_s = $StopAt; start_at_s = $StartAt
    events = $events; client_summary = $summary
}
$file = Join-Path $root ("results\{0}-timeline.json" -f $summary.run_id)
$timeline | ConvertTo-Json -Depth 6 | Set-Content $file -Encoding utf8
Write-Host "   saved $([IO.Path]::GetRelativePath($root, $file))"

# Restore normal behaviour: equal round robin without health checks, B running.
Remove-Item Env:ENVOY_CONFIG -ErrorAction SilentlyContinue
docker start $Replica 2>&1 | Out-Null
docker compose up -d --no-deps envoy 2>&1 | Out-Null
Write-Host "== restored envoy/envoy.yaml (round robin); $Replica running"
