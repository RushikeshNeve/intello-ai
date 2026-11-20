# PowerShell wrapper for scripts/ops/backup.sh (needs Git Bash and Docker Desktop).
#   .\scripts\ops\backup.ps1 -Project ukos -Out D:\backups -Keep 7
param(
  [Parameter(Mandatory = $true)][string]$Project,
  [string]$Out = "",
  [int]$Keep = 0
)
$ErrorActionPreference = "Stop"
$bash = (Get-Command bash.exe -ErrorAction SilentlyContinue).Source
if (-not $bash) { $bash = "C:\Program Files\Git\bin\bash.exe" }
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$argsList = @("scripts/ops/backup.sh")
if ($Out) { $argsList += @("--out", $Out) }
if ($Keep -gt 0) { $argsList += @("--keep", $Keep) }
$env:OPS_PROJECT = $Project
if ($Project -eq "ukos") { $env:ALLOW_DEV_STACK = "1" }   # an explicit, named backup of the dev stack
Push-Location $root
try { & $bash @argsList; exit $LASTEXITCODE } finally { Pop-Location }
