# GAZEBO GIC — Local ↔ Production Deployment Runbook

**Last updated:** 2026-05-01  
**Maintained by:** Arnold Nsobya (GIC-007 / IT Admin)  
**Repository:** https://github.com/Arnoldandy-beep/NEW-GAZEBO-GIC  
**Production path:** `/home/appuser/gazebo_gic`  
**Local path:** `c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC`

---

## Overview of the Two Directions

| Direction | When to use | What moves |
|-----------|-------------|-----------|
| **Prod → Local** | Refresh local with live data | DB backup + uploads tar |
| **Local → Prod** | Ship new code/data to production | Git push + optional DB snapshot |

---

## Decision Tree — What Are You Pushing?

Before starting, decide your scenario. Each has a slightly different path.

```
Are you pushing code changes (Python, templates, JS, config)?
  YES → do Phase 1 + Phase 3  (git push + deploy script)
  NO  → skip to Phase 4

Are you also pushing NEW DATA from local to prod?
  YES — new records only (e.g. test data, settings)
    → Use --dev-db flag in deploy script  (safe INSERT-only merge)
  YES — full local DB should replace prod (e.g. you fixed data locally)
    → Do Phase 4B  (full DB swap — destructive, needs extra care)
  NO  → skip Phase 4

Do you have new uploaded files (photos, attachments, PDFs)?
  YES → do Phase 5  (uploads sync)
  NO  → skip Phase 5
```

---

## PART A — LOCAL → PROD

---

### Phase 0 — Pre-Deploy Checklist (Local Machine)

Run through this before touching prod. Never deploy broken code.

**1. Confirm the local app starts and works:**
```powershell
python app.py
# Visit http://127.0.0.1:8080
# Click through: members, savings, loans, minutes, fines, fees — all loading?
```

**2. Check for uncommitted changes:**
```powershell
git status
git diff
```

**3. Confirm requirements.txt is up to date if you added new packages:**
```powershell
pip freeze | Select-String "flask|werkzeug|dateutil|openpyxl|gunicorn|requests"
# If you added a new package, update requirements.txt before pushing
```

**4. Note your current git commit — your rollback target if needed:**
```powershell
git log --oneline -5
# Write down the top commit hash (e.g. e60a76f)
```

---

### Phase 1 — Commit and Push Code to GitHub

```powershell
cd "c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC"

# Stage your changes — be specific, never use git add . blindly
git add app.py auth.py admin_routes.py member_routes.py utils.py config.py database.py
git add templates/           # if templates changed
git add static/css/ static/js/   # if CSS/JS changed
git add requirements.txt     # if dependencies changed

# DO NOT add: data/*.db  static/uploads/  .env

# Confirm what you are about to commit
git status
git diff --staged

# Commit
git commit -m "Brief description of what changed and why"

# Push to GitHub
git push origin main

# Confirm it landed
git log --oneline origin/main -3
```

> **Never commit:** `data/gazebo_gic.db`, `static/uploads/`, `.env`, `__pycache__/`
> These are already in `.gitignore` — keep it that way.

---

### Phase 2 — Take a Safe Prod Backup (SSH into Server)

**Always do this before any deployment. No exceptions.**

```bash
# SSH into your prod server
ssh appuser@YOUR_SERVER_IP

# Navigate to the project
cd /home/appuser/gazebo_gic

# Take a safe online backup (works even while app is running)
sqlite3 data/gazebo_gic.db ".backup 'backups/pre_deploy_$(date +%Y%m%d_%H%M%S).db'"

# Verify the backup is a real file (should be 200 KB+, NOT 4 KB)
ls -lh backups/pre_deploy_*.db

# Quick sanity check — confirm tables exist in the backup
sqlite3 backups/pre_deploy_*.db "SELECT name FROM sqlite_master WHERE type='table';" | head -10
```

> Write down the backup filename — you will need it if you rollback.
> Example: `backups/pre_deploy_20260501_140000.db`

---

### Phase 3 — Deploy Code to Prod (Run the Deploy Script)

Still on the SSH session on the prod server:

```bash
cd /home/appuser/gazebo_gic

# Activate virtual environment
source venv/bin/activate

# Make the script executable if not already done
chmod +x scripts/deploy_from_git.sh

# SCENARIO A: Code changes only (most common)
./scripts/deploy_from_git.sh --branch main

# SCENARIO B: Code changes + merge new data from local (INSERT-only, safe)
# Only do this if you also ran Phase 4A to upload a local db snapshot first
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/local_snapshot.db
```

**What the script does internally:**
1. Backs up prod DB again (double backup — good)
2. `git fetch` + `git pull --ff-only origin main` (gets your Phase 1 push)
3. Applies schema migrations (idempotent `ALTER TABLE` additions)
4. Optionally merges local snapshot (INSERT-only — never overwrites prod records)

> If the script fails mid-way it stops immediately (`set -euo pipefail`).
> Your prod DB is untouched — the Phase 2 backup is your safety net.

---

### Phase 4A — Push New Local Data to Prod (INSERT-only merge — Safe)

Use this when you have added new records locally (new settings, members, config data)
and want them on prod **without touching any existing prod records**.

**On your local Windows machine — create a snapshot:**
```powershell
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$snap = "C:\temp\local_snapshot_$ts.db"
& sqlite3 "data\gazebo_gic.db" ".backup '$snap'"
Write-Host "Snapshot: $snap"
Write-Host "Size: $([math]::Round((Get-Item $snap).Length/1KB, 1)) KB"
```

**Upload to prod server via WinSCP:**
- Connect to your prod server in WinSCP
- Upload `C:\temp\local_snapshot_YYYYMMDD_HHMMSS.db` to `/tmp/` on the server

**Then include it in the deploy command (back on SSH):**
```bash
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/local_snapshot_YYYYMMDD_HHMMSS.db
```

> `safe_data_merge.py` is **INSERT-ONLY**.
> It will never update or delete any existing prod row.
> It only adds rows whose primary key does not already exist in prod.
> Safe to run multiple times.

---

### Phase 4B — Full Local DB Overwrite on Prod (Destructive — Extra Care Required)

Use this **only** when your local DB should completely replace prod — for example,
you did a major data cleanup or restructure locally. This wipes all prod-only changes
made since you last pulled from prod.

**On prod server — stop the app first:**
```bash
sudo systemctl stop gazebo-gic

# Verify it is stopped
sudo systemctl status gazebo-gic    # Should say: inactive (dead)

# Verify port is free
ss -tlnp | grep 8080                # Should return nothing
```

**On your local Windows machine — create and upload the snapshot:**
```powershell
$snap = "C:\temp\gic-refresh\local_full_$(Get-Date -Format 'yyyyMMdd_HHmmss').db"
& sqlite3 "data\gazebo_gic.db" ".backup '$snap'"
Write-Host "Snapshot: $snap  Size: $([math]::Round((Get-Item $snap).Length/1KB,1)) KB"
```

- Upload via WinSCP to `/tmp/` on the prod server

**Back on SSH:**
```bash
# Safety backup one more time
cp data/gazebo_gic.db backups/pre_fullswap_$(date +%Y%m%d_%H%M%S).db

# Replace prod db with your local snapshot
cp /tmp/local_full_YYYYMMDD_HHMMSS.db data/gazebo_gic.db

# Apply any schema migrations (idempotent — always safe to run)
python3 -c "
import sys; sys.path.insert(0, '.')
from database import init_schema
init_schema('data/gazebo_gic.db')
print('Migrations applied OK')
"

# Restart
sudo systemctl start gazebo-gic
sudo systemctl status gazebo-gic
```

---

### Phase 5 — Sync New Uploads to Prod (Photos, PDFs, Attachments)

Only do this if you added new files locally (member photos, expense receipts,
minutes PDFs, loan applications).

**On your local Windows machine — package the uploads:**
```powershell
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$tar = "C:\temp\uploads_local_$ts.tar.gz"
tar -czf $tar -C "c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC\static" uploads/
Write-Host "Packaged: $tar  Size: $([math]::Round((Get-Item $tar).Length/1MB, 2)) MB"
```

**Upload via WinSCP** to `/tmp/` on the prod server.

**On SSH:**
```bash
# Extract — merges into existing uploads, never deletes existing files
tar -xzf /tmp/uploads_local_YYYYMMDD_HHMMSS.tar.gz \
    -C /home/appuser/gazebo_gic/static/ \
    --keep-old-files    # skip files that already exist on prod

# Verify total file count
find /home/appuser/gazebo_gic/static/uploads -type f | wc -l

# Fix ownership — app must be able to read these
sudo chown -R appuser:appuser /home/appuser/gazebo_gic/static/uploads/
```

---

### Phase 6 — Restart the Service and Verify

```bash
# Restart (or start if it was stopped in Phase 4B)
sudo systemctl restart gazebo-gic

# Watch the first 10 seconds of startup
sudo journalctl -u gazebo-gic.service -f --since "1 minute ago"
# Press Ctrl+C once you see "Running on http://..."

# Confirm it is running
sudo systemctl status gazebo-gic
# Should say: Active: active (running)

# Hit the app to confirm it responds
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/
# Should print: 200
```

**Full data sanity check on prod:**
```bash
sqlite3 data/gazebo_gic.db << 'EOF'
SELECT 'members'    , COUNT(*) FROM members;
SELECT 'savings'    , COUNT(*) FROM savings;
SELECT 'loans'      , COUNT(*) FROM loans;
SELECT 'minutes'    , COUNT(*) FROM minutes;
SELECT 'fines'      , COUNT(*) FROM fines;
SELECT 'annual_fees', COUNT(*) FROM annual_fees;
SELECT 'users'      , COUNT(*) FROM users;
SELECT 'expenses'   , COUNT(*) FROM expenses;
EOF
```

**Confirm running commit matches what you pushed:**
```bash
git log --oneline -3
# Top commit should match what you pushed in Phase 1
```

---

### Phase 7 — Rollback (If Anything Goes Wrong)

**Code rollback — revert to previous git commit:**
```bash
# On prod server
git log --oneline -5          # find the last good commit hash
git checkout COMMIT_HASH      # e.g. git checkout e60a76f
sudo systemctl restart gazebo-gic
```

**Database rollback — restore the Phase 2 backup:**
```bash
sudo systemctl stop gazebo-gic

# List available backups
ls -lht backups/ | head -10

# Restore — replace TIMESTAMP with actual filename from Phase 2
cp backups/pre_deploy_YYYYMMDD_HHMMSS.db data/gazebo_gic.db

sudo systemctl start gazebo-gic
sudo systemctl status gazebo-gic

# Confirm data is back
sqlite3 data/gazebo_gic.db "SELECT COUNT(*) FROM members;"
```

---

## PART B — PROD → LOCAL (Refresh Local with Live Prod Data)

---

### Step 1 — Take a Safe Backup on Prod (SSH)

```bash
ssh appuser@YOUR_SERVER_IP
cd /home/appuser/gazebo_gic

# Safe online DB backup
sqlite3 data/gazebo_gic.db ".backup '/tmp/gazebo_backup_$(date +%Y%m%d_%H%M%S).db'"

# Verify it is real (must be 200 KB+)
ls -lh /tmp/gazebo_backup_*.db

# Package uploads
tar -czf /tmp/uploads_$(date +%Y%m%d_%H%M%S).tar.gz static/uploads/
ls -lh /tmp/uploads_*.tar.gz
```

> **Do NOT use plain `cp` on the live db file.**
> A plain copy during a write can produce a corrupt or empty file (4 KB blank).
> Always use `sqlite3 .backup` — it uses SQLite's hot-backup API.

---

### Step 2 — Download Both Files via WinSCP

1. Connect to your prod server in WinSCP
2. Navigate to `/tmp/`
3. Download:
   - `gazebo_backup_YYYYMMDD_HHMMSS.db` → `C:\temp\gic-refresh\`
   - `uploads_YYYYMMDD_HHMMSS.tar.gz`   → `C:\temp\gic-refresh\`

---

### Step 3 — Stop Local App

```powershell
# Find and kill the process on port 8080
$pid = (netstat -ano | Select-String ":8080").ToString().Trim().Split()[-1]
Stop-Process -Id $pid -Force
Start-Sleep -Seconds 1

# Confirm port is free
netstat -ano | Select-String ":8080"   # Should return nothing
```

---

### Step 4 — Restore Prod DB Locally

```powershell
$localDb  = "c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC\data\gazebo_gic.db"
$prodSnap = "C:\temp\gic-refresh\gazebo_backup_YYYYMMDD_HHMMSS.db"   # update filename
$ts       = Get-Date -Format "yyyyMMdd_HHmmss"
$localBak = "c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC\data\gazebo_gic_local_backup_$ts.db"

# Back up current local db before overwriting
Copy-Item $localDb $localBak
Write-Host "Local backup: $localBak"

# Copy prod db into place
Copy-Item $prodSnap $localDb -Force
Write-Host "Prod db restored: $([math]::Round((Get-Item $localDb).Length/1KB,1)) KB"
```

---

### Step 5 — Extract Prod Uploads Locally

```powershell
$uploadDest = "c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC\static\uploads"
$tar        = "C:\temp\gic-refresh\uploads_YYYYMMDD_HHMMSS.tar.gz"   # update filename

tar -xzf $tar -C $uploadDest --strip-components=1
$count = (Get-ChildItem $uploadDest -Recurse -File).Count
Write-Host "Upload files present: $count"
```

---

### Step 6 — Apply Schema Migrations and Restart

```powershell
# Apply migrations (safe on any db)
$script = @'
import sys
sys.path.insert(0, r"c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC")
from database import init_schema
from config import Config
init_schema(Config.DATABASE_PATH)
print("Migrations applied OK")
'@
$script | python

# Start the app
cd "c:\Users\Administrator\Documents\ProjectAI\MasterPiece-UAT\NEW-GAZEBO-GIC"
Start-Process python -ArgumentList "app.py" -WindowStyle Hidden
Start-Sleep -Seconds 3

# Verify it is up
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080" -UseBasicParsing -TimeoutSec 5
    Write-Host "HTTP $($r.StatusCode) - Server is up"
} catch {
    Write-Host "ERROR: $_"
}

# Open in browser
Start-Process "http://127.0.0.1:8080"
```

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────┐
│               LOCAL → PROD  (ship your changes)                │
├─────────────────────────────────────────────────────────────────┤
│ LOCAL (Windows)                                                 │
│  0. Test app locally — verify all modules work                  │
│  1. git add [files] → git commit → git push origin main        │
│  2. (data) sqlite3 snapshot → upload via WinSCP to /tmp/       │
│  3. (uploads) tar uploads/ → upload via WinSCP to /tmp/        │
├─────────────────────────────────────────────────────────────────┤
│ PROD (SSH)                                                      │
│  4. sqlite3 db ".backup backups/pre_deploy_$(date…).db"        │
│  5. ./scripts/deploy_from_git.sh --branch main [--dev-db …]   │
│  6. (full swap only) systemctl stop → cp snapshot → migrations  │
│  7. (uploads) tar -xzf uploads.tar.gz --keep-old-files         │
│  8. sudo systemctl restart gazebo-gic                          │
│  9. journalctl -u gazebo-gic.service -f   (watch logs)        │
│ 10. sqlite3 db "SELECT COUNT(*) FROM members;"                  │
├─────────────────────────────────────────────────────────────────┤
│ ROLLBACK                                                        │
│  Code: git checkout LAST_GOOD_HASH → systemctl restart         │
│  Data: cp backups/pre_deploy_*.db data/gazebo_gic.db           │
│        → systemctl restart                                     │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│               PROD → LOCAL  (refresh local with live data)     │
├─────────────────────────────────────────────────────────────────┤
│ PROD (SSH)                                                      │
│  1. sqlite3 db ".backup /tmp/gazebo_backup_$(date…).db"        │
│  2. tar -czf /tmp/uploads_$(date…).tar.gz static/uploads/      │
│  3. Download both via WinSCP to C:\temp\gic-refresh\           │
├─────────────────────────────────────────────────────────────────┤
│ LOCAL (Windows)                                                 │
│  4. Kill port 8080 process                                      │
│  5. Backup local db → copy prod snapshot into data/            │
│  6. tar -xzf uploads.tar.gz into static/                       │
│  7. python init_schema → python app.py                         │
│  8. Verify at http://127.0.0.1:8080                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Important Notes

### On the Deploy Script and Migrations
The deploy script (`scripts/deploy_from_git.sh`) calls `python3 init_db.py` for
migrations. The `init_db.py` seed function only inserts data when the database does
not exist — it is safe on an existing prod DB. The real migration engine is
`database.py → init_schema() → _apply_migrations()` which uses
`CREATE TABLE IF NOT EXISTS` and `ALTER TABLE`, so it is always idempotent.

**If you add a new column or table:** put it in `database.py → _apply_migrations()`
and both the deploy script and `app.py` startup will pick it up automatically on
the next deployment — no manual SQL needed on the server.

### Why Not `cp` the Live DB?
A plain file copy on a live SQLite database can capture it mid-write, producing
a corrupt or empty (4 KB) file. Always use `sqlite3 .backup` — it uses SQLite's
own hot-backup API and is always consistent regardless of active writes.

### safe_data_merge.py is INSERT-ONLY
The `--dev-db` merge path will never update or delete any existing prod row.
It only inserts rows whose primary key does not already exist in prod.
You can safely run it multiple times without risk of data loss.

### Uploads: Always Use --keep-old-files When Extracting to Prod
This prevents overwriting a file that was uploaded directly on prod (e.g. a member
uploaded their own photo via the portal) with an older version from your local machine.
