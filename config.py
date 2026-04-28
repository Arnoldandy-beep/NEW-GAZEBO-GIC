"""
GAZEBO Investment Club - Configuration
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY', 'gazebo-gic-change-this-in-production-2026')
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    SESSION_REFRESH_EACH_REQUEST = False
    IDLE_SESSION_TIMEOUT_SECONDS = 60 * 3  # 3 minutes of inactivity

    # Alert integrations
    TELEGRAM_ALERTS_ENABLED = os.environ.get("TELEGRAM_ALERTS_ENABLED", "1") == "1"
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
    APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8080")

    # Database
    #DATABASE_PATH = str(BASE_DIR / 'data' / 'gazebo_gic.db')
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "data" / "gazebo_gic.db"))

    # Club Settings
    CLUB_NAME = 'GAZEBO Investment Club'
    CLUB_SHORT_NAME = 'GAZEBO GIC'
    CLUB_LOCATION = 'Kampala, Uganda'
    CURRENCY = 'UGX'
    CURRENCY_SYMBOL = 'USh'
    ESTABLISHED_DATE = '2025-08-01'  # August 2025
    SAVINGS_START_PERIOD = '2025-08'  # First savings month

    # Financial Rules
    MONTHLY_SAVINGS_AMOUNT = 100_000          # Mandatory monthly savings (UGX)
    LOAN_INTEREST_RATE_MONTHLY = 0.05         # 5% per month
    LOAN_MAX_TERM_MONTHS = 6                  # Max 6 months
    LOAN_PENALTY_RATE_MONTHLY = 0.05          # 5% penalty per overdue month
    GUARANTORS_REQUIRED = 2                   # Number of guarantors
    TOP_SAVER_AWARD = 50_000                  # Annual top saver award (UGX)
    ANNUAL_FEE = 50_000                       # Annual membership fee
    DIVIDEND_DAY = 31                         # 31st December
    DIVIDEND_MONTH = 12

    # Roles - hierarchy used for access control
    ROLES = {
        'IT_ADMIN':        {'name': 'IT Administrator', 'admin': True,  'level': 100},
        'CHAIRMAN':        {'name': 'Chairman',         'admin': True,  'level': 90},
        'SECRETARY':       {'name': 'Secretary',        'admin': True,  'level': 80},
        'TREASURER':       {'name': 'Treasurer',        'admin': True,  'level': 80},
        'COMMITTEE':       {'name': 'Committee Member', 'admin': True,  'level': 70},
        'MEMBER':          {'name': 'Member',           'admin': False, 'level': 10},
    }

    # Loan Statuses
    LOAN_STATUSES = ['Pending', 'Active', 'Cleared', 'Declined', 'Defaulted']

    # Pagination
    ITEMS_PER_PAGE = 20

    # File Uploads
    #UPLOAD_FOLDER = str(BASE_DIR / 'static' / 'uploads')
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "static" / "uploads"))
    ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB per upload
