param(
    [ValidateSet("baseline", "anomaly", "recovery")]
    [string]$Scenario = "baseline",

    [ValidateRange(1, 200)]
    [int]$RequestCount = 50,

    [string]$BaseUrl = "http://localhost:5000",

    [ValidateRange(0, 10000)]
    [int]$PauseMilliseconds = 100
)

$ErrorActionPreference = "Stop"
$base = $BaseUrl.TrimEnd("/")

try {
    $health = Invoke-RestMethod -Uri "$base/health" -Method Get -TimeoutSec 10
    if ($health.status -ne "healthy") {
        throw "Backend returned an unexpected health response."
    }
} catch {
    Write-Error "Pet Store backend is unavailable at $base. Start the backend and try again. $($_.Exception.Message)"
    exit 1
}

Write-Output "Scenario: $Scenario"
Write-Output "Endpoint: GET $base/products"
Write-Output "Requests: $RequestCount"

$results = [System.Collections.Generic.List[object]]::new()

for ($number = 1; $number -le $RequestCount; $number++) {
    $requestId = "part-e-$Scenario-$($number.ToString('000'))"
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "$base/products" `
            -Method Get `
            -Headers @{ "X-Request-ID" = $requestId } `
            -TimeoutSec 10
        $statusCode = [int]$response.StatusCode
    } catch {
        $stopwatch.Stop()
        Write-Error "Request $number failed after $([math]::Round($stopwatch.Elapsed.TotalMilliseconds, 3)) ms. $($_.Exception.Message)"
        exit 1
    }

    $stopwatch.Stop()
    $durationMs = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 3)
    $result = [pscustomobject]@{
        Request = $number
        RequestId = $requestId
        Status = $statusCode
        DurationMs = $durationMs
    }
    $results.Add($result)
    Write-Output ("{0}/{1}  status={2}  duration_ms={3}  request_id={4}" -f $number, $RequestCount, $statusCode, $durationMs, $requestId)

    if ($PauseMilliseconds -gt 0 -and $number -lt $RequestCount) {
        Start-Sleep -Milliseconds $PauseMilliseconds
    }
}

$durations = $results.DurationMs | Sort-Object
$average = [math]::Round(($durations | Measure-Object -Average).Average, 3)
$maximum = [math]::Round(($durations | Measure-Object -Maximum).Maximum, 3)
$slowRequests = @($durations | Where-Object { $_ -ge 450 }).Count

Write-Output ""
Write-Output "Completed scenario '$Scenario'."
Write-Output "Successful requests: $($results.Count)/$RequestCount"
Write-Output "Average duration: $average ms"
Write-Output "Maximum duration: $maximum ms"
Write-Output "Requests at or above 450 ms: $slowRequests"
