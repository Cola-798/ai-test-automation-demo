param(
    [string]$ListenAddress = $(if ($env:DEMO_LISTEN_ADDRESS) { $env:DEMO_LISTEN_ADDRESS } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:DEMO_PORT) { [int]$env:DEMO_PORT } else { 8000 }),
    [switch]$UiOnly
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$runtimeDir = if ($env:DEMO_RUNTIME_DIR) { $env:DEMO_RUNTIME_DIR } else { Join-Path $projectDir "runtime-data" }
$logDir = Join-Path $runtimeDir "logs"
$appiumStatusUrl = "http://127.0.0.1:4723/status"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-AppiumReady {
    try {
        $response = Invoke-RestMethod -Uri $appiumStatusUrl -Method Get -TimeoutSec 2
        return $response.value.ready -eq $true
    }
    catch {
        return $false
    }
}

function Test-PlatformReady {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -Method Get -TimeoutSec 2
        return $response.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Get-LanIPv4Address {
    $privateAddress = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
        Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
        ForEach-Object { $_.IPv4Address.IPAddress } |
        Where-Object {
            $_ -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' -and
            $_ -notlike '169.254.*'
        } |
        Select-Object -First 1

    if ($privateAddress) {
        return $privateAddress
    }
    return "localhost"
}

if (-not $UiOnly -and -not (Test-AppiumReady)) {
    $appiumCommand = Get-Command appium.cmd -ErrorAction SilentlyContinue
    if (-not $appiumCommand) {
        throw "Appium was not found. Install Appium Server and the UiAutomator2 driver first."
    }

    Write-Host "Starting Appium device service..."
    Start-Process `
        -FilePath $appiumCommand.Source `
        -ArgumentList @("--address", "127.0.0.1", "--port", "4723", "--base-path", "/", "--session-override", "--log-level", "info", "--log-no-colors") `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$logDir\appium-server.stdout.log" `
        -RedirectStandardError "$logDir\appium-server.stderr.log"

    $ready = $false
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-AppiumReady) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        throw "Appium failed to start. Check runtime-data\logs\appium-server.stderr.log."
    }
}

if ($ListenAddress -eq "0.0.0.0") {
    $displayHost = Get-LanIPv4Address
    Write-Host "Demo platform (LAN): http://${displayHost}:$Port"
    Write-Host "Run dashboard (LAN): http://${displayHost}:$Port/dashboard"
}
else {
    Write-Host "Demo platform: http://${ListenAddress}:$Port"
    Write-Host "Run dashboard: http://${ListenAddress}:$Port/dashboard"
}
if (Test-PlatformReady) {
    Write-Host "Demo platform is already running."
    exit 0
}
& "$projectDir\.venv\Scripts\python.exe" -m uvicorn app.main:app --host $ListenAddress --port $Port
exit $LASTEXITCODE
