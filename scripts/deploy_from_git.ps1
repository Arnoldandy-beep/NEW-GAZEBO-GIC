param(
    [string]$RepoDir = "C:\Users\Administrator\Documents\ProjectAI\NEW_GAZEBO\gazebo_gic",
    [string]$Branch = "main",
    [string]$PythonExe = "C:\Users\Administrator\Documents\ProjectAI\NEW_GAZEBO\gazebo_gic\.venv\Scripts\python.exe",
    [string]$ProdDbPath = "C:\Users\Administrator\Documents\ProjectAI\NEW_GAZEBO\gazebo_gic\data\gazebo_gic.db",
    [string]$DevDbPath = "",
    [string]$BackupDir = "C:\Users\Administrator\Documents\ProjectAI\NEW_GAZEBO\gazebo_gic\data\backups"
)

$ErrorActionPreference = "Stop"

Write-Host "=== Gazebo GIC Safe Deploy ===" -ForegroundColor Cyan
Write-Host "Repo: $RepoDir"
Write-Host "Branch: $Branch"

if (-not (Test-Path $RepoDir)) {
    throw "Repo directory not found: $RepoDir"
}
if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = Join-Path $BackupDir ("gazebo_gic_prod_" + $ts + ".db")

if (Test-Path $ProdDbPath) {
    Copy-Item $ProdDbPath $backupFile -Force
    Write-Host "Backup created: $backupFile" -ForegroundColor Green
} else {
    Write-Host "Warning: production DB not found at $ProdDbPath" -ForegroundColor Yellow
}

Push-Location $RepoDir
try {
    Write-Host "Fetching latest git refs..." -ForegroundColor Cyan
    git fetch origin

    Write-Host "Checking out branch $Branch..." -ForegroundColor Cyan
    git checkout $Branch

    Write-Host "Pulling latest code (fast-forward only)..." -ForegroundColor Cyan
    git pull --ff-only origin $Branch

    if (Test-Path $ProdDbPath) {
        # Protect production data in case DB file is tracked in git.
        Copy-Item $backupFile $ProdDbPath -Force
        Write-Host "Production DB restored from backup after pull." -ForegroundColor Green
    }

    Write-Host "Applying schema migrations..." -ForegroundColor Cyan
    & $PythonExe -c "from database import init_schema; from config import Config; init_schema(Config.DATABASE_PATH); print('Schema migrations applied:', Config.DATABASE_PATH)"

    if ($DevDbPath -and (Test-Path $DevDbPath)) {
        Write-Host "Running insert-only merge from dev snapshot..." -ForegroundColor Cyan
        & $PythonExe scripts/safe_data_merge.py --prod-db $ProdDbPath --dev-db $DevDbPath
    } elseif ($DevDbPath) {
        Write-Host "Dev DB path provided but not found: $DevDbPath" -ForegroundColor Yellow
    } else {
        Write-Host "No dev snapshot provided; skipping data merge." -ForegroundColor Yellow
    }

    Write-Host "Deploy script completed successfully." -ForegroundColor Green
    Write-Host "Next: restart your app service/process (systemd/pm2/supervisor/IIS as applicable)." -ForegroundColor Cyan
}
finally {
    Pop-Location
}
