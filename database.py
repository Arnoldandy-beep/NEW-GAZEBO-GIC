"""
GAZEBO Investment Club - Database connection and utilities
"""
import sqlite3
from flask import g, current_app
from contextlib import contextmanager


def get_db():
    """Get a database connection scoped to the current Flask request."""
    if 'db' not in g:
        g.db = sqlite3.connect(
            current_app.config['DATABASE_PATH'],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db


def close_db(e=None):
    """Close the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


@contextmanager
def standalone_db(db_path):
    """Standalone DB connection (used outside of Flask request context, e.g. seeding)."""
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DDL - schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- ============================================================
-- USERS / AUTHENTICATION
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    member_id       INTEGER UNIQUE,
    is_active       INTEGER DEFAULT 1,
    must_change_pw  INTEGER DEFAULT 1,
    last_login      TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
);

-- ============================================================
-- MEMBERS - core club membership
-- ============================================================
CREATE TABLE IF NOT EXISTS members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_no       TEXT UNIQUE NOT NULL,         -- e.g. GIC-001
    full_name       TEXT NOT NULL,
    phone           TEXT,
    email           TEXT,
    national_id     TEXT,
    district        TEXT DEFAULT 'Kampala',
    address         TEXT,
    next_of_kin     TEXT,
    nok_relationship TEXT,
    nok_phone       TEXT,
    nok_address     TEXT,
    role            TEXT NOT NULL DEFAULT 'MEMBER',
    join_date       DATE NOT NULL,
    status          TEXT DEFAULT 'Active',        -- Active / Inactive / Suspended
    nid_copy_url    TEXT,
    nid_copy_approved INTEGER DEFAULT 0,
    membership_form_url TEXT,
    photo_url       TEXT,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_members_no ON members(member_no);
CREATE INDEX IF NOT EXISTS idx_members_role ON members(role);
CREATE INDEX IF NOT EXISTS idx_members_status ON members(status);

-- ============================================================
-- SAVINGS - monthly contributions
-- ============================================================
CREATE TABLE IF NOT EXISTS savings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL,
    period          TEXT NOT NULL,                -- YYYY-MM e.g. 2025-08
    amount          INTEGER NOT NULL,             -- In UGX, whole numbers
    payment_date    DATE NOT NULL,
    payment_method  TEXT DEFAULT 'Bank Transfer',  -- Cash / Mobile Money / Bank
    reference_no    TEXT,
    notes           TEXT,
    recorded_by     INTEGER,                      -- user id of admin who recorded
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE,
    FOREIGN KEY (recorded_by) REFERENCES users(id),
    UNIQUE (member_id, period)
);

CREATE INDEX IF NOT EXISTS idx_savings_member ON savings(member_id);
CREATE INDEX IF NOT EXISTS idx_savings_period ON savings(period);

-- ============================================================
-- LOANS
-- ============================================================
CREATE TABLE IF NOT EXISTS loans (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_no             TEXT UNIQUE NOT NULL,     -- e.g. GIC-L-001
    member_id           INTEGER NOT NULL,
    principal           INTEGER NOT NULL,
    interest_rate       REAL DEFAULT 0.05,        -- 5% per month
    term_months         INTEGER NOT NULL,         -- 1..6
    purpose             TEXT,
    issued_date         DATE NOT NULL,
    approved_date       DATE,
    disbursed_date      DATE,
    due_date            DATE NOT NULL,            -- Always last day of month
    status              TEXT DEFAULT 'Pending',
    guarantor1_id       INTEGER,
    guarantor2_id       INTEGER,
    g1_locked_amount    INTEGER DEFAULT 0,        -- portion of g1 savings locked
    g2_locked_amount    INTEGER DEFAULT 0,
    self_locked_amount  INTEGER DEFAULT 0,        -- portion of own savings locked
    approved_by         INTEGER,
    declined_reason     TEXT,
    cleared_date        DATE,
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id)       REFERENCES members(id) ON DELETE CASCADE,
    FOREIGN KEY (guarantor1_id)   REFERENCES members(id),
    FOREIGN KEY (guarantor2_id)   REFERENCES members(id),
    FOREIGN KEY (approved_by)     REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_loans_member ON loans(member_id);
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_loans_due ON loans(due_date);

-- ============================================================
-- LOAN APPROVALS - multi-step approvals for new loans and amendments
-- ============================================================
CREATE TABLE IF NOT EXISTS loan_approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id         INTEGER NOT NULL,
    amendment_id    INTEGER,
    stage           TEXT NOT NULL,               -- NEW / AMENDMENT
    user_id         INTEGER NOT NULL,
    decision        TEXT NOT NULL DEFAULT 'APPROVE', -- APPROVE / REJECT
    comment         TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_loan_approval_unique
ON loan_approvals(loan_id, IFNULL(amendment_id, 0), stage, user_id);

-- ============================================================
-- LOAN AMENDMENTS - maker/checker queue for edits on approved loans
-- ============================================================
CREATE TABLE IF NOT EXISTS loan_amendments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id             INTEGER NOT NULL,
    requested_by        INTEGER NOT NULL,
    status              TEXT DEFAULT 'Pending',  -- Pending / Approved / Rejected
    payload_json        TEXT NOT NULL,
    reason              TEXT,
    approvals_required  INTEGER DEFAULT 2,
    approved_at         TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE,
    FOREIGN KEY (requested_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_loan_amendments_loan ON loan_amendments(loan_id);
CREATE INDEX IF NOT EXISTS idx_loan_amendments_status ON loan_amendments(status);

-- ============================================================
-- NOTIFICATIONS - dashboard alerts
-- ============================================================
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    message         TEXT NOT NULL,
    link            TEXT,
    is_read         INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at);

-- ============================================================
-- MEMBER PROFILE CHANGE REQUESTS - maker/checker for member self-updates
-- ============================================================
CREATE TABLE IF NOT EXISTS member_profile_change_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id           INTEGER NOT NULL,
    request_type        TEXT NOT NULL,   -- NEXT_OF_KIN
    payload_json        TEXT NOT NULL,
    status              TEXT DEFAULT 'Pending',  -- Pending / Approved / Rejected
    requested_by_user_id INTEGER NOT NULL,
    approved_by_user_id INTEGER,
    approved_at         TIMESTAMP,
    rejected_reason     TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE,
    FOREIGN KEY (requested_by_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (approved_by_user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_member_profile_change_status
ON member_profile_change_requests(status, request_type, created_at);

CREATE INDEX IF NOT EXISTS idx_member_profile_change_member
ON member_profile_change_requests(member_id, request_type, created_at);

-- ============================================================
-- LOAN REPAYMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS loan_repayments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id         INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    principal_part  INTEGER DEFAULT 0,
    interest_part   INTEGER DEFAULT 0,
    penalty_part    INTEGER DEFAULT 0,
    payment_date    DATE NOT NULL,
    payment_method  TEXT DEFAULT 'Bank Transfer',
    reference_no    TEXT,
    notes           TEXT,
    recorded_by     INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE,
    FOREIGN KEY (recorded_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_repayments_loan ON loan_repayments(loan_id);

-- ============================================================
-- LOAN PENALTIES - applied on 1st day of new month for overdue loans
-- ============================================================
CREATE TABLE IF NOT EXISTS loan_penalties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id         INTEGER NOT NULL,
    period          TEXT NOT NULL,                -- YYYY-MM, the month penalty applies
    penalty_amount  INTEGER NOT NULL,
    applied_date    DATE NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE,
    UNIQUE(loan_id, period)
);

-- ============================================================
-- INTEREST & DIVIDENDS - end-of-year distributions
-- ============================================================
CREATE TABLE IF NOT EXISTS dividend_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    year                    INTEGER UNIQUE NOT NULL,
    total_interest_pool     INTEGER NOT NULL,
    top_saver_award         INTEGER NOT NULL,
    distributable_pool      INTEGER NOT NULL,
    member_count            INTEGER NOT NULL,
    per_member_amount       INTEGER NOT NULL,
    top_saver_id            INTEGER,
    top_saver_total         INTEGER,
    distribution_date       DATE NOT NULL,
    status                  TEXT DEFAULT 'Distributed', -- Distributed / Pending / Cancelled
    notes                   TEXT,
    processed_by            INTEGER,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (top_saver_id) REFERENCES members(id),
    FOREIGN KEY (processed_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS dividend_payouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dividend_run_id INTEGER NOT NULL,
    member_id       INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    is_top_saver    INTEGER DEFAULT 0,
    notes           TEXT,
    FOREIGN KEY (dividend_run_id) REFERENCES dividend_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (member_id) REFERENCES members(id)
);

-- ============================================================
-- MEETING MINUTES
-- ============================================================
CREATE TABLE IF NOT EXISTS minutes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date    DATE NOT NULL,
    title           TEXT NOT NULL,
    venue           TEXT,
    chaired_by      INTEGER,
    recorded_by     INTEGER,
    attendance      TEXT,                  -- Comma separated member IDs or names
    agenda          TEXT,
    discussion      TEXT,
    resolutions     TEXT,
    signed_file     TEXT,
    next_meeting    DATE,
    is_published    INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chaired_by)  REFERENCES members(id),
    FOREIGN KEY (recorded_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_minutes_date ON minutes(meeting_date);

-- ============================================================
-- ANNUAL FEES
-- ============================================================
CREATE TABLE IF NOT EXISTS annual_fees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL,
    year            INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    status          TEXT DEFAULT 'Unpaid',  -- Paid / Unpaid
    paid_date       DATE,
    deadline        DATE,
    notes           TEXT,
    recorded_by     INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE,
    FOREIGN KEY (recorded_by) REFERENCES users(id),
    UNIQUE (member_id, year)
);

-- ============================================================
-- OPERATIONAL INCOME (non-fee/fine income lines)
-- ============================================================
CREATE TABLE IF NOT EXISTS operational_incomes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    income_no               TEXT UNIQUE NOT NULL,
    income_date             DATE NOT NULL,
    amount                  INTEGER NOT NULL,
    source                  TEXT NOT NULL,
    notes                   TEXT,
    recorded_by             INTEGER NOT NULL,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (recorded_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_operational_incomes_date ON operational_incomes(income_date);

-- ============================================================
-- EXPENSE REQUESTS (treasurer submits, chairman approves)
-- ============================================================
CREATE TABLE IF NOT EXISTS expenses (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_no                  TEXT UNIQUE NOT NULL,
    expense_date                DATE NOT NULL,
    amount                      INTEGER NOT NULL,
    purpose                     TEXT NOT NULL,
    category                    TEXT DEFAULT 'Operations',
    notes                       TEXT,
    status                      TEXT DEFAULT 'Pending', -- Pending / Approved / Rejected
    requested_by                INTEGER NOT NULL,
    requested_approver_user_id  INTEGER,
    approved_by                 INTEGER,
    approved_at                 TIMESTAMP,
    decision_notes              TEXT,
    receipt_path                TEXT,
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (requested_by) REFERENCES users(id),
    FOREIGN KEY (requested_approver_user_id) REFERENCES users(id),
    FOREIGN KEY (approved_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses(status);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);

-- ============================================================
-- FINES
-- ============================================================
CREATE TABLE IF NOT EXISTS fines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL,
    fine_date       DATE NOT NULL,
    violation       TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    status          TEXT DEFAULT 'Unpaid',  -- Paid / Unpaid / Waived
    paid_date       DATE,
    issued_by       INTEGER,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE,
    FOREIGN KEY (issued_by) REFERENCES users(id)
);

-- ============================================================
-- AUDIT LOG - record every administrative action
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    action          TEXT NOT NULL,
    entity_type     TEXT,
    entity_id       INTEGER,
    description     TEXT,
    ip_address      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);

-- ============================================================
-- SETTINGS - editable system parameters
-- ============================================================
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    description     TEXT,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by      INTEGER,
    FOREIGN KEY (updated_by) REFERENCES users(id)
);
"""


def init_schema(db_path):
    """Initialize the database schema. Idempotent."""
    with standalone_db(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _apply_migrations(conn)


def _column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r['name'] == column for r in rows)


def _apply_migrations(conn):
    """Schema migrations for existing databases."""
    if not _column_exists(conn, 'loans', 'application_file'):
        conn.execute("ALTER TABLE loans ADD COLUMN application_file TEXT")
    if not _column_exists(conn, 'minutes', 'signed_file'):
        conn.execute("ALTER TABLE minutes ADD COLUMN signed_file TEXT")
    if not _column_exists(conn, 'expenses', 'receipt_path'):
        conn.execute("ALTER TABLE expenses ADD COLUMN receipt_path TEXT")
    if not _column_exists(conn, 'members', 'nid_copy_url'):
        conn.execute("ALTER TABLE members ADD COLUMN nid_copy_url TEXT")
    if not _column_exists(conn, 'members', 'nid_copy_approved'):
        conn.execute("ALTER TABLE members ADD COLUMN nid_copy_approved INTEGER DEFAULT 0")
    if not _column_exists(conn, 'members', 'membership_form_url'):
        conn.execute("ALTER TABLE members ADD COLUMN membership_form_url TEXT")
    if not _column_exists(conn, 'members', 'nok_relationship'):
        conn.execute("ALTER TABLE members ADD COLUMN nok_relationship TEXT")
    if not _column_exists(conn, 'members', 'nok_address'):
        conn.execute("ALTER TABLE members ADD COLUMN nok_address TEXT")
    # Audit log enrichment
    if not _column_exists(conn, 'audit_log', 'user_agent'):
        conn.execute("ALTER TABLE audit_log ADD COLUMN user_agent TEXT")
    if not _column_exists(conn, 'audit_log', 'actor_username'):
        conn.execute("ALTER TABLE audit_log ADD COLUMN actor_username TEXT")
    # Account lockout
    if not _column_exists(conn, 'users', 'failed_login_count'):
        conn.execute("ALTER TABLE users ADD COLUMN failed_login_count INTEGER DEFAULT 0")
    if not _column_exists(conn, 'users', 'locked_until'):
        conn.execute("ALTER TABLE users ADD COLUMN locked_until TIMESTAMP")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS member_profile_change_requests (
               id                  INTEGER PRIMARY KEY AUTOINCREMENT,
               member_id           INTEGER NOT NULL,
               request_type        TEXT NOT NULL,
               payload_json        TEXT NOT NULL,
               status              TEXT DEFAULT 'Pending',
               requested_by_user_id INTEGER NOT NULL,
               approved_by_user_id INTEGER,
               approved_at         TIMESTAMP,
               rejected_reason     TEXT,
               created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE,
               FOREIGN KEY (requested_by_user_id) REFERENCES users(id) ON DELETE CASCADE,
               FOREIGN KEY (approved_by_user_id) REFERENCES users(id)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_member_profile_change_status
           ON member_profile_change_requests(status, request_type, created_at)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_member_profile_change_member
           ON member_profile_change_requests(member_id, request_type, created_at)"""
    )
