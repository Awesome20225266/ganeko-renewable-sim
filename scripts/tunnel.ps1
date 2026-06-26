# Instant public URL for the LOCAL server via a Cloudflare quick tunnel (no account).
# Usage (from the project root, with the app already running on :8000):
#   py -m uvicorn app.main:app --port 8000      # in one terminal
#   powershell -File scripts/tunnel.ps1          # in another
#
# The printed https://<random>.trycloudflare.com URL is live while THIS process and the
# server stay running. The URL changes each time the tunnel restarts.
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$cf = Join-Path $PSScriptRoot "..\tools\cloudflared.exe"
if (-not (Test-Path $cf)) {
  New-Item -ItemType Directory -Force (Split-Path $cf) | Out-Null
  Write-Host "Downloading cloudflared..."
  Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cf
}
# Point at 127.0.0.1 (NOT localhost) to avoid IPv6 (::1) resolution issues.
& $cf tunnel --no-autoupdate --url "http://127.0.0.1:$Port"
