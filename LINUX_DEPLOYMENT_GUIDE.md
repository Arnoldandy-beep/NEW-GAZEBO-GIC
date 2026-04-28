# Gazebo GIC Linux Deployment Guide

## Overview
This guide walks you through setting up and deploying the Gazebo Investment Club application on an Ubuntu server using the automated deployment script.

---

## Prerequisites

Before deploying, ensure your Ubuntu server has:

- **Git** installed: `sudo apt-get install git`
- **Python 3.9+**: `python3 --version`
- **pip** (Python package manager): `sudo apt-get install python3-pip`
- **SQLite3** (usually pre-installed): `sqlite3 --version`
- **SSH access** to your server
- **Application repository** cloned on the server
- **Virtual environment** set up (optional but recommended)

---

## Initial Server Setup (One-Time)

### Step 1: SSH into Your Ubuntu Server

```bash
ssh username@your_server_ip
```

### Step 2: Navigate to Your Project Directory

```bash
cd /path/to/gazebo_gic
```

Example for typical deployment:
```bash
cd /home/appuser/gazebo_gic
```

### Step 3: Set Up Virtual Environment (Recommended)

Create and activate a Python virtual environment:

```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt
```

### Step 4: Initialize Database (First Time Only)

Run the schema initialization script:

```bash
python3 init_db.py
```

This creates the SQLite database with all required tables.

### Step 5: Configure Environment Variables

Create a `.env` file in your project root with your server settings:

```bash
cat > .env << 'EOF'
APP_URL=https://your-domain.com
DATABASE_PATH=/path/to/gazebo_gic/data/gazebo_gic.db
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
TELEGRAM_ALERTS_ENABLED=true
FLASK_ENV=production
FLASK_DEBUG=false
EOF
```

Replace values with your actual server configuration.

### Step 6: Set Up Application Service (systemd, PM2, or Docker)

**Option A: Using systemd (Recommended)**

Create a systemd service file:

```bash
sudo nano /etc/systemd/system/gazebo-gic.service
```

Add this content:

```ini
[Unit]
Description=Gazebo Investment Club Application
After=network.target

[Service]
Type=notify
User=appuser
WorkingDirectory=/home/appuser/gazebo_gic
Environment="PATH=/home/appuser/gazebo_gic/venv/bin"
ExecStart=/home/appuser/gazebo_gic/venv/bin/python app.py
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Then enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable gazebo-gic.service
sudo systemctl start gazebo-gic.service
```

Check status:
```bash
sudo systemctl status gazebo-gic.service
```

**Option B: Using PM2 (Node.js Package Manager Alternative)**

```bash
sudo npm install -g pm2
pm2 start "python3 app.py" --name gazebo-gic
pm2 startup
pm2 save
```

---

## Deployment Workflow

### Making the Script Executable

First, make the deployment script executable:

```bash
chmod +x scripts/deploy_from_git.sh
```

### Creating a Dev Database Snapshot

Before each deployment, create a snapshot of your development database:

```bash
# From your development machine or server
sqlite3 data/gazebo_gic.db ".backup /tmp/dev_snapshot_$(date +%Y%m%d_%H%M%S).db"

# Then transfer it to the server if on different machines
scp /tmp/dev_snapshot_*.db appuser@your_server_ip:/tmp/
```

Or on the server directly:
```bash
sqlite3 data/gazebo_gic.db ".backup /tmp/dev_snapshot_$(date +%Y%m%d_%H%M%S).db"
```

### Deployment Scenarios

#### Scenario 1: Deploy Code Only (No Data Merge)

```bash
./scripts/deploy_from_git.sh --branch main
```

This will:
1. Backup the current production database
2. Pull the latest code from Git (main branch)
3. Run database schema migrations
4. Preserve all existing production data

#### Scenario 2: Deploy Code + Merge Missing Dev Data

```bash
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/dev_snapshot.db
```

This will:
1. Backup the current production database
2. Pull the latest code from Git (main branch)
3. Run database schema migrations
4. **Insert only missing records** from dev database (no overwrites)

#### Scenario 3: Deploy from Different Branch

```bash
./scripts/deploy_from_git.sh --branch develop
```

#### Scenario 4: Deploy with Custom Paths

```bash
./scripts/deploy_from_git.sh \
  --branch main \
  --repo-dir /home/appuser/gazebo_gic \
  --python /home/appuser/gazebo_gic/venv/bin/python3 \
  --backup-dir /backups/gazebo-gic \
  --dev-db /tmp/dev_snapshot.db
```

---

## Step-by-Step Deployment Example

Here's a complete example of deploying to your Ubuntu server:

### Step 1: Prepare on Your Development Machine

```bash
# Commit all changes to git
cd ~/projects/gazebo_gic
git add .
git commit -m "Feature: Add password complexity and loan reminders"
git push origin main

# Create dev snapshot
sqlite3 data/gazebo_gic.db ".backup /tmp/dev_snapshot_$(date +%Y%m%d_%H%M%S).db"

# Copy snapshot to server
scp /tmp/dev_snapshot_*.db appuser@192.168.1.100:/tmp/
```

### Step 2: Deploy on Production Server

```bash
# SSH into server
ssh appuser@192.168.1.100

# Navigate to project
cd /home/appuser/gazebo_gic

# Activate virtual environment (if using venv)
source venv/bin/activate

# Run deployment script
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/dev_snapshot_*.db
```

### Step 3: Verify Deployment

```bash
# Check script exit code
echo $?  # Should be 0 for success

# View backup location (shown in script output)
ls -lh backups/

# Check git status
git status

# Verify application is running
sudo systemctl status gazebo-gic  # If using systemd
# or
pm2 status  # If using PM2

# Check application logs
tail -f /var/log/gazebo-gic.log
# or for systemd:
sudo journalctl -u gazebo-gic.service -f

# Verify database
sqlite3 data/gazebo_gic.db "SELECT COUNT(*) FROM members;"
```

### Step 4: If Something Goes Wrong - Restore from Backup

```bash
# Find the backup file
ls -lh backups/

# Restore the backup
cp backups/gazebo_gic_backup_YYYYMMDD_HHMMSS.db data/gazebo_gic.db

# Restart the application
sudo systemctl restart gazebo-gic  # If using systemd
# or
pm2 restart gazebo-gic  # If using PM2
```

---

## Automated Deployment (Optional - Scheduled)

### Set Up a Cron Job for Regular Backups

```bash
# Edit crontab
crontab -e

# Add this line to backup database daily at 2 AM
0 2 * * * sqlite3 /home/appuser/gazebo_gic/data/gazebo_gic.db ".backup /home/appuser/gazebo_gic/backups/daily_backup_$(date +\%Y\%m\%d).db"

# Save with Ctrl+X, then Y, then Enter
```

### Set Up Deployment Hook (After Git Push)

This allows automatic deployment when code is pushed to a specific branch:

1. Create a post-receive hook on the server:

```bash
mkdir -p /path/to/gazebo_gic.git/hooks
nano /path/to/gazebo_gic.git/hooks/post-receive
```

Add this content:

```bash
#!/bin/bash
export GIT_WORK_TREE=/home/appuser/gazebo_gic
export GIT_DIR=/path/to/gazebo_gic.git
cd $GIT_WORK_TREE
git checkout -f main

# Run deployment script
./scripts/deploy_from_git.sh --branch main

# Restart application
sudo systemctl restart gazebo-gic
```

Make it executable:
```bash
chmod +x /path/to/gazebo_gic.git/hooks/post-receive
```

---

## Troubleshooting

### Script Fails with "Permission Denied"

```bash
# Make script executable (if not already done)
chmod +x scripts/deploy_from_git.sh

# Verify permissions
ls -l scripts/deploy_from_git.sh
# Should show: -rwxr-xr-x
```

### Python Module Not Found

```bash
# Activate virtual environment
source venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt
```

### Git Pull Fails

```bash
# Check git configuration
git config --list

# Verify SSH key is set up for GitHub/GitLab
ssh -T git@github.com  # For GitHub
ssh -T git@gitlab.com  # For GitLab

# Check remote URL
git remote -v

# Manually pull to see the error
git pull origin main
```

### Database Merge Produces Unexpected Results

```bash
# Check database file sizes
ls -lh data/gazebo_gic.db /tmp/dev_snapshot.db

# Verify backup was created
ls -lh backups/

# Run merge in dry-run mode (check script output for statistics)
python3 scripts/safe_data_merge.py --prod-db data/gazebo_gic.db --dev-db /tmp/dev_snapshot.db

# If issues occur, restore from backup
cp backups/gazebo_gic_backup_YYYYMMDD_HHMMSS.db data/gazebo_gic.db
```

### Application Won't Start After Deployment

```bash
# Check systemd logs
sudo journalctl -u gazebo-gic.service -n 50

# Check if port is in use
sudo lsof -i :5000  # Or your configured port

# Check virtual environment
source venv/bin/activate
python3 -c "import flask; print(flask.__version__)"

# Test if app.py runs directly
python3 app.py
```

---

## Monitoring and Logging

### View Application Logs

**Using systemd:**
```bash
# Last 50 lines
sudo journalctl -u gazebo-gic.service -n 50

# Follow live logs
sudo journalctl -u gazebo-gic.service -f

# Logs since last restart
sudo journalctl -u gazebo-gic.service --since today
```

**Using PM2:**
```bash
pm2 logs gazebo-gic
```

### Backup Management

```bash
# List all backups
ls -lh backups/

# Keep only last 10 backups (cleanup old ones)
cd backups && ls -t | tail -n +11 | xargs rm -f && cd ..

# Archive old backups
tar -czf backups_archive_$(date +%Y%m).tar.gz backups/*.db
```

---

## Security Best Practices

1. **Keep Git SSH Keys Secure**
   ```bash
   chmod 600 ~/.ssh/id_rsa
   ```

2. **Use Environment Variables** (don't commit secrets)
   ```bash
   # Use .env file (add to .gitignore)
   echo ".env" >> .gitignore
   git add .gitignore
   git commit -m "Add .env to gitignore"
   ```

3. **Restrict Script Permissions**
   ```bash
   chmod 750 scripts/deploy_from_git.sh
   ```

4. **Run Application as Non-Root User**
   ```bash
   sudo useradd -m -s /bin/bash appuser
   sudo chown -R appuser:appuser /home/appuser/gazebo_gic
   ```

5. **Use HTTPS for Backups**
   ```bash
   # When transferring backups over network
   scp -P 22 appuser@server:/path/to/backup.db .
   ```

---

## Quick Reference

```bash
# Make script executable (one-time)
chmod +x scripts/deploy_from_git.sh

# Deploy code only
./scripts/deploy_from_git.sh --branch main

# Deploy code + merge dev data
./scripts/deploy_from_git.sh --branch main --dev-db /tmp/dev_snapshot.db

# View help
./scripts/deploy_from_git.sh --help

# Check deployment status
sudo systemctl status gazebo-gic

# View logs
sudo journalctl -u gazebo-gic.service -f

# Restore from backup
cp backups/gazebo_gic_backup_YYYYMMDD_HHMMSS.db data/gazebo_gic.db
sudo systemctl restart gazebo-gic
```

---

## Need Help?

If you encounter issues:

1. Check the deployment script output for error messages
2. Review logs: `sudo journalctl -u gazebo-gic.service -f`
3. Verify backups exist: `ls -lh backups/`
4. Test database connection: `sqlite3 data/gazebo_gic.db ".tables"`
5. Restore from backup and retry
