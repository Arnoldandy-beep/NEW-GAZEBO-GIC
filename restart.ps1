# restart.ps1 — Stop and restart the GIC Flask server
Set-Location $PSScriptRoot

Write-Host "Stopping..." -NoNewline
Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host " done." -ForegroundColor Green

Write-Host "Starting server..." -ForegroundColor Cyan
if (Test-Path ".venv\Scripts\python.exe") {
    & .venv\Scripts\python.exe app.py
} else {
    python app.py
}
