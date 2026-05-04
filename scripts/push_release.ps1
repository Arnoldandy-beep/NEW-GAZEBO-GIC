# ===========================================================================
# GAZEBO GIC -- Windows Release Push Script
# ===========================================================================
# Run this on your Windows dev/UAT machine to push code changes to git.
# The production server then runs scripts/deploy_prod.sh to pull safely.
#
# Usage:  .\scripts\push_release.ps1
#         .\scripts\push_release.ps1 -Message "your commit message"
# ===========================================================================
param(
    [string]$Message = "",
    [string]$Branch  = "main"
)

# Do NOT set ErrorActionPreference = Stop — git writes harmless warnings to
# stderr which PS5.1 wraps as NativeCommandError and would abort the script.
# Instead we check $LASTEXITCODE after every critical git call.
$ErrorActionPreference = "Continue"

function Write-Step { param($t) Write-Host "`n==> $t" -ForegroundColor Cyan }
function Write-Ok   { param($t) Write-Host "[OK]   $t" -ForegroundColor Green }
function Write-Warn { param($t) Write-Host "[WARN] $t" -ForegroundColor Yellow }
function Write-Fail {
    param($t)
    Write-Host "[ERR]  $t" -ForegroundColor Red
    exit 1
}
function Assert-Git {
    param($label)
    if ($LASTEXITCODE -ne 0) { Write-Fail "git $label failed (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------------------
Write-Step "Pre-flight: verify we are in a git repository"
# ---------------------------------------------------------------------------
if (-not (Test-Path ".git")) { Write-Fail "Not a git repository. Run from the project root." }
Write-Ok "Git repo confirmed"

# ---------------------------------------------------------------------------
Write-Step "Safety check: make sure the database is not tracked by git"
# ---------------------------------------------------------------------------
# @() wraps the output so .Count works correctly even when git returns nothing.
$trackedFiles = @(git ls-files "data/gazebo_gic.db")
if ($trackedFiles.Count -gt 0) {
    Write-Host ""
    Write-Host "  CRITICAL: data/gazebo_gic.db is still tracked by git!" -ForegroundColor Red
    Write-Host "  A git pull on prod WILL overwrite production passwords." -ForegroundColor Red
    Write-Host ""
    $ans = Read-Host "  Press Y to untrack it now and continue, or N to abort"
    if ($ans -eq 'Y' -or $ans -eq 'y') {
        git rm --cached "data/gazebo_gic.db"
        Assert-Git "rm --cached"
        Write-Ok "Database removed from git index (file stays on disk)"
    } else {
        Write-Fail "Aborted. Fix the DB tracking issue before pushing."
    }
} else {
    Write-Ok "Database is not tracked by git -- production data is safe"
}

# ---------------------------------------------------------------------------
Write-Step "Show current changes"
# ---------------------------------------------------------------------------
git status --short

# ---------------------------------------------------------------------------
Write-Step "Stage code changes (never the database or uploads)"
# ---------------------------------------------------------------------------
git add --all
Assert-Git "add --all"

# Silently unstage sensitive files if they slipped in; ignore errors here
# (files that were never staged will simply produce no-op output from git)
foreach ($path in @("data/gazebo_gic.db", ".env", "flask_err.txt")) {
    $inStaging = @(git diff --cached --name-only -- $path)
    if ($inStaging.Count -gt 0) {
        git restore --staged $path
        Write-Warn "Unstaged sensitive file: $path"
    }
}

$staged = @(git diff --cached --name-only)
if ($staged.Count -eq 0) {
    Write-Warn "Nothing to commit -- working tree is clean."
    exit 0
}

Write-Host ""
Write-Host "  Files staged for commit:" -ForegroundColor Cyan
$staged | ForEach-Object { Write-Host "    + $_" -ForegroundColor White }

# ---------------------------------------------------------------------------
Write-Step "Commit"
# ---------------------------------------------------------------------------
if (-not $Message) {
    $Message = Read-Host "`n  Enter commit message (or press Enter for auto-message)"
    if (-not $Message) {
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm"
        $Message = "Release: code update $ts"
    }
}

git commit -m $Message
Assert-Git "commit"
Write-Ok "Committed: $Message"

# ---------------------------------------------------------------------------
Write-Step "Push to origin/$Branch"
# ---------------------------------------------------------------------------
git push origin $Branch
Assert-Git "push"
Write-Ok "Pushed to origin/$Branch"

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  +--------------------------------------------------+" -ForegroundColor Green
Write-Host "  |  Code pushed successfully!                       |" -ForegroundColor Green
Write-Host "  +--------------------------------------------------+" -ForegroundColor Green
Write-Host ""
Write-Host "  Next: SSH into your Ubuntu server and run:" -ForegroundColor Cyan
Write-Host "    cd /var/www/gazebo_gic" -ForegroundColor White
Write-Host "    sudo ./scripts/deploy_prod.sh" -ForegroundColor White
Write-Host ""
Write-Host "  The deploy script will:" -ForegroundColor Cyan
Write-Host "    - Backup the production DB before pulling" -ForegroundColor White
Write-Host "    - Pull the code you just pushed" -ForegroundColor White
Write-Host "    - Restore the production DB immediately after pull" -ForegroundColor White
Write-Host "    - Apply schema migrations (safe, no data loss)" -ForegroundColor White
Write-Host "    - Restart the service automatically" -ForegroundColor White
Write-Host ""
