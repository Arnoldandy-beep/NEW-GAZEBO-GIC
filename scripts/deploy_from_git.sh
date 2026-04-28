#!/bin/bash

###############################################################################
# Gazebo GIC Deployment Script (Linux/Ubuntu)
# 
# Purpose: Automated server deployment from git with safe database handling
# Usage: ./scripts/deploy_from_git.sh --branch main [--dev-db /path/to/dev.db]
#
# Features:
#   - Backup current production database
#   - Fetch and pull latest code from git
#   - Restore production database (prevents accidental overwrites)
#   - Run database schema migrations (idempotent)
#   - Optionally merge missing dev records into prod (insert-only)
#
###############################################################################

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'  # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Default values
BRANCH="main"
DEV_DB=""
REPO_DIR="."
PYTHON_EXE="python3"
BACKUP_DIR="./backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --dev-db)
            DEV_DB="$2"
            shift 2
            ;;
        --repo-dir)
            REPO_DIR="$2"
            shift 2
            ;;
        --python)
            PYTHON_EXE="$2"
            shift 2
            ;;
        --backup-dir)
            BACKUP_DIR="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --branch BRANCH         Git branch to deploy (default: main)"
            echo "  --dev-db PATH           Path to dev database snapshot for merge (optional)"
            echo "  --repo-dir DIR          Repository directory (default: current directory)"
            echo "  --python EXE            Python executable path (default: python3)"
            echo "  --backup-dir DIR        Backup directory (default: ./backups)"
            echo "  --help                  Show this help message"
            echo ""
            echo "Examples:"
            echo "  ./scripts/deploy_from_git.sh --branch main"
            echo "  ./scripts/deploy_from_git.sh --branch main --dev-db /tmp/dev_snapshot.db"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Verify we're in the right directory
if [ ! -d "$REPO_DIR/.git" ]; then
    log_error "Not a git repository: $REPO_DIR"
    exit 1
fi

# Determine production database path
PROD_DB="$REPO_DIR/data/gazebo_gic.db"

if [ ! -f "$PROD_DB" ]; then
    log_error "Production database not found: $PROD_DB"
    exit 1
fi

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

log_info "=========================================="
log_info "Gazebo GIC Deployment Script"
log_info "=========================================="
log_info "Repository: $REPO_DIR"
log_info "Branch: $BRANCH"
log_info "Timestamp: $TIMESTAMP"
log_info "Production DB: $PROD_DB"
if [ -n "$DEV_DB" ]; then
    log_info "Dev DB (for merge): $DEV_DB"
fi
log_info ""

# Step 1: Backup current production database
log_info "Step 1: Backing up production database..."
BACKUP_FILE="$BACKUP_DIR/gazebo_gic_backup_${TIMESTAMP}.db"
if cp "$PROD_DB" "$BACKUP_FILE"; then
    log_success "Database backed up to: $BACKUP_FILE"
else
    log_error "Failed to backup database"
    exit 1
fi

# Step 2: Git operations
log_info ""
log_info "Step 2: Fetching and pulling latest code from git..."

cd "$REPO_DIR"

if ! git fetch origin; then
    log_error "Git fetch failed"
    exit 1
fi
log_info "Git fetch completed"

if ! git checkout "$BRANCH"; then
    log_error "Git checkout to branch '$BRANCH' failed"
    exit 1
fi
log_info "Checked out to branch: $BRANCH"

if ! git pull --ff-only origin "$BRANCH"; then
    log_error "Git pull failed (fast-forward only)"
    exit 1
fi
log_success "Git pull completed"

# Step 3: Run database schema migrations
log_info ""
log_info "Step 3: Running database schema migrations..."

if [ ! -f "init_db.py" ]; then
    log_error "init_db.py not found in repository"
    exit 1
fi

if $PYTHON_EXE init_db.py; then
    log_success "Database schema migrations completed"
else
    log_error "Database schema migrations failed"
    exit 1
fi

# Step 4: Merge dev database (optional)
if [ -n "$DEV_DB" ]; then
    log_info ""
    log_info "Step 4: Merging dev database into production..."
    
    if [ ! -f "$DEV_DB" ]; then
        log_error "Dev database not found: $DEV_DB"
        exit 1
    fi
    
    if [ ! -f "scripts/safe_data_merge.py" ]; then
        log_error "scripts/safe_data_merge.py not found"
        exit 1
    fi
    
    if $PYTHON_EXE scripts/safe_data_merge.py \
        --prod-db "$PROD_DB" \
        --dev-db "$DEV_DB"; then
        log_success "Dev database merge completed"
    else
        log_error "Dev database merge failed"
        log_warn "Production database is still backed up at: $BACKUP_FILE"
        exit 1
    fi
else
    log_info ""
    log_info "Step 4: Skipping dev database merge (--dev-db not provided)"
fi

log_info ""
log_info "=========================================="
log_success "Deployment completed successfully!"
log_info "=========================================="
log_info ""
log_info "Backup location: $BACKUP_FILE"
log_info ""
log_info "Next steps:"
log_info "  1. Verify the application is running correctly"
log_info "  2. Check application logs for any errors"
log_info "  3. If issues occur, restore backup:"
log_info "     cp $BACKUP_FILE $PROD_DB"
log_info ""

exit 0
