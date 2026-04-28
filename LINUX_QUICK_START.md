# Ubuntu Deployment Quick Start

## TL;DR - Fastest Path to Production

### First Time Setup (5 minutes)

```bash
# 1. SSH to server
ssh appuser@your_server_ip

# 2. Set up project
cd /home/appuser/gazebo_gic
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 init_db.py

# 3. Make deployment script executable
chmod +x scripts/deploy_from_git.sh

# 4. Enable application as systemd service
sudo nano /etc/systemd/system/gazebo-gic.service
# [Paste content from LINUX_DEPLOYMENT_GUIDE.md - systemd section]
sudo systemctl daemon-reload
sudo systemctl enable gazebo-gic.service
sudo systemctl start gazebo-gic.service
```

### Regular Deployments (2 minutes)

```bash
# From development machine:
cd ~/gazebo_gic
git push origin main
sqlite3 data/gazebo_gic.db ".backup /tmp/snapshot.db"
scp /tmp/snapshot.db appuser@your_server_ip:/tmp/

# On server:
ssh appuser@your_server_ip
cd /home/appuser/gazebo_gic
source venv/bin/activate

# Deploy (choose one):
./scripts/deploy_from_git.sh --branch main                              # Code only
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/snapshot.db   # Code + data merge
```

## Common Commands

| Task | Command |
|------|---------|
| **SSH to server** | `ssh appuser@your_server_ip` |
| **Activate venv** | `source venv/bin/activate` |
| **Check app status** | `sudo systemctl status gazebo-gic` |
| **View logs** | `sudo journalctl -u gazebo-gic.service -f` |
| **Restart app** | `sudo systemctl restart gazebo-gic` |
| **Stop app** | `sudo systemctl stop gazebo-gic` |
| **Start app** | `sudo systemctl start gazebo-gic` |
| **List backups** | `ls -lh backups/` |
| **Restore from backup** | `cp backups/gazebo_gic_backup_YYYYMMDD_HHMMSS.db data/gazebo_gic.db && sudo systemctl restart gazebo-gic` |
| **Deploy script help** | `./scripts/deploy_from_git.sh --help` |
| **Create DB backup** | `sqlite3 data/gazebo_gic.db ".backup /tmp/snapshot.db"` |

## Deployment Script Syntax

```bash
./scripts/deploy_from_git.sh [OPTIONS]

OPTIONS:
  --branch BRANCH           Git branch to deploy (default: main)
  --dev-db PATH            Dev database snapshot for merge (optional)
  --repo-dir DIR           Repository directory (default: current)
  --python EXE             Python executable (default: python3)
  --backup-dir DIR         Backup directory (default: ./backups)
  --help                   Show help
```

## Scenarios

### Scenario 1: First Deployment
```bash
./scripts/deploy_from_git.sh --branch main
```

### Scenario 2: Deploy with New Data
```bash
# Create snapshot first (e.g., 20240428_143022_snapshot.db)
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/20240428_143022_snapshot.db
```

### Scenario 3: Deploy from Staging Branch
```bash
./scripts/deploy_from_git.sh --branch staging
```

### Scenario 4: Emergency Restore
```bash
# List backups
ls -lh backups/

# Restore (replace YYYYMMDD_HHMMSS with actual timestamp)
cp backups/gazebo_gic_backup_YYYYMMDD_HHMMSS.db data/gazebo_gic.db

# Restart
sudo systemctl restart gazebo-gic
```

## Monitoring

### View Live Application Logs
```bash
sudo journalctl -u gazebo-gic.service -f
```

### Check Application Status
```bash
sudo systemctl status gazebo-gic
```

### Verify Database is Running
```bash
sqlite3 data/gazebo_gic.db "SELECT COUNT(*) FROM members;"
```

### Check Server Disk Space
```bash
df -h
```

### List Recent Backups
```bash
ls -lh backups/ | head -20
```

## Environment Variables

Create `.env` file in project root:

```bash
cat > .env << 'EOF'
# Database
DATABASE_PATH=/home/appuser/gazebo_gic/data/gazebo_gic.db

# Flask
FLASK_ENV=production
FLASK_DEBUG=false
APP_URL=https://your-domain.com

# Telegram (if using alerts)
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_ALERTS_ENABLED=true
EOF
```

**Important:** Add `.env` to `.gitignore` so secrets don't get committed:
```bash
echo ".env" >> .gitignore
git add .gitignore
git commit -m "Add .env to gitignore"
```

## Troubleshooting

### App won't start
```bash
# Check logs for errors
sudo journalctl -u gazebo-gic.service -n 100

# Test if app runs manually
python3 app.py

# Check if port is already in use
sudo lsof -i :5000
```

### Deployment script fails
```bash
# Check if script is executable
ls -l scripts/deploy_from_git.sh

# Check Git SSH access
ssh -T git@github.com

# Try manual git pull first
git fetch origin
git pull origin main
```

### Database merge issues
```bash
# Check if both databases exist
ls -l data/gazebo_gic.db /tmp/snapshot.db

# Verify backup was created
ls -l backups/

# Restore from backup if needed
cp backups/gazebo_gic_backup_*.db data/gazebo_gic.db
```

### Permissions error
```bash
# Fix script permissions
chmod +x scripts/deploy_from_git.sh

# Verify
ls -l scripts/deploy_from_git.sh
# Should show: -rwxr-xr-x
```

## File Locations Reference

```
/home/appuser/gazebo_gic/
├── app.py                    # Main Flask app
├── init_db.py                # Database init script
├── requirements.txt          # Python dependencies
├── .env                      # Environment variables (not committed)
├── data/
│   └── gazebo_gic.db        # Production database
├── backups/                  # Backup directory
│   ├── gazebo_gic_backup_20240428_120000.db
│   └── gazebo_gic_backup_20240428_130000.db
├── scripts/
│   ├── deploy_from_git.sh   # Deployment script
│   └── safe_data_merge.py   # Data merge script
├── venv/                     # Virtual environment
└── templates/
    └── [template files]
```

## System Service Commands

```bash
# Check service status
sudo systemctl status gazebo-gic

# Start service
sudo systemctl start gazebo-gic

# Stop service
sudo systemctl stop gazebo-gic

# Restart service
sudo systemctl restart gazebo-gic

# Enable on boot
sudo systemctl enable gazebo-gic

# Disable on boot
sudo systemctl disable gazebo-gic

# View recent logs
sudo journalctl -u gazebo-gic.service -n 50

# Follow logs in real-time
sudo journalctl -u gazebo-gic.service -f

# Clear old logs (older than 7 days)
sudo journalctl --vacuum-time=7d
```

## Backup Management

```bash
# List all backups
ls -lh backups/

# Count backups
ls -1 backups/ | wc -l

# Find oldest backup
ls -t backups/ | tail -1

# Calculate backup size
du -sh backups/

# Clean old backups (keep last 30 days)
find backups/ -name "*.db" -mtime +30 -delete

# Archive backups
tar -czf archive_$(date +%Y%m%d).tar.gz backups/
```

## Git Operations

```bash
# Check current branch
git branch

# Check uncommitted changes
git status

# View last commit
git log -1

# Update all remotes
git fetch origin

# Pull specific branch
git pull origin main

# Check remote URL
git remote -v
```

## Python & Virtual Environment

```bash
# Create virtual environment
python3 -m venv venv

# Activate environment
source venv/bin/activate

# Deactivate environment
deactivate

# Install requirements
pip install -r requirements.txt

# Check Python version
python3 --version

# List installed packages
pip list
```

## Network & Connectivity

```bash
# Test Git SSH access
ssh -T git@github.com

# Check if domain resolves
nslookup your-domain.com

# Test port connectivity
nc -zv your-domain.com 443

# Check listening ports
sudo lsof -i -P -n

# View network stats
netstat -tuln
```

## Database Operations

```bash
# Connect to database
sqlite3 data/gazebo_gic.db

# List all tables
sqlite3 data/gazebo_gic.db ".tables"

# Count records in table
sqlite3 data/gazebo_gic.db "SELECT COUNT(*) FROM members;"

# Backup database
sqlite3 data/gazebo_gic.db ".backup /tmp/backup.db"

# Restore database
sqlite3 data/gazebo_gic.db ".restore /tmp/backup.db"

# Check database integrity
sqlite3 data/gazebo_gic.db "PRAGMA integrity_check;"
```

## Performance Monitoring

```bash
# Monitor CPU and memory
top

# Quick system info
uname -a

# Check disk usage
df -h

# Check memory usage
free -h

# Monitor network traffic
nethogs

# Check process details
ps aux | grep gazebo
ps aux | grep python
```
