# Fixes the Docker Desktop start-up crash on this machine (BLK-001):
#   "listening on unix://...\dockerInference: remove ...: The file cannot be accessed by the system."
# Cause: an unclean Docker exit leaves AF_UNIX socket stubs that Windows cannot delete (error 1920).
# Fix: rename (never delete) the folders that contain ONLY such stubs; Docker recreates them.
#
# Usage (PowerShell, as your normal user, after Docker Desktop has crashed/closed):
#   powershell -ExecutionPolicy Bypass -File scripts\windows\fix-docker-sockets.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\fix-docker-sockets.ps1 -Start
param([switch]$Start)

$ErrorActionPreference = 'Stop'
$running = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(Docker Desktop|com\.docker\.backend)$' }
if ($running) {
    Write-Host "Docker Desktop is still running. Quit it from the tray (or close the error dialog) and re-run."
    exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$targets = @("$env:LOCALAPPDATA\Docker\run", "$env:LOCALAPPDATA\docker-secrets-engine")
foreach ($dir in $targets) {
    if (-not (Test-Path -LiteralPath $dir)) { Write-Host "ok      $dir (absent)"; continue }
    $items = @(Get-ChildItem -LiteralPath $dir -Force)
    $stubs = @($items | Where-Object { -not $_.PSIsContainer -and ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) })
    if ($items.Count -eq 0) { Write-Host "ok      $dir (empty)"; continue }
    if ($stubs.Count -ne $items.Count) {
        Write-Host "SKIP    $dir contains non-socket files; not touching it:"
        $items | ForEach-Object { Write-Host "          $($_.Name)" }
        continue
    }
    $leaf = Split-Path $dir -Leaf
    Rename-Item -LiteralPath $dir -NewName "$leaf.stale-$stamp"
    Write-Host "renamed $dir -> $leaf.stale-$stamp ($($stubs.Count) socket stub(s))"
}

if ($Start) {
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    Write-Host "Docker Desktop started."
}
