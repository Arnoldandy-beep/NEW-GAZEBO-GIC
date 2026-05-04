# GAZEBO GIC — Safe Production Upgrade Guide

> **Rule #1 — Never copy the UAT database to production.**
> Only push code files. The production database is sacred and must never be replaced or overwritten.

**Environments:**
- **UAT / Dev** — Windows (your local machine)
- **Production** — Ubuntu Linux server

Commands are labelled `[WINDOWS]` or `[UBUNTU]` so there is no confusion.

---

## What to Deploy vs. What to Keep

| Item | Deploy to Prod? | Notes |
|------|-----------------|-------|
| `*.py` files | ✅ Yes | All Python source code |
| `templates/` | ✅ Yes | All HTML templates |
| `static/css/`, `static/js/` | ✅ Yes | Styles and scripts |
| `config.py` | ✅ Yes | Review for prod-specific values |
| `requirements.txt` | ✅ Yes | If dependencies changed |
| `data/gazebo_gic.db` | ❌ **NEVER** | Lives on prod, never overwrite |
| `static/uploads/` | ❌ **NEVER** | Prod member photos/files |
| `.env` / secrets | ❌ **NEVER** | Keep prod secrets separate |

---

## Before Every Upgrade — Pre-flight Checklist

```
[ ] 1. Back up the production database (Step 1 below)
[ ] 2. Check which Python files changed in UAT since last deploy
[ ] 3. Check if database.py SCHEMA_SQL changed (new columns / tables)
[ ] 4. Prepare migration SQL if schema changed (Step 3 below)
[ ] 5. Test the migration SQL on a copy of prod DB first
```

---

## Step-by-Step Upgrade Procedure

---

### Step 1 — Backup Production Database First [UBUNTU]

SSH into the prod server and run this **before touching anything else**:

```bash
# Run on Ubuntu prod server — BEFORE every upgrade
cd /path/to/your/app          # e.g. cd /opt/gazebo-gic

timestamp=$(date +"%Y%m%d_%H%M%S")
mkdir -p data/backups
cp data/gazebo_gic.db "data/backups/gazebo_gic_${timestamp}.db"
echo "Backup saved: data/backups/gazebo_gic_${timestamp}.db"
ls -lh data/backups/           # Verify it has a non-zero file size
```

Or use the provided script:
```bash
bash backup_db.sh
```

To restore from a backup if something goes wrong:
```bash
cp data/backups/gazebo_gic_YYYYMMDD_HHMMSS.db data/gazebo_gic.db
```

---

### Step 2 — Check Schema Changes First [WINDOWS — UAT]

Before transferring any files, check what changed in `database.py`:

```powershell
# On Windows UAT machine
git diff HEAD~1 HEAD -- database.py
# Or compare against a tagged release
git diff v1.0 HEAD -- database.py
```

Look for:
- New `CREATE TABLE IF NOT EXISTS` blocks → safe, no action needed
- New **columns** on existing tables → **requires ALTER TABLE migration** (Step 3)
- Changed column types / constraints → requires careful manual migration

---

### Step 3 — Prepare Migration SQL [WINDOWS — UAT]

If new columns were added to existing tables, write a migration script **before** deploying.
Save it in the `migrations/` folder with a date prefix.

**Example — `migrations/20260504_add_bank_details.sql`:**
```sql
-- Migration: Add bank detail columns to members table
-- Date: 2026-05-04
-- SQLite does not support IF NOT EXISTS on ALTER TABLE.
-- Before running, check if column exists:
--   SELECT name FROM pragma_table_info('members') WHERE name='bank_name';
-- Only run the lines for columns that do NOT yet exist.

ALTER TABLE members ADD COLUMN bank_name          TEXT DEFAULT NULL;
ALTER TABLE members ADD COLUMN bank_branch         TEXT DEFAULT NULL;
ALTER TABLE members ADD COLUMN bank_account_name   TEXT DEFAULT NULL;
ALTER TABLE members ADD COLUMN bank_account_no     TEXT DEFAULT NULL;
```

**Test the migration on a copy of the prod DB first [WINDOWS]:**
```powershell
# Copy prod backup to a test file, run migration against it
Copy-Item "data\backups\gazebo_gic_latest.db" "data\test_migration.db"
sqlite3 "data\test_migration.db" ".read migrations\20260504_add_bank_details.sql"
sqlite3 "data\test_migration.db" "PRAGMA table_info(members);"
# Confirm new columns appear — then delete the test file
Remove-Item "data\test_migration.db"
```

---

### Step 4 — Transfer Code Files to Production

**Option A — Git (recommended):**

```bash
# [UBUNTU] On prod server
cd /opt/gazebo-gic
git fetch origin
git diff HEAD origin/main -- database.py   # Review schema diff one more time
git pull origin main
```

> Git will never overwrite `data/gazebo_gic.db` or `static/uploads/` as long as
> those paths are in `.gitignore`. Verify this is the case.

**Option B — Manual transfer via SCP [WINDOWS → UBUNTU]:**

```powershell
# [WINDOWS] From your UAT machine — copy code files only
# Replace user@prod-server and /opt/gazebo-gic with your actual values

$prod = "user@prod-server:/opt/gazebo-gic"

scp admin_routes.py     "${prod}/admin_routes.py"
scp app.py              "${prod}/app.py"
scp auth.py             "${prod}/auth.py"
scp config.py           "${prod}/config.py"
scp database.py         "${prod}/database.py"
scp requirements.txt    "${prod}/requirements.txt"

# Sync templates and static (rsync is safer — only sends changed files)
# Run from WSL or Git Bash if rsync is not available in PowerShell:
rsync -avz --delete templates/  user@prod-server:/opt/gazebo-gic/templates/
rsync -avz --delete static/css/ user@prod-server:/opt/gazebo-gic/static/css/
rsync -avz --delete static/js/  user@prod-server:/opt/gazebo-gic/static/js/
```

**Never include these in any transfer command:**
```
data/               ← prod database
static/uploads/     ← prod member files
.env
```

---

### Step 5 — Run Migration SQL on Prod [UBUNTU]

Only needed if schema changed (new columns). Run **after** code is deployed but
**before** restarting the server:

```bash
# [UBUNTU] On prod server
cd /opt/gazebo-gic

# Check if column already exists before running
sqlite3 data/gazebo_gic.db "SELECT name FROM pragma_table_info('members') WHERE name='bank_name';"

# If it returned nothing, run the migration:
sqlite3 data/gazebo_gic.db < migrations/20260504_add_bank_details.sql

# Verify columns were added
sqlite3 data/gazebo_gic.db "PRAGMA table_info(members);"
```

**SQLite ALTER TABLE rules:**
- You can only ADD a column — not DROP or RENAME via ALTER
- New columns must accept NULL or have a DEFAULT value
- Running the same ALTER twice will error — check first with `pragma_table_info`

---

### Step 6 — Install New Dependencies (if any) [UBUNTU]

Only if `requirements.txt` changed:

```bash
# [UBUNTU]
cd /opt/gazebo-gic
source .venv/bin/activate        # or however your venv is named
pip install -r requirements.txt
```

---

### Step 7 — Restart the Server [UBUNTU]

```bash
# [UBUNTU] — adjust for however your server is managed

# If using systemd service:
sudo systemctl restart gazebo-gic

# If using a process manager like supervisor:
sudo supervisorctl restart gazebo-gic

# If running manually (screen / tmux session):
# Kill the old process, then:
source .venv/bin/activate
python app.py
```

---

### Step 8 — Verify After Restart [UBUNTU]

```bash
# [UBUNTU] Quick smoke test
sqlite3 data/gazebo_gic.db ".tables"
sqlite3 data/gazebo_gic.db "SELECT COUNT(*) FROM users;"
sqlite3 data/gazebo_gic.db "SELECT COUNT(*) FROM members;"

# Check for errors in the Flask log
tail -40 flask_err.txt
```

Then open the app in a browser, log in with a real prod account, and confirm everything works.

---

## Upgrade Checklist (Print and Use Every Time)

```
GAZEBO GIC — Upgrade Checklist                Date: ___________

[WINDOWS — UAT MACHINE]
[ ] git diff checked — schema changes noted: Yes / No
[ ] Migration SQL written and saved in migrations/ folder
[ ] Migration SQL tested on a prod DB copy — passed: Yes / N/A

[UBUNTU — PROD SERVER]
[ ] Backup taken: data/backups/gazebo_gic___________.db
[ ] Backup file size verified (non-zero)
[ ] Code files deployed (NO database file, NO uploads folder)
[ ] New dependencies installed (pip install): Yes / N/A
[ ] Migration SQL run on prod database: Yes / N/A
[ ] Column verification run: PRAGMA table_info(...)

POST-UPGRADE
[ ] Server restarted
[ ] Login tested with a real prod account
[ ] Member count unchanged before/after: _____ members
[ ] No 500 errors in flask_err.txt

Sign-off: ___________________________
```

---

## .gitignore — Protect Prod Data

Make sure your `.gitignore` contains these lines so git never tracks the database
or uploaded files — and `git pull` on prod can never overwrite them:

```
data/gazebo_gic.db
data/backups/
static/uploads/
.env
__pycache__/
*.pyc
.venv/
```

---

## Root Cause of the Password Overwrite

Your passwords were reset because `data/gazebo_gic.db` from UAT was copied to
production, replacing all real member accounts, passwords, and financial records
with UAT test data.

Going forward: **the database file never travels from UAT to production**. Only SQL
migration scripts travel, and only after you have verified them against a backup copy.
