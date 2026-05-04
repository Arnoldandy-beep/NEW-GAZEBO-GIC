"""
GAZEBO Investment Club - Utility helpers
- Date / period helpers
- Financial calculations (interest, penalties, eligibility)
- Loan number generation
- Authorization decorators
"""
import calendar
import json
import re
from datetime import date, datetime, timedelta
from functools import wraps
from flask import session, redirect, url_for, flash, request, abort
from dateutil.relativedelta import relativedelta
from urllib import request as urlrequest, parse as urlparse

from config import Config
from database import get_db


_TELEGRAM_ACTIONS = {
    'RECORD_SAVINGS',
    'BULK_SAVINGS',
    'RECORD_FEE',
    'CREATE_LOAN',
    'APPROVE_LOAN',
    'REQUEST_LOAN_AMENDMENT',
    'APPROVE_LOAN_AMENDMENT',
    'DISBURSE_LOAN',
    'CREATE_FINE',
    'PAY_FINE',
    'REQUEST_EXPENSE',
    'APPROVE_EXPENSE',
}


def _get_telegram_config(db):
    token = Config.TELEGRAM_BOT_TOKEN
    chat_id = Config.TELEGRAM_CHAT_ID
    try:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key IN ('telegram_bot_token','telegram_chat_id')"
        ).fetchall()
        settings_map = {r['key']: (r['value'] or '').strip() for r in rows}
        token = settings_map.get('telegram_bot_token') or token
        chat_id = settings_map.get('telegram_chat_id') or chat_id
    except Exception:
        # settings table might be unavailable during early bootstrap
        pass
    return (token or '').strip(), (chat_id or '').strip()


def _send_telegram_alert(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    data = json.dumps(payload).encode('utf-8')
    req = urlrequest.Request(api_url, data=data, headers={'Content-Type': 'application/json'})
    with urlrequest.urlopen(req, timeout=6) as resp:
        return 200 <= int(getattr(resp, 'status', 0) or 0) < 300


def _should_notify_telegram(action, role_name):
    role = (role_name or '').upper()
    if action == 'LOGIN':
        return bool(Config.ROLES.get(role, {}).get('admin'))
    if not bool(Config.ROLES.get(role, {}).get('admin')):
        return False
    if role == 'IT_ADMIN':
        return False
    return action in _TELEGRAM_ACTIONS


def _telegram_text_for_action(action, description, role_name):
    role_label = Config.ROLES.get((role_name or '').upper(), {}).get('name', role_name or '-')
    username = session.get('username') or '-'
    full_name = session.get('full_name') or '-'
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '-').split(',')[0].strip()
    base = [
        f"GAZEBO GIC Alert",
        f"Action: {action}",
        f"By: {full_name} ({username})",
        f"Role: {role_label}",
        f"IP: {ip}",
    ]
    if description:
        base.append(f"Details: {description}")

    if action == 'LOGIN':
        ua = request.headers.get('User-Agent', '') if request else ''
        if ua:
            base.append(f"Device: {ua[:140]}")
    if action == 'CREATE_LOAN':
        base.append("Note: Loan submitted and approval workflow is required.")
    return "\n".join(base)


def send_telegram_test_message(db, text, bot_token=None, chat_id=None):
    """Send a test Telegram message using provided credentials or saved settings."""
    token = (bot_token or '').strip()
    chat = (chat_id or '').strip()
    if not token or not chat:
        token_cfg, chat_cfg = _get_telegram_config(db)
        token = token or token_cfg
        chat = chat or chat_cfg
    if not token or not chat:
        return False
    return _send_telegram_alert(token, chat, text)


def normalize_phone_for_whatsapp(phone_value):
    """Normalize common local formats to international digits for wa.me."""
    if not phone_value:
        return ''
    p = re.sub(r'[^0-9+]', '', str(phone_value).strip())
    if p.startswith('+'):
        p = p[1:]
    if p.startswith('00'):
        p = p[2:]
    if p.startswith('0'):
        p = '256' + p[1:]
    return re.sub(r'\D', '', p)


def build_whatsapp_link(phone_value, message):
    number = normalize_phone_for_whatsapp(phone_value)
    if not number:
        return None
    return f"https://wa.me/{number}?text={urlparse.quote(message or '')}"


# ---------------------------------------------------------------------------
# Date / Period helpers
# ---------------------------------------------------------------------------
def period_str(d):
    """Convert a date to YYYY-MM period string."""
    if isinstance(d, str):
        d = datetime.strptime(d, '%Y-%m-%d').date()
    return d.strftime('%Y-%m')


def period_label(period):
    """Convert YYYY-MM to e.g. 'Aug-25'."""
    if not period:
        return ''
    try:
        d = datetime.strptime(period, '%Y-%m')
        return d.strftime('%b-%y')
    except ValueError:
        return period


def end_of_month(d):
    """Last day of given date's month."""
    if isinstance(d, str):
        d = datetime.strptime(d, '%Y-%m-%d').date()
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


def first_of_month(d):
    if isinstance(d, str):
        d = datetime.strptime(d, '%Y-%m-%d').date()
    return date(d.year, d.month, 1)


def list_periods(start_period, end_period):
    """List all YYYY-MM periods inclusive between start and end."""
    start = datetime.strptime(start_period, '%Y-%m')
    end = datetime.strptime(end_period, '%Y-%m')
    out = []
    cur = start
    while cur <= end:
        out.append(cur.strftime('%Y-%m'))
        cur += relativedelta(months=1)
    return out


def all_savings_periods(today=None):
    """List of all expected savings periods from club start to current month."""
    today = today or date.today()
    end = period_str(today)
    return list_periods(Config.SAVINGS_START_PERIOD, end)


def months_between(d1, d2):
    """Whole calendar months from d1 to d2."""
    if isinstance(d1, str):
        d1 = datetime.strptime(d1, '%Y-%m-%d').date()
    if isinstance(d2, str):
        d2 = datetime.strptime(d2, '%Y-%m-%d').date()
    return (d2.year - d1.year) * 12 + d2.month - d1.month


def calculate_due_date(issued_date, term_months):
    """
    All loans due by 30th (last day) of the calendar month
    that is term_months after the issue month.
    """
    if isinstance(issued_date, str):
        issued_date = datetime.strptime(issued_date, '%Y-%m-%d').date()
    target_month = issued_date + relativedelta(months=term_months)
    return end_of_month(target_month)


# ---------------------------------------------------------------------------
# Currency helpers
# ---------------------------------------------------------------------------
def fmt_money(amount, with_symbol=True):
    """Format integer/float UGX amount."""
    if amount is None:
        amount = 0
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        return str(amount)
    formatted = f"{amount:,}"
    if with_symbol:
        return f"{Config.CURRENCY_SYMBOL} {formatted}"
    return formatted


# ---------------------------------------------------------------------------
# Member numbering
# ---------------------------------------------------------------------------
def next_member_no(db):
    """Generate the next available GIC-XXX member number."""
    rows = db.execute(
        "SELECT member_no FROM members WHERE member_no LIKE 'GIC-%'"
    ).fetchall()
    used = set()
    for row in rows:
        try:
            used.add(int(row['member_no'].split('-')[-1]))
        except (ValueError, IndexError, AttributeError):
            continue

    n = 1
    while n in used:
        n += 1
    return f"GIC-{n:03d}"


def next_loan_no(db):
    """Generate the next loan number GIC-L-XXX."""
    row = db.execute(
        "SELECT loan_no FROM loans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "GIC-L-001"
    last = row['loan_no']
    try:
        num = int(last.split('-')[-1])
    except (ValueError, IndexError):
        num = 0
    return f"GIC-L-{num + 1:03d}"


# ---------------------------------------------------------------------------
# Member savings & locked funds
# ---------------------------------------------------------------------------
def get_member_total_savings(db, member_id):
    """Total savings amount contributed by a member (gross)."""
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM savings WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    return int(row['total'] or 0)


def get_member_locked_amount(db, member_id):
    """Total of member's savings currently locked as security on active loans."""
    row = db.execute(
        """
        SELECT COALESCE(SUM(self_locked_amount), 0) AS s
          FROM loans
         WHERE member_id = ? AND status IN ('Pending', 'Active')
        """,
        (member_id,),
    ).fetchone()
    own_locked = int(row['s'] or 0)

    g1 = db.execute(
        """
        SELECT COALESCE(SUM(g1_locked_amount), 0) AS s
          FROM loans
         WHERE guarantor1_id = ? AND status IN ('Pending', 'Active')
        """,
        (member_id,),
    ).fetchone()
    g2 = db.execute(
        """
        SELECT COALESCE(SUM(g2_locked_amount), 0) AS s
          FROM loans
         WHERE guarantor2_id = ? AND status IN ('Pending', 'Active')
        """,
        (member_id,),
    ).fetchone()
    g_locked = int(g1['s'] or 0) + int(g2['s'] or 0)

    return own_locked + g_locked


def get_member_available_savings(db, member_id):
    """Savings free to be used as collateral (total - currently locked)."""
    total = get_member_total_savings(db, member_id)
    locked = get_member_locked_amount(db, member_id)
    return max(0, total - locked)


def get_member_savings_balance(db, member_id):
    """
    Net savings balance for the member (total - any locked) plus history details.
    Returns dict.
    """
    total = get_member_total_savings(db, member_id)
    locked = get_member_locked_amount(db, member_id)
    return {
        'total': total,
        'locked': locked,
        'available': max(0, total - locked),
    }


# ---------------------------------------------------------------------------
# Loan calculations
# ---------------------------------------------------------------------------
def calculate_loan_interest(principal, term_months, rate=None):
    """Total contractual interest for the loan (simple)."""
    rate = rate if rate is not None else Config.LOAN_INTEREST_RATE_MONTHLY
    return int(principal * rate * term_months)


def calculate_loan_total_due(principal, term_months, rate=None):
    """Total amount due (principal + interest) at issuance."""
    return int(principal) + calculate_loan_interest(principal, term_months, rate)


def calculate_overdue_months(due_date, today=None):
    """
    How many penalty months apply.
    Penalty kicks in from the 1st day past the due date (day 1 overdue = 1 month).
    Each additional calendar month past the due date adds another penalty month.

    Examples:
      Due 2026-03-31, today 2026-04-01 -> 1 month penalty  (day 1 late)
      Due 2026-03-31, today 2026-04-30 -> 1 month penalty
      Due 2026-03-31, today 2026-05-01 -> 2 month penalty
    """
    today = today or date.today()
    if isinstance(due_date, str):
        due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
    if today <= due_date:
        return 0
    # 1 penalty month from day 1 past due, then +1 per calendar month elapsed
    diff = (today.year - due_date.year) * 12 + (today.month - due_date.month)
    return max(1, diff)


def calculate_loan_position(loan, repayments, penalties=None, today=None):
    """
    Compute the live position of a loan: outstanding principal, contractual
    interest still due, accrued penalty interest, total outstanding, days overdue.
    `loan` is a dict-like row, `repayments` is a list of repayment rows,
    `penalties` is a list of penalty rows (optional).
    """
    today = today or date.today()
    principal = int(loan['principal'])
    rate = float(loan['interest_rate'] or Config.LOAN_INTEREST_RATE_MONTHLY)
    term_months = int(loan['term_months'])

    # Contractual interest
    contract_interest = int(principal * rate * term_months)

    # Penalty interest from explicitly recorded penalty rows OR computed if missing
    if penalties is None:
        penalties = []
    recorded_penalty = sum(int(p['penalty_amount']) for p in penalties)

    # Repayment totals
    total_paid = sum(int(r['amount']) for r in repayments)
    paid_principal = sum(int(r['principal_part'] or 0) for r in repayments)
    paid_interest = sum(int(r['interest_part'] or 0) for r in repayments)
    paid_penalty = sum(int(r['penalty_part'] or 0) for r in repayments)

    # If parts weren't itemised, distribute amount: penalty -> interest -> principal
    if (paid_principal + paid_interest + paid_penalty) == 0 and total_paid > 0:
        remaining = total_paid
        # First pay penalty
        applied_penalty = min(remaining, recorded_penalty)
        remaining -= applied_penalty
        # Then interest
        applied_interest = min(remaining, contract_interest)
        remaining -= applied_interest
        # Then principal
        applied_principal = min(remaining, principal)
        paid_penalty = applied_penalty
        paid_interest = applied_interest
        paid_principal = applied_principal

    outstanding_principal = max(0, principal - paid_principal)
    outstanding_interest = max(0, contract_interest - paid_interest)
    outstanding_penalty = max(0, recorded_penalty - paid_penalty)

    # If outstanding penalty is zero but loan is overdue and no penalty rows recorded,
    # surface a *projected* penalty so admins can see it.
    due_date = loan['due_date']
    if isinstance(due_date, str):
        due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
    overdue_months = calculate_overdue_months(due_date, today)
    days_overdue = max(0, (today - due_date).days)

    if recorded_penalty == 0 and overdue_months > 0 and outstanding_principal > 0:
        projected_penalty = int(outstanding_principal * Config.LOAN_PENALTY_RATE_MONTHLY * overdue_months)
        outstanding_penalty = projected_penalty

    total_outstanding = outstanding_principal + outstanding_interest + outstanding_penalty

    return {
        'principal':            principal,
        'contract_interest':    contract_interest,
        'recorded_penalty':     recorded_penalty,
        'total_paid':           total_paid,
        'paid_principal':       paid_principal,
        'paid_interest':        paid_interest,
        'paid_penalty':         paid_penalty,
        'outstanding_principal':outstanding_principal,
        'outstanding_interest': outstanding_interest,
        'outstanding_penalty':  outstanding_penalty,
        'total_outstanding':    total_outstanding,
        'days_overdue':         days_overdue,
        'overdue_months':       overdue_months,
        'is_overdue':           days_overdue > 0 and total_outstanding > 0,
    }


# ---------------------------------------------------------------------------
# Loan eligibility
# ---------------------------------------------------------------------------
def calculate_max_loan_amount(db, member_id, guarantor1_id=None, guarantor2_id=None):
    """
    Maximum loan amount for a borrower with optional guarantors.
    Security = borrower's available savings + guarantor1 available + guarantor2 available
    """
    own = get_member_available_savings(db, member_id)
    g1 = get_member_available_savings(db, guarantor1_id) if guarantor1_id else 0
    g2 = get_member_available_savings(db, guarantor2_id) if guarantor2_id else 0
    return own + g1 + g2


def determine_loan_security(db, member_id, principal, guarantor1_id, guarantor2_id):
    """
    Work out how to lock collateral for a proposed loan.
    Rules:
    - Two guarantors are always mandatory.
    - Borrower's own savings are locked first.
    - Any remaining (spill) is split equally between the two guarantors.
    - If borrower's savings fully cover the principal, guarantor locks are 0
      (they are still required as co-signatories but no savings are frozen).
    Returns dict { self_locked, g1_locked, g2_locked, sufficient, message }.
    """
    own_avail = get_member_available_savings(db, member_id)
    g1_avail = get_member_available_savings(db, guarantor1_id) if guarantor1_id else 0
    g2_avail = get_member_available_savings(db, guarantor2_id) if guarantor2_id else 0

    # Always require two guarantors
    if not (guarantor1_id and guarantor2_id):
        return {
            'self_locked':   0,
            'g1_locked':     0,
            'g2_locked':     0,
            'sufficient':    False,
            'message':       "Two guarantors are required for all loans.",
            'own_available': own_avail,
            'g1_available':  g1_avail,
            'g2_available':  g2_avail,
        }

    self_locked = min(principal, own_avail)
    remaining = principal - self_locked

    g1_locked = 0
    g2_locked = 0
    sufficient = True
    message = ''

    if remaining > 0:
        # Split remaining equally between the two guarantors
        half = (remaining + 1) // 2  # round up
        g1_locked = min(half, g1_avail)
        g2_locked = min(remaining - g1_locked, g2_avail)
        if g1_locked + g2_locked < remaining:
            # Try the other way around (one guarantor may have more)
            g2_locked = min(half, g2_avail)
            g1_locked = min(remaining - g2_locked, g1_avail)
        if g1_locked + g2_locked < remaining:
            sufficient = False
            shortfall = remaining - g1_locked - g2_locked
            message = f"Insufficient guarantor savings. Short by {fmt_money(shortfall)}."

    return {
        'self_locked':   self_locked,
        'g1_locked':     g1_locked,
        'g2_locked':     g2_locked,
        'sufficient':    sufficient,
        'message':       message,
        'own_available': own_avail,
        'g1_available':  g1_avail,
        'g2_available':  g2_avail,
    }


def _tenure_ceiling(term_months):
    """
    Tenure ceiling policy:
    - < 6 months  -> 500,000
    - 6 - 12      -> 1,000,000
    - > 12        -> no cap
    """
    if term_months < 6:
        return 500_000
    if term_months <= 12:
        return 1_000_000
    return None


def _history_multiplier(db, member_id):
    """
    History multiplier policy:
    - Prior default: 0.0x
    - At least one late loan: 0.75x
    - Clean history: 1.0x
    """
    has_default = db.execute(
        "SELECT 1 FROM loans WHERE member_id=? AND status='Defaulted' LIMIT 1",
        (member_id,),
    ).fetchone()
    if has_default:
        return 0.0, 'prior_default', 0

    late_row = db.execute(
        """SELECT COUNT(DISTINCT lp.loan_id) AS c
             FROM loan_penalties lp
             JOIN loans l ON l.id = lp.loan_id
            WHERE l.member_id = ?""",
        (member_id,),
    ).fetchone()
    late_count = int(late_row['c'] or 0)
    if late_count >= 1:
        return 0.75, 'late_history', late_count

    return 1.0, 'clean', 0


def calculate_member_max_eligible_loan(
    db, member_id, guarantor1_id=None, guarantor2_id=None, term_months=1
):
    """
    Policy formula:
    Max Eligible Loan = MIN(
      1) 0.75 * (borrower available + g1 available + g2 available)
      2) 3 * borrower cumulative savings
      3) 0.25 * available-to-lend pool
      4) tenure ceiling
    ) * history_multiplier
    """
    own_available = get_member_available_savings(db, member_id)
    own_cumulative = get_member_total_savings(db, member_id)
    g1_available = get_member_available_savings(db, guarantor1_id) if guarantor1_id else 0
    g2_available = get_member_available_savings(db, guarantor2_id) if guarantor2_id else 0

    active_ids = db.execute(
        "SELECT id FROM members WHERE status='Active'"
    ).fetchall()
    available_pool = sum(get_member_available_savings(db, m['id']) for m in active_ids)

    cap_security = int(0.75 * (own_available + g1_available + g2_available))
    cap_own_multiple = int(3 * own_cumulative)
    cap_pool = int(0.25 * available_pool)
    cap_tenure = _tenure_ceiling(term_months)

    caps = [cap_security, cap_own_multiple, cap_pool]
    if cap_tenure is not None:
        caps.append(cap_tenure)
    pre_history_cap = max(0, min(caps))

    history_mult, history_bucket, late_count = _history_multiplier(db, member_id)
    max_eligible = int(pre_history_cap * history_mult)

    return {
        'max_eligible': max_eligible,
        'cap_security': cap_security,
        'cap_own_multiple': cap_own_multiple,
        'cap_pool': cap_pool,
        'cap_tenure': cap_tenure,
        'pre_history_cap': pre_history_cap,
        'history_multiplier': history_mult,
        'history_bucket': history_bucket,
        'late_count': late_count,
        'own_available': own_available,
        'own_cumulative': own_cumulative,
        'g1_available': g1_available,
        'g2_available': g2_available,
        'available_pool': available_pool,
    }


# ---------------------------------------------------------------------------
# Authorization decorators
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('auth.login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('auth.login', next=request.path))
        role = session.get('role', 'MEMBER')
        # Block admin access if logged in via member number (GIC-xxx)
        if session.get('login_type') == 'member':
            flash('Member portal does not have admin access.', 'warning')
            return redirect(url_for('member.dashboard'))
        if not Config.ROLES.get(role, {}).get('admin'):
            flash('Administrative privileges required.', 'danger')
            return redirect(url_for('member.dashboard'))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Restrict to specific roles (subset of admin roles)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            if session.get('role') not in roles:
                flash('You do not have permission to access this page.', 'danger')
                return redirect(url_for('admin.dashboard') if Config.ROLES.get(
                    session.get('role'), {}).get('admin') else url_for('member.dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def log_action(action, entity_type=None, entity_id=None, description=None,
               actor_user_id=None, actor_username=None):
    """Persist an audit-log entry for the current request.

    actor_user_id / actor_username allow logging events (e.g. LOGIN_FAILED)
    where no session exists yet.
    """
    try:
        db = get_db()
        # Prefer X-Forwarded-For (set by reverse proxies) over direct remote_addr
        ip = None
        ua = None
        if request:
            ip = (request.headers.get('X-Forwarded-For') or
                  request.remote_addr or '').split(',')[0].strip() or None
            ua = (request.headers.get('User-Agent') or '')[:512] or None
        db.execute(
            """INSERT INTO audit_log
               (user_id, action, entity_type, entity_id, description,
                ip_address, actor_username, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id if actor_user_id is not None else session.get('user_id'),
                action,
                entity_type,
                entity_id,
                description,
                ip,
                actor_username if actor_username is not None else session.get('username'),
                ua,
            ),
        )
        db.commit()

        role_name = session.get('role') if session else None
        if Config.TELEGRAM_ALERTS_ENABLED and _should_notify_telegram(action, role_name):
            bot_token, chat_id = _get_telegram_config(db)
            if bot_token and chat_id:
                text = _telegram_text_for_action(action, description, role_name)
                _send_telegram_alert(bot_token, chat_id, text)
    except Exception as e:
        # Audit logging must never break the main flow
        print(f"[audit_log] failed: {e}")


# ---------------------------------------------------------------------------
# Shared notification helpers (used by both admin and member routes)
# ---------------------------------------------------------------------------

def notify_user(db, user_id, title, message, link=None):
    """Insert a notification for a specific user_id."""
    if not user_id:
        return
    db.execute(
        "INSERT INTO notifications (user_id, title, message, link) VALUES (?, ?, ?, ?)",
        (user_id, title, message, link),
    )


def notify_member(db, member_id, title, message, link=None):
    """Push notification to the active user account of a member."""
    if not member_id:
        return
    user = db.execute(
        "SELECT id FROM users WHERE member_id=? AND is_active=1 ORDER BY id LIMIT 1",
        (member_id,),
    ).fetchone()
    if user:
        notify_user(db, user['id'], title, message, link)


def notify_roles(db, roles, title, message, link=None, exclude_user_id=None):
    """Broadcast a notification to all active users in the given roles."""
    placeholders = ','.join(['?'] * len(roles))
    params = list(roles)
    sql = (
        f"SELECT u.id FROM users u JOIN members m ON m.id = u.member_id "
        f"WHERE u.is_active=1 AND m.role IN ({placeholders})"
    )
    if exclude_user_id:
        sql += " AND u.id != ?"
        params.append(exclude_user_id)
    for u in db.execute(sql, params).fetchall():
        notify_user(db, u['id'], title, message, link)


def deduct_member_savings(db, member_id, amount_to_deduct):
    """Delete/reduce savings records from most recent backwards to cover amount_to_deduct.
    Returns (actual_deducted, periods_cleared)."""
    if amount_to_deduct <= 0:
        return 0, []
    records = db.execute(
        "SELECT * FROM savings WHERE member_id=? ORDER BY period DESC",
        (member_id,),
    ).fetchall()
    remaining = amount_to_deduct
    deducted = 0
    periods_cleared = []
    for rec in records:
        if remaining <= 0:
            break
        if rec['amount'] <= remaining:
            db.execute("DELETE FROM savings WHERE id=?", (rec['id'],))
            deducted += rec['amount']
            remaining -= rec['amount']
            periods_cleared.append(rec['period'])
        else:
            db.execute("UPDATE savings SET amount=? WHERE id=?",
                       (rec['amount'] - remaining, rec['id']))
            deducted += remaining
            remaining = 0
    return deducted, periods_cleared
