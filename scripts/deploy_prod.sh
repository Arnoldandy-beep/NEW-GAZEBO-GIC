#!/bin/bash
# ===========================================================================
# GAZEBO Investment Club — Production Deploy Script (Ubuntu)
# ===========================================================================
# Usage (on the Ubuntu server, from the app root):
#   chmod +x scripts/deploy_prod.sh
#   sudo ./scripts/deploy_prod.sh
#
# What this script does:
#   1.  Creates a timestamped backup of the production database
#   2.  Creates a timestamped backup of the current code
#   3.  Pulls the latest code from git (main branch)
#   4.  RESTORES the production database (shields it from any git pull)
#   5.  Installs/upgrades Python dependencies
#   6.  Applies schema migrations (safe — only CREATE IF NOT EXISTS)
#   7.  Restarts the application service
#   8.  Prints a verification checklist
#
# User login credentials are NEVER affected:
#   Schema migrations only ADD new tables/columns using CREATE TABLE IF NOT EXISTS
#   and ALTER TABLE ADD COLUMN.  The users table and all passwords remain intact.
#
# Configuration — edit the four variables below before first run:
# ===========================================================================

APP_DIR="/var/www/gazebo_gic"          # Absolute path to the app on the server
SERVICE_NAME="gazebo_gic"              # systemd service name  (gazebo_gic.service)
PYTHON_EXE="$APP_DIR/.venv/bin/python" # Python inside the virtual environment
GIT_BRANCH="main"                      # Branch to deploy

# ---------------------------------------------------------------------------
# DO NOT EDIT BELOW THIS LINE
# ---------------------------------------------------------------------------
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${BOLD}${BLUE}==> $*${NC}"; }

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_FILE="$APP_DIR/data/gazebo_gic.db"
BACKUP_DIR="$APP_DIR/data/backups"
DB_BACKUP="$BACKUP_DIR/gazebo_gic_${TIMESTAMP}.db"
CODE_BACKUP="/tmp/gazebo_code_backup_${TIMESTAMP}.tar.gz"

# ---------------------------------------------------------------------------
step "Pre-flight checks"
# ---------------------------------------------------------------------------
[ -d "$APP_DIR" ]      || error "App directory not found: $APP_DIR"
[ -d "$APP_DIR/.git" ] || error "Not a git repo: $APP_DIR"
[ -f "$DB_FILE" ]      || error "Production DB not found: $DB_FILE"
[ -f "$PYTHON_EXE" ]   || error "Python venv not found: $PYTHON_EXE"
which git >/dev/null   || error "git is not installed"
ok "All pre-flight checks passed"

# ---------------------------------------------------------------------------
step "Step 1 — Backup production database"
# ---------------------------------------------------------------------------
mkdir -p "$BACKUP_DIR"
cp "$DB_FILE" "$DB_BACKUP"
ok "Database backed up → $DB_BACKUP"

# Keep only the 15 most recent DB backups
ls -t "$BACKUP_DIR"/gazebo_gic_*.db 2>/dev/null | tail -n +16 | xargs -r rm --
info "Old backups pruned (kept last 15)"

# ---------------------------------------------------------------------------
step "Step 2 — Backup current code"
# ---------------------------------------------------------------------------
tar -czf "$CODE_BACKUP" \
    --exclude="$APP_DIR/.venv" \
    --exclude="$APP_DIR/data" \
    --exclude="$APP_DIR/static/uploads" \
    --exclude="$APP_DIR/__pycache__" \
    --exclude="$APP_DIR/.git" \
    -C "$(dirname "$APP_DIR")" "$(basename "$APP_DIR")" 2>/dev/null || true
ok "Code snapshot → $CODE_BACKUP"

# ---------------------------------------------------------------------------
step "Step 3 — Pull latest code from git"
# ---------------------------------------------------------------------------
cd "$APP_DIR"
git fetch origin
git checkout "$GIT_BRANCH"
git pull --ff-only origin "$GIT_BRANCH"
ok "git pull completed on branch: $GIT_BRANCH"

# ---------------------------------------------------------------------------
step "Step 4 — RESTORE production database (shields from git pull)"
# ---------------------------------------------------------------------------
# Even if the DB file is still tracked in git, we always restore our backup
# immediately after the pull. This guarantees prod data is NEVER overwritten.
cp "$DB_BACKUP" "$DB_FILE"
ok "Production database restored from backup — credentials intact"

# ---------------------------------------------------------------------------
step "Step 5 — Install / upgrade Python dependencies"
# ---------------------------------------------------------------------------
"$PYTHON_EXE" -m pip install --quiet --upgrade pip
"$PYTHON_EXE" -m pip install --quiet -r "$APP_DIR/requirements.txt"
ok "Python dependencies up to date"

# ---------------------------------------------------------------------------
step "Step 6 — Apply database schema migrations"
# ---------------------------------------------------------------------------
# _apply_migrations() uses only CREATE TABLE IF NOT EXISTS and
# ALTER TABLE ADD COLUMN — it never drops tables or modifies existing rows.
"$PYTHON_EXE" - <<'PYEOF'
import sys, os
sys.path.insert(0, os.environ.get('APP_DIR', '.'))
os.chdir(os.environ.get('APP_DIR', '.'))
from database import init_schema
from config import Config
init_schema(Config.DATABASE_PATH)
print("Schema migrations applied successfully.")
PYEOF
ok "Schema migrations complete"

# ---------------------------------------------------------------------------
step "Step 7 — Restart application service"
# ---------------------------------------------------------------------------
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "Service '$SERVICE_NAME' restarted and is running"
    else
        error "Service '$SERVICE_NAME' failed to start after restart — check: journalctl -u $SERVICE_NAME -n 50"
    fi
else
    warn "Service '$SERVICE_NAME' was not running — attempting to start it..."
    systemctl start "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" && ok "Service started" || \
        error "Service failed to start — check: journalctl -u $SERVICE_NAME -n 50"
fi

# ---------------------------------------------------------------------------
step "Deployment Complete"
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   GAZEBO GIC — Deployment Successful             ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  DB backup  : ${YELLOW}$DB_BACKUP${NC}"
echo -e "  Code backup: ${YELLOW}$CODE_BACKUP${NC}"
echo -e "  Branch     : ${YELLOW}$GIT_BRANCH${NC}"
echo -e "  Deployed at: ${YELLOW}$(date)${NC}"
echo ""
echo -e "${BOLD}Verification checklist:${NC}"
echo -e "  [ ] Open the app in the browser and log in"
echo -e "  [ ] Check the admin dashboard loads correctly"
echo -e "  [ ] Confirm member accounts and passwords work"
echo -e "  [ ] Visit /admin/fees  — confirm reminder buttons appear"
echo -e "  [ ] Visit /admin/fines — confirm reminder buttons appear"
echo -e "  [ ] Visit /admin/offboarding — confirm module loads"
echo -e "  [ ] Check service logs: journalctl -u $SERVICE_NAME -n 30"
echo ""
echo -e "${BOLD}If anything is wrong — rollback in one command:${NC}"
echo -e "  ${RED}cp $DB_BACKUP $DB_FILE && systemctl restart $SERVICE_NAME${NC}"
echo ""
