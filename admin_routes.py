"""
GAZEBO Investment Club - Administrative routes
Handles members, savings, loans, dividends, minutes, fees, fines, settings
"""
import os
import uuid
import json
import csv
import io
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, current_app, jsonify, Response
)
from werkzeug.security import generate_password_hash
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

from database import get_db
from utils import (
    admin_required, login_required, log_action, role_required,
    fmt_money, period_str, period_label, all_savings_periods,
    end_of_month, calculate_due_date, calculate_loan_position,
    calculate_overdue_months, get_member_total_savings,
    get_member_locked_amount, get_member_available_savings,
    determine_loan_security, calculate_max_loan_amount,
    next_member_no, next_loan_no,
)
from config import Config

bp = Blueprint('admin', __name__, url_prefix='/admin')


def _save_upload(file_field_name, subfolder=''):
    """Save an uploaded image. Returns relative path like 'uploads/photos/abc.jpg' or None."""
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in Config.ALLOWED_IMAGE_EXTENSIONS:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    dest_dir = os.path.join(Config.UPLOAD_FOLDER, subfolder)
    os.makedirs(dest_dir, exist_ok=True)
    f.save(os.path.join(dest_dir, filename))
    rel = f"uploads/{subfolder}/{filename}" if subfolder else f"uploads/{filename}"
    return rel.replace('\\', '/')


def _save_expense_receipt(file_field_name='receipt_file'):
    """Save an expense receipt (image or PDF). Returns relative path or None."""
    ALLOWED = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf'}
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    dest_dir = os.path.join(Config.UPLOAD_FOLDER, 'expense-receipts')
    os.makedirs(dest_dir, exist_ok=True)
    f.save(os.path.join(dest_dir, filename))
    return f"uploads/expense-receipts/{filename}"


def _auto_apply_penalties(db, loan_id):
    """Auto-apply overdue penalties for a single Active loan without locking the DB.
    Safe to call on every page view or repayment — idempotent per period."""
    loan = db.execute("SELECT * FROM loans WHERE id=? AND status='Active'", (loan_id,)).fetchone()
    if not loan:
        return 0
    due_date = loan['due_date']
    if isinstance(due_date, str):
        from datetime import datetime as _dt
        due_date_obj = _dt.strptime(due_date, '%Y-%m-%d').date()
    else:
        due_date_obj = due_date
    today = date.today()
    overdue_months = calculate_overdue_months(due_date_obj, today)
    if overdue_months <= 0:
        return 0
    repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan_id,)).fetchall()
    pos = calculate_loan_position(loan, repays, [])
    if pos['outstanding_principal'] <= 0:
        return 0
    new_count = 0
    for k in range(1, overdue_months + 1):
        # Period label: month k after due (1-indexed)
        m_offset = due_date_obj.month + k - 1
        p_year = due_date_obj.year + m_offset // 12
        p_month = m_offset % 12 + 1
        period = f"{p_year}-{p_month:02d}"
        existing = db.execute(
            "SELECT 1 FROM loan_penalties WHERE loan_id=? AND period=?",
            (loan_id, period),
        ).fetchone()
        if existing:
            continue
        penalty_amount = int(pos['outstanding_principal'] * Config.LOAN_PENALTY_RATE_MONTHLY)
        applied_date = date(p_year, p_month, 1).isoformat()
        db.execute(
            """INSERT INTO loan_penalties (loan_id, period, penalty_amount, applied_date, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (loan_id, period, penalty_amount, applied_date,
             f'Auto-penalty for overdue period {period}'),
        )
        new_count += 1
    if new_count:
        db.commit()
    return new_count


def _save_loan_application(file_field_name='application_file'):
    """Save signed loan application file. Returns relative path or None."""
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    allowed = {'pdf', 'jpg', 'jpeg', 'png', 'webp'}
    if ext not in allowed:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    dest_dir = os.path.join(Config.UPLOAD_FOLDER, 'loan-applications')
    os.makedirs(dest_dir, exist_ok=True)
    f.save(os.path.join(dest_dir, filename))
    return f"uploads/loan-applications/{filename}"


def _save_signed_minutes(file_field_name='signed_file'):
    """Save signed meeting minutes file. Returns relative path or None."""
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    allowed = {'pdf', 'jpg', 'jpeg', 'png', 'webp'}
    if ext not in allowed:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    dest_dir = os.path.join(Config.UPLOAD_FOLDER, 'minutes')
    os.makedirs(dest_dir, exist_ok=True)
    f.save(os.path.join(dest_dir, filename))
    return f"uploads/minutes/{filename}"


def _notify_roles(db, roles, title, message, link=None, exclude_user_id=None):
    placeholders = ','.join(['?'] * len(roles))
    params = list(roles)
    sql = f"""SELECT u.id
                FROM users u
                JOIN members m ON m.id = u.member_id
               WHERE u.is_active = 1 AND m.role IN ({placeholders})"""
    if exclude_user_id:
        sql += " AND u.id != ?"
        params.append(exclude_user_id)
    users = db.execute(sql, params).fetchall()
    for u in users:
        db.execute(
            """INSERT INTO notifications (user_id, title, message, link)
               VALUES (?, ?, ?, ?)""",
            (u['id'], title, message, link),
        )


def _notify_user(db, user_id, title, message, link=None):
    if not user_id:
        return
    db.execute(
        """INSERT INTO notifications (user_id, title, message, link)
           VALUES (?, ?, ?, ?)""",
        (user_id, title, message, link),
    )


def _notify_member(db, member_id, title, message, link=None):
    """Push a notification to a member login if one exists and is active."""
    if not member_id:
        return
    user = db.execute(
        "SELECT id FROM users WHERE member_id=? AND is_active=1 ORDER BY id LIMIT 1",
        (member_id,),
    ).fetchone()
    if user:
        _notify_user(db, user['id'], title, message, link)


def _member_removal_impact(db, member_id):
    counts = {
        'logins': db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE member_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'savings': db.execute(
            "SELECT COUNT(*) AS c FROM savings WHERE member_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'borrower_loans': db.execute(
            "SELECT COUNT(*) AS c FROM loans WHERE member_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'guarantor_loans': db.execute(
            "SELECT COUNT(*) AS c FROM loans WHERE guarantor1_id = ? OR guarantor2_id = ?",
            (member_id, member_id),
        ).fetchone()['c'],
        'fines': db.execute(
            "SELECT COUNT(*) AS c FROM fines WHERE member_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'fees': db.execute(
            "SELECT COUNT(*) AS c FROM annual_fees WHERE member_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'dividend_payouts': db.execute(
            "SELECT COUNT(*) AS c FROM dividend_payouts WHERE member_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'top_saver_runs': db.execute(
            "SELECT COUNT(*) AS c FROM dividend_runs WHERE top_saver_id = ?",
            (member_id,),
        ).fetchone()['c'],
        'minutes_chaired': db.execute(
            "SELECT COUNT(*) AS c FROM minutes WHERE chaired_by = ?",
            (member_id,),
        ).fetchone()['c'],
    }
    historical_refs = (
        counts['savings']
        + counts['borrower_loans']
        + counts['guarantor_loans']
        + counts['fines']
        + counts['fees']
        + counts['dividend_payouts']
        + counts['top_saver_runs']
        + counts['minutes_chaired']
    )
    counts['can_hard_delete'] = historical_refs == 0
    return counts


def _next_archived_member_no(db, old_member_no):
    """Generate a unique archive member number so original GIC numbers can be reused."""
    base = (old_member_no or 'MEM').replace(' ', '')
    candidate = f"ARC-{base}"
    i = 1
    while db.execute("SELECT 1 FROM members WHERE member_no = ?", (candidate,)).fetchone():
        candidate = f"ARC-{base}-{i}"
        i += 1
    return candidate


def _normalize_legacy_archived_status(db):
    """Keep legacy records consistent after status rename from Archived to Exited."""
    db.execute("UPDATE members SET status = 'Exited' WHERE status = 'Archived'")


def _next_doc_no(db, table_name, column_name, prefix):
    row = db.execute(
        f"SELECT {column_name} AS no FROM {table_name} ORDER BY id DESC LIMIT 1"
    ).fetchone()
    seq = 1
    if row and row['no']:
        try:
            seq = int(str(row['no']).split('-')[-1]) + 1
        except (TypeError, ValueError):
            seq = 1
    return f"{prefix}-{seq:04d}"


def _operational_fund_snapshot(db):
    fees_collected = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM annual_fees WHERE status='Paid'"
    ).fetchone()['s']
    fines_collected = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM fines WHERE status='Paid'"
    ).fetchone()['s']
    recorded_income = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM operational_incomes"
    ).fetchone()['s']
    approved_expenses = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE status='Approved'"
    ).fetchone()['s']
    pending_expenses = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE status='Pending'"
    ).fetchone()['s']

    operational_income = int(fees_collected or 0) + int(fines_collected or 0) - int(approved_expenses or 0)
    operational_fund_total = (
        int(fees_collected or 0)
        + int(fines_collected or 0)
        + int(recorded_income or 0)
        - int(approved_expenses or 0)
    )
    available_after_pending = operational_fund_total - int(pending_expenses or 0)

    return {
        'fees_collected': int(fees_collected or 0),
        'fines_collected': int(fines_collected or 0),
        'recorded_income': int(recorded_income or 0),
        'approved_expenses': int(approved_expenses or 0),
        'pending_expenses': int(pending_expenses or 0),
        'operational_income': operational_income,
        'operational_fund_total': operational_fund_total,
        'available_after_pending': available_after_pending,
    }


def _loan_interest_snapshot(db):
    collected = db.execute(
        "SELECT COALESCE(SUM(interest_part),0) s FROM loan_repayments"
    ).fetchone()['s']

    projected_interest = 0
    projected_penalty = 0
    open_loans = db.execute(
        "SELECT * FROM loans WHERE status IN ('Active','Pending')"
    ).fetchall()
    for loan in open_loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        projected_interest += int(pos['outstanding_interest'] or 0)
        projected_penalty += int(pos['outstanding_penalty'] or 0)

    return {
        'collected': int(collected or 0),
        'projected_open': projected_interest + projected_penalty,
        'projected_interest': projected_interest,
        'projected_penalty': projected_penalty,
    }


def _remove_member_record(db, member):
    impact = _member_removal_impact(db, member['id'])
    linked_login = db.execute(
        "SELECT username FROM users WHERE member_id = ? ORDER BY id LIMIT 1",
        (member['id'],),
    ).fetchone()

    if impact['can_hard_delete']:
        db.execute("DELETE FROM members WHERE id = ?", (member['id'],))
        return {
            'mode': 'deleted',
            'login_username': linked_login['username'] if linked_login else None,
            'message': f"Member {member['member_no']} was fully deleted.",
        }

    archived_member_no = _next_archived_member_no(db, member['member_no'])
    db.execute("UPDATE users SET is_active = 0 WHERE member_id = ?", (member['id'],))
    db.execute(
        """UPDATE members
              SET member_no = ?, status = 'Exited', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (archived_member_no, member['id']),
    )
    return {
        'mode': 'exited',
        'login_username': linked_login['username'] if linked_login else None,
        'message': (
            f"Member {member['member_no']} has historical records, so the system marked the member as Exited "
            "and disabled their login instead of hard-deleting them."
        ),
    }


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@bp.route('/')
@admin_required
def dashboard():
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()

    # Member counts
    total_members = db.execute(
        "SELECT COUNT(*) c FROM members WHERE status='Active'"
    ).fetchone()['c']

    # Total savings
    total_savings = db.execute(
        """SELECT COALESCE(SUM(s.amount), 0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE m.status = 'Active'"""
    ).fetchone()['s']

    # Loan stats
    loan_stats = db.execute(
        """SELECT status, COUNT(*) c, COALESCE(SUM(principal),0) p
             FROM loans GROUP BY status"""
    ).fetchall()
    loan_summary = {row['status']: dict(row) for row in loan_stats}

    # Outstanding from active/pending loans
    active_loans = db.execute(
        """SELECT l.*, m.member_no, m.full_name, m.phone
             FROM loans l
             LEFT JOIN members m ON m.id = l.member_id
            WHERE l.status IN ('Active','Pending')"""
    ).fetchall()

    outstanding_principal = 0
    outstanding_interest = 0
    outstanding_penalty = 0
    overdue_count = 0
    overdue_loans = []
    for loan in active_loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id = ?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id = ?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        outstanding_principal += pos['outstanding_principal']
        outstanding_interest += pos['outstanding_interest']
        outstanding_penalty += pos['outstanding_penalty']
        if pos['is_overdue']:
            overdue_count += 1
            overdue_loans.append({
                'id': loan['id'],
                'loan_no': loan['loan_no'],
                'member_no': loan['member_no'],
                'full_name': loan['full_name'],
                'amount_owed': pos['total_outstanding'],
                'overdue_months': pos['overdue_months'],
                'days_overdue': pos['days_overdue'],
                'penalty': pos['outstanding_penalty'],
            })

    # Savings defaulters this month
    today = date.today()
    cur_period = period_str(today)
    current_year = today.year

    # Current month savings total and defaulters
    month_savings_total = db.execute(
        """SELECT COALESCE(SUM(s.amount), 0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE s.period = ? AND m.status = 'Active'""",
        (cur_period,),
    ).fetchone()['s']

    active_members_rows = db.execute(
        "SELECT id, member_no, full_name, phone, join_date FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    all_paid_rows = db.execute(
        "SELECT member_id, period FROM savings"
    ).fetchall()
    paid_map = {}
    for r in all_paid_rows:
        paid_map.setdefault(r['member_id'], set()).add(r['period'])

    current_month_defaulters = []
    periods_to_date = all_savings_periods(today)
    for m in active_members_rows:
        paid_periods = paid_map.get(m['id'], set())
        if cur_period in paid_periods:
            continue
        join_date = m['join_date']
        if isinstance(join_date, str):
            join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
        else:
            join_d = join_date
        join_period = period_str(join_d)
        expected = [p for p in periods_to_date if p >= join_period]
        missing = [p for p in expected if p not in paid_periods]
        arrears_amount = len(missing) * Config.MONTHLY_SAVINGS_AMOUNT
        current_month_defaulters.append({
            'id': m['id'],
            'member_no': m['member_no'],
            'full_name': m['full_name'],
            'phone': m['phone'],
            'arrears_amount': arrears_amount,
        })

    # Current month savings coverage
    month_paid_members = db.execute(
        """SELECT COUNT(DISTINCT s.member_id) c
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE s.period = ? AND m.status = 'Active'""",
        (cur_period,),
    ).fetchone()['c']
    month_outstanding_members = max(0, total_members - month_paid_members)
    month_outstanding_amount = month_outstanding_members * Config.MONTHLY_SAVINGS_AMOUNT

    # Annual fees summary (current year)
    fees_paid_members = db.execute(
        """SELECT COUNT(DISTINCT member_id) c
             FROM annual_fees
            WHERE year = ? AND status = 'Paid'""",
        (current_year,),
    ).fetchone()['c']
    fees_pool = db.execute(
        """SELECT COALESCE(SUM(amount), 0) s
             FROM annual_fees
            WHERE year = ? AND status = 'Paid'""",
        (current_year,),
    ).fetchone()['s']
    fees_outstanding_members = max(0, total_members - fees_paid_members)
    fees_projection = total_members * Config.ANNUAL_FEE

    # Fines summary (current year)
    fines_year = db.execute(
        """SELECT
                 COALESCE(SUM(CASE WHEN status='Paid' THEN amount ELSE 0 END), 0) collected,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN amount ELSE 0 END), 0) outstanding,
                 COALESCE(SUM(amount), 0) issued,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN 1 ELSE 0 END), 0) unpaid_count
             FROM fines
            WHERE strftime('%Y', fine_date) = ?""",
        (str(current_year),),
    ).fetchone()

    # Minutes summary
    minutes_published = db.execute(
        "SELECT COUNT(*) c FROM minutes WHERE is_published = 1"
    ).fetchone()['c']
    minutes_total = db.execute(
        "SELECT COUNT(*) c FROM minutes"
    ).fetchone()['c']

    # Operational pool and expenses
    ops = _operational_fund_snapshot(db)
    operations_collected = ops['fees_collected'] + ops['fines_collected']
    operations_projection = int(fees_projection or 0) + int(fines_year['issued'] or 0)
    operations_outstanding = max(0, operations_projection - operations_collected)
    operations_income = ops['operational_income']
    operations_fund_total = ops['operational_fund_total']
    operations_available_after_pending = ops['available_after_pending']
    expenses_spent = ops['approved_expenses']
    expenses_pending = ops['pending_expenses']
    expenses_spent_count = db.execute(
        "SELECT COUNT(*) c FROM expenses WHERE status='Approved'"
    ).fetchone()['c']

    # Pending expenses for chairman dashboard panel
    pending_expenses_rows = db.execute(
        """SELECT e.*, req.username AS requested_by_name,
                  COALESCE(reqm.full_name, req.username) AS requested_by_full
             FROM expenses e
             LEFT JOIN users req ON req.id = e.requested_by
             LEFT JOIN members reqm ON reqm.id = req.member_id
            WHERE e.status = 'Pending'
            ORDER BY e.created_at DESC"""
    ).fetchall()

    # Loan interest performance
    loan_interest = _loan_interest_snapshot(db)
    loan_interest_collected = loan_interest['collected']
    loan_interest_projected = loan_interest['projected_open']

    # Recent activity
    recent_audit = db.execute(
        """SELECT al.*, m.full_name FROM audit_log al
             LEFT JOIN users u ON u.id = al.user_id
             LEFT JOIN members m ON m.id = u.member_id
             ORDER BY al.id DESC LIMIT 12"""
    ).fetchall()

    unread_notifications = db.execute(
        """SELECT * FROM notifications
            WHERE user_id = ? AND is_read = 0
            ORDER BY id DESC LIMIT 8""",
        (session['user_id'],),
    ).fetchall()

    # Available cash = total savings - outstanding principal of active loans
    cash_available = total_savings - outstanding_principal

    # Pool utilization
    pool_utilization = 0
    if total_savings > 0:
        pool_utilization = round((outstanding_principal / total_savings) * 100, 1)

    # Savings trend - last 6 months
    periods = all_savings_periods()[-6:]
    trend = []
    for p in periods:
        row = db.execute(
            """SELECT COALESCE(SUM(s.amount),0) s, COUNT(*) c
                 FROM savings s
                 JOIN members m ON m.id = s.member_id
                WHERE s.period=? AND m.status='Active'""",
            (p,),
        ).fetchone()
        trend.append({
            'period': p,
            'label': period_label(p),
            'total': row['s'] or 0,
            'count': row['c'] or 0,
        })

    # Savings trend - last 5 years (year totals)
    start_year = int(Config.SAVINGS_START_PERIOD.split('-')[0])
    years = list(range(max(start_year, current_year - 4), current_year + 1))
    yearly_trend = []
    for y in years:
        row = db.execute(
            """SELECT COALESCE(SUM(s.amount),0) s
                 FROM savings s
                 JOIN members m ON m.id = s.member_id
                WHERE s.period LIKE ? AND m.status='Active'""",
            (f"{y}-%",),
        ).fetchone()
        yearly_trend.append({
            'year': y,
            'total': row['s'] or 0,
        })

    return render_template(
        'admin/dashboard.html',
        total_members=total_members,
        total_savings=total_savings,
        outstanding_principal=outstanding_principal,
        outstanding_interest=outstanding_interest,
        outstanding_penalty=outstanding_penalty,
        cash_available=cash_available,
        pool_utilization=pool_utilization,
        loan_summary=loan_summary,
        overdue_count=overdue_count,
        recent_audit=recent_audit,
        unread_notifications=unread_notifications,
        trend=trend,
        current_period=period_label(cur_period),
        current_year=current_year,
        month_paid_members=month_paid_members,
        month_outstanding_members=month_outstanding_members,
        month_outstanding_amount=month_outstanding_amount,
        month_savings_total=month_savings_total,
        current_month_defaulters=current_month_defaulters,
        overdue_loans=overdue_loans,
        fees_pool=fees_pool,
        fees_paid_members=fees_paid_members,
        fees_outstanding_members=fees_outstanding_members,
        fees_projection=fees_projection,
        fines_collected=fines_year['collected'],
        fines_outstanding=fines_year['outstanding'],
        fines_unpaid_count=fines_year['unpaid_count'],
        minutes_published=minutes_published,
        minutes_total=minutes_total,
        operations_collected=operations_collected,
        operations_income=operations_income,
        operations_projection=operations_projection,
        operations_outstanding=operations_outstanding,
        operations_fund_total=operations_fund_total,
        operations_available_after_pending=operations_available_after_pending,
        expenses_spent=expenses_spent,
        expenses_pending=expenses_pending,
        expenses_spent_count=expenses_spent_count,
        loan_interest_collected=loan_interest_collected,
        loan_interest_projected=loan_interest_projected,
        pending_rows=pending_expenses_rows,
        yearly_trend=yearly_trend,
    )


@bp.route('/notifications/<int:notification_id>/open')
@admin_required
def notification_open(notification_id):
    db = get_db()
    n = db.execute(
        "SELECT * FROM notifications WHERE id=? AND user_id=?",
        (notification_id, session['user_id']),
    ).fetchone()
    if not n:
        flash('Notification not found.', 'warning')
        return redirect(url_for('admin.dashboard'))

    db.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notification_id,))
    db.commit()
    if n['link']:
        return redirect(n['link'])
    return redirect(url_for('admin.dashboard'))


# ===========================================================================
# MEMBERS
# ===========================================================================
@bp.route('/members')
@admin_required
def members_list():
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()
    q = (request.args.get('q') or '').strip()
    role_filter = request.args.get('role') or ''
    status_filter = request.args.get('status') or ''

    sql = "SELECT * FROM members WHERE 1=1"
    params = []
    if q:
        sql += " AND (full_name LIKE ? OR member_no LIKE ? OR phone LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if role_filter:
        sql += " AND role = ?"
        params.append(role_filter)
    if status_filter == 'all':
        pass
    elif status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    else:
        sql += " AND status NOT IN ('Exited', 'Archived')"
    sql += " ORDER BY member_no"
    members = db.execute(sql, params).fetchall()

    # Add savings totals per member
    enriched = []
    for m in members:
        savings_total = get_member_total_savings(db, m['id'])
        locked = get_member_locked_amount(db, m['id'])
        has_user = db.execute(
            "SELECT 1 FROM users WHERE member_id = ?", (m['id'],)
        ).fetchone()
        d = dict(m)
        if m['status'] in {'Exited', 'Archived'}:
            d['savings_total'] = None
            d['locked'] = None
        else:
            d['savings_total'] = savings_total
            d['locked'] = locked
        d['has_login'] = bool(has_user)
        enriched.append(d)

    return render_template(
        'admin/members_list.html',
        members=enriched, q=q,
        role_filter=role_filter, status_filter=status_filter,
    )


@bp.route('/members/new', methods=['GET', 'POST'])
@admin_required
def member_create():
    db = get_db()
    if request.method == 'POST':
        # Personal
        surname     = (request.form.get('surname') or '').strip().upper()
        first_name  = (request.form.get('first_name') or '').strip().upper()
        other_names = (request.form.get('other_names') or '').strip().upper()
        full_name   = f"{first_name} {surname}".strip()
        gender      = request.form.get('gender') or ''
        date_of_birth   = request.form.get('date_of_birth') or None
        nationality     = (request.form.get('nationality') or 'Ugandan').strip()
        marital_status  = request.form.get('marital_status') or ''
        national_id = (request.form.get('national_id') or '').strip()
        tin         = (request.form.get('tin') or '').strip()
        # Contact
        phone          = (request.form.get('phone') or '').strip()
        alt_phone      = (request.form.get('alt_phone') or '').strip()
        whatsapp_no    = (request.form.get('whatsapp_no') or '').strip()
        email          = (request.form.get('email') or '').strip()
        district       = (request.form.get('district') or 'Kampala').strip()
        village_street = (request.form.get('village_street') or '').strip()
        # Occupation
        employment_status = request.form.get('employment_status') or ''
        occupation        = (request.form.get('occupation') or '').strip()
        employer_business = (request.form.get('employer_business') or '').strip()
        monthly_income_raw = request.form.get('monthly_income') or ''
        monthly_income = float(monthly_income_raw) if monthly_income_raw else None
        # Contributions
        monthly_contribution_raw = request.form.get('monthly_contribution') or '100000'
        try:
            monthly_contribution = float(monthly_contribution_raw)
        except ValueError:
            monthly_contribution = 100000.0
        preferred_pay_date_raw = request.form.get('preferred_pay_date') or '5'
        try:
            preferred_pay_date = int(preferred_pay_date_raw)
        except ValueError:
            preferred_pay_date = 5
        mobile_money_no = (request.form.get('mobile_money_no') or '').strip()
        bank_account_no = (request.form.get('bank_account_no') or '').strip()
        # NOK
        next_of_kin     = (request.form.get('next_of_kin') or '').strip()
        nok_relationship = (request.form.get('nok_relationship') or '').strip()
        nok_phone       = (request.form.get('nok_phone') or '').strip()
        nok_address     = (request.form.get('nok_address') or '').strip()
        # Membership
        role            = request.form.get('role') or 'MEMBER'
        join_date       = request.form.get('join_date') or date.today().isoformat()
        share_account_no = (request.form.get('share_account_no') or '').strip()
        notes           = request.form.get('notes') or ''
        # Login
        create_login    = request.form.get('create_login') == 'on'
        username        = (request.form.get('username') or '').strip().lower()
        password        = request.form.get('password') or ''

        if not first_name or not surname:
            flash('First name and surname are required.', 'danger')
            return render_template('admin/member_form.html', member=None, roles=Config.ROLES, today=date.today())

        if monthly_contribution < 100000:
            flash('Monthly contribution must be at least UGX 100,000.', 'danger')
            return render_template('admin/member_form.html', member=None, roles=Config.ROLES, today=date.today())

        if role not in Config.ROLES:
            flash('Invalid role.', 'danger')
            return render_template('admin/member_form.html', member=None, roles=Config.ROLES, today=date.today())

        if role == 'IT_ADMIN' and session.get('role') != 'IT_ADMIN':
            flash('Only the IT Administrator can create another IT Administrator.', 'danger')
            return render_template('admin/member_form.html', member=None, roles=Config.ROLES, today=date.today())

        # File uploads
        photo_url   = _save_upload('photo', 'photos')
        nid_copy_url = _save_upload('nid_copy', 'nid_copies')

        member_no = next_member_no(db)
        cur = db.execute(
            """INSERT INTO members
               (member_no, full_name, surname, first_name, other_names,
                gender, date_of_birth, nationality, marital_status,
                national_id, tin, nid_copy_url,
                phone, alt_phone, whatsapp_no, email, district, village_street,
                employment_status, occupation, employer_business, monthly_income,
                monthly_contribution, preferred_pay_date, mobile_money_no, bank_account_no,
                next_of_kin, nok_relationship, nok_phone, nok_address,
                role, join_date, share_account_no, status, notes, photo_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (member_no, full_name, surname, first_name, other_names,
             gender, date_of_birth, nationality, marital_status,
             national_id, tin, nid_copy_url,
             phone, alt_phone, whatsapp_no, email, district, village_street,
             employment_status, occupation, employer_business, monthly_income,
             monthly_contribution, preferred_pay_date, mobile_money_no, bank_account_no,
             next_of_kin, nok_relationship, nok_phone, nok_address,
             role, join_date, share_account_no, 'Active', notes, photo_url),
        )
        member_id = cur.lastrowid

        if create_login:
            if not username:
                username = member_no.lower()  # default: gic-xxx
                base = username
                i = 1
                while db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    i += 1
                    username = f"{base}-{i}"
            if not password:
                password = 'gazebo123'
            db.execute(
                """INSERT INTO users
                   (username, password_hash, member_id, must_change_pw, is_active)
                   VALUES (?, ?, ?, 1, 1)""",
                (username, generate_password_hash(password), member_id),
            )
            flash(f"Login created. Username: {username} / Default password: "
                  f"{password} (must change on first login).", 'info')

        db.commit()
        log_action('CREATE_MEMBER', 'member', member_id,
                   f"Created member {member_no} - {full_name}")
        flash(f"Member {member_no} ({full_name}) created.", 'success')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    return render_template('admin/member_form.html', member=None,
                           roles=Config.ROLES, today=date.today())


@bp.route('/members/<int:member_id>')
@admin_required
def member_detail(member_id):
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()
    member = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))

    is_exited_member = member['status'] in {'Exited', 'Archived'}

    savings = []
    loans = []
    fines = []
    fees = []
    if not is_exited_member:
        savings = db.execute(
            "SELECT * FROM savings WHERE member_id = ? ORDER BY period DESC",
            (member_id,),
        ).fetchall()
        loans = db.execute(
            """SELECT l.*, m1.full_name g1_name, m2.full_name g2_name
                 FROM loans l
                 LEFT JOIN members m1 ON m1.id = l.guarantor1_id
                 LEFT JOIN members m2 ON m2.id = l.guarantor2_id
                WHERE l.member_id = ?
                ORDER BY l.issued_date DESC""",
            (member_id,),
        ).fetchall()
        fines = db.execute(
            "SELECT * FROM fines WHERE member_id = ? ORDER BY fine_date DESC",
            (member_id,),
        ).fetchall()
        fees = db.execute(
            "SELECT * FROM annual_fees WHERE member_id = ? ORDER BY year DESC",
            (member_id,),
        ).fetchall()
    user_row = db.execute(
        "SELECT * FROM users WHERE member_id = ?", (member_id,),
    ).fetchone()

    total_savings = 0 if is_exited_member else get_member_total_savings(db, member_id)
    locked = 0 if is_exited_member else get_member_locked_amount(db, member_id)
    available = max(0, total_savings - locked)

    # Build full savings periods view with statuses
    history = []
    if not is_exited_member:
        expected_periods = all_savings_periods()
        join = member['join_date']
        if isinstance(join, str):
            join_d = datetime.strptime(join, '%Y-%m-%d').date()
        else:
            join_d = join
        join_period = period_str(join_d)
        expected = [p for p in expected_periods if p >= join_period]

        paid_map = {s['period']: s for s in savings}
        for p in expected:
            s = paid_map.get(p)
            history.append({
                'period': p,
                'label': period_label(p),
                'amount': s['amount'] if s else 0,
                'paid': bool(s),
                'payment_date': s['payment_date'] if s else None,
            })

    return render_template(
        'admin/member_detail.html',
        member=member, savings=savings, loans=loans,
        fines=fines, fees=fees, user_row=user_row,
        total_savings=total_savings, locked=locked, available=available,
        is_exited_member=is_exited_member,
        history=history,
    )


@bp.route('/members/<int:member_id>/edit', methods=['GET', 'POST'])
@admin_required
def member_edit(member_id):
    db = get_db()
    member = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))

    if request.method == 'POST':
        # Personal
        surname     = (request.form.get('surname') or '').strip().upper()
        first_name  = (request.form.get('first_name') or '').strip().upper()
        other_names = (request.form.get('other_names') or '').strip().upper()
        full_name   = f"{first_name} {surname}".strip()
        gender      = request.form.get('gender') or ''
        date_of_birth    = request.form.get('date_of_birth') or None
        nationality      = (request.form.get('nationality') or 'Ugandan').strip()
        marital_status   = request.form.get('marital_status') or ''
        national_id      = (request.form.get('national_id') or '').strip()
        tin              = (request.form.get('tin') or '').strip()
        # Contact
        phone          = (request.form.get('phone') or '').strip()
        alt_phone      = (request.form.get('alt_phone') or '').strip()
        whatsapp_no    = (request.form.get('whatsapp_no') or '').strip()
        email          = (request.form.get('email') or '').strip()
        district       = (request.form.get('district') or 'Kampala').strip()
        village_street = (request.form.get('village_street') or '').strip()
        # Occupation
        employment_status = request.form.get('employment_status') or ''
        occupation        = (request.form.get('occupation') or '').strip()
        employer_business = (request.form.get('employer_business') or '').strip()
        monthly_income_raw = request.form.get('monthly_income') or ''
        monthly_income = float(monthly_income_raw) if monthly_income_raw else None
        # Contributions
        monthly_contribution_raw = request.form.get('monthly_contribution') or '100000'
        try:
            monthly_contribution = float(monthly_contribution_raw)
        except ValueError:
            monthly_contribution = 100000.0
        preferred_pay_date_raw = request.form.get('preferred_pay_date') or '5'
        try:
            preferred_pay_date = int(preferred_pay_date_raw)
        except ValueError:
            preferred_pay_date = 5
        mobile_money_no = (request.form.get('mobile_money_no') or '').strip()
        bank_account_no = (request.form.get('bank_account_no') or '').strip()
        # NOK
        next_of_kin      = (request.form.get('next_of_kin') or '').strip()
        nok_relationship = (request.form.get('nok_relationship') or '').strip()
        nok_phone        = (request.form.get('nok_phone') or '').strip()
        nok_address      = (request.form.get('nok_address') or '').strip()
        # Membership
        role             = request.form.get('role') or member['role']
        status           = request.form.get('status') or member['status']
        join_date        = request.form.get('join_date') or member['join_date']
        share_account_no = (request.form.get('share_account_no') or '').strip()
        notes            = request.form.get('notes') or ''

        if monthly_contribution < 100000:
            flash('Monthly contribution must be at least UGX 100,000.', 'danger')
            return render_template('admin/member_form.html', member=member,
                                   roles=Config.ROLES, today=date.today())

        if role not in Config.ROLES:
            flash('Invalid role.', 'danger')
            return render_template('admin/member_form.html', member=member,
                                   roles=Config.ROLES, today=date.today())
        if role == 'IT_ADMIN' and member['role'] != 'IT_ADMIN' \
                and session.get('role') != 'IT_ADMIN':
            flash('Only the IT Administrator can assign IT_ADMIN role.', 'danger')
            return render_template('admin/member_form.html', member=member,
                                   roles=Config.ROLES, today=date.today())

        # File uploads — only replace if a new file was provided
        photo_url    = _save_upload('photo', 'photos') or member['photo_url']
        nid_copy_url = _save_upload('nid_copy', 'nid_copies') or member['nid_copy_url']

        db.execute(
            """UPDATE members SET
                  full_name=?, surname=?, first_name=?, other_names=?,
                  gender=?, date_of_birth=?, nationality=?, marital_status=?,
                  national_id=?, tin=?, nid_copy_url=?,
                  phone=?, alt_phone=?, whatsapp_no=?, email=?, district=?, village_street=?,
                  employment_status=?, occupation=?, employer_business=?, monthly_income=?,
                  monthly_contribution=?, preferred_pay_date=?, mobile_money_no=?, bank_account_no=?,
                  next_of_kin=?, nok_relationship=?, nok_phone=?, nok_address=?,
                  role=?, join_date=?, share_account_no=?, status=?, notes=?,
                  photo_url=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (full_name, surname, first_name, other_names,
             gender, date_of_birth, nationality, marital_status,
             national_id, tin, nid_copy_url,
             phone, alt_phone, whatsapp_no, email, district, village_street,
             employment_status, occupation, employer_business, monthly_income,
             monthly_contribution, preferred_pay_date, mobile_money_no, bank_account_no,
             next_of_kin, nok_relationship, nok_phone, nok_address,
             role, join_date, share_account_no, status, notes,
             photo_url, member_id),
        )
        db.commit()
        log_action('UPDATE_MEMBER', 'member', member_id,
                   f"Updated {member['member_no']}")
        _notify_member(
            db,
            member_id,
            'Profile Updated',
            'Your member profile details were updated by administration.',
            url_for('member.profile'),
        )
        db.commit()
        flash('Member updated.', 'success')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    return render_template('admin/member_form.html', member=member,
                           roles=Config.ROLES, today=date.today())


@bp.route('/members/<int:member_id>/delete', methods=['POST'])
@admin_required
def member_delete(member_id):
    db = get_db()
    member = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))

    if member_id == session.get('member_id'):
        flash('You cannot remove your own member record while logged in.', 'danger')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    result = _remove_member_record(db, member)
    db.commit()
    log_action('DELETE_MEMBER', 'member', member_id,
               f"{result['mode'].capitalize()} member {member['member_no']}")
    flash(result['message'], 'success')
    return redirect(url_for('admin.members_list'))


@bp.route('/members/<int:member_id>/login', methods=['POST'])
@admin_required
def member_create_login(member_id):
    db = get_db()
    member = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))

    existing = db.execute("SELECT * FROM users WHERE member_id = ?",
                          (member_id,)).fetchone()
    if existing:
        flash('This member already has a login.', 'warning')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    username = (request.form.get('username') or '').strip().lower()
    password = request.form.get('password') or 'gazebo123'

    if not username:
        username = member['full_name'].split()[0].lower()
        base = username
        i = 1
        while db.execute("SELECT 1 FROM users WHERE username = ?",
                         (username,)).fetchone():
            i += 1
            username = f"{base}{i}"

    if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        flash('Username already exists.', 'danger')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    db.execute(
        """INSERT INTO users (username, password_hash, member_id, must_change_pw)
           VALUES (?, ?, ?, 1)""",
        (username, generate_password_hash(password), member_id),
    )
    db.commit()
    log_action('CREATE_LOGIN', 'user', member_id,
               f"Created login {username} for {member['member_no']}")
    flash(f"Login created. Username: {username}, Password: {password}", 'success')
    return redirect(url_for('admin.member_detail', member_id=member_id))


@bp.route('/members/<int:member_id>/reset-password', methods=['POST'])
@admin_required
def member_reset_password(member_id):
    db = get_db()
    new_pw = request.form.get('new_password') or 'gazebo123'
    db.execute(
        """UPDATE users SET password_hash=?, must_change_pw=1
           WHERE member_id = ?""",
        (generate_password_hash(new_pw), member_id),
    )
    db.commit()
    log_action('RESET_PASSWORD', 'user', member_id,
               f"Password reset for member {member_id}")
    flash(f'Password reset to "{new_pw}". Member must change on first login.',
          'success')
    return redirect(url_for('admin.member_detail', member_id=member_id))


# ===========================================================================
# SAVINGS
# ===========================================================================
def _add_month_period(period, months=1):
    base = datetime.strptime(period, '%Y-%m')
    return (base + relativedelta(months=months)).strftime('%Y-%m')


def _next_unallocated_periods(db, member_id, units):
    """Get the next N unpaid periods for a member, starting from club start (Aug-2025)."""
    if units <= 0:
        return []

    taken = {
        r['period'] for r in db.execute(
            "SELECT period FROM savings WHERE member_id = ?",
            (member_id,),
        ).fetchall()
    }

    periods = []
    cur = Config.SAVINGS_START_PERIOD
    guard = 0
    while len(periods) < units and guard < 1000:
        if cur not in taken:
            periods.append(cur)
        cur = _add_month_period(cur, 1)
        guard += 1
    return periods


@bp.route('/savings')
@admin_required
def savings_list():
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()
    view = (request.args.get('view') or 'overview').lower()
    if view not in {'overview', 'ledger', 'calendar', 'amendments'}:
        view = 'overview'

    member_scope = (request.args.get('member_scope') or 'active').lower()
    if member_scope not in {'active', 'non_active'}:
        member_scope = 'active'

    scope_filter_sql = "m.status = 'Active'"
    scope_member_sql = "status = 'Active'"
    scope_label = 'Active'
    if member_scope == 'non_active':
        scope_filter_sql = "m.status != 'Active' AND m.status != 'Exited' AND m.status != 'Archived'"
        scope_member_sql = "status != 'Active' AND status != 'Exited' AND status != 'Archived'"
        scope_label = 'Non-Active'

    cur_period = period_str(date.today())
    periods_to_date = all_savings_periods()

    active_members = db.execute(
        """SELECT * FROM members
            WHERE status = 'Active'
            ORDER BY member_no"""
    ).fetchall()
    active_count = len(active_members)

    total_savings_all = db.execute(
        """SELECT COALESCE(SUM(s.amount), 0) AS s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE """ + scope_filter_sql
    ).fetchone()['s']

    scope_count = db.execute(
        "SELECT COUNT(*) c FROM members WHERE " + scope_member_sql
    ).fetchone()['c']
    expected_to_date = scope_count * len(periods_to_date) * Config.MONTHLY_SAVINGS_AMOUNT

    member_totals = db.execute(
        """SELECT m.id, m.member_no, m.full_name, m.status,
                  COALESCE(SUM(s.amount), 0) AS total_savings,
                  COUNT(s.id) AS months_covered
             FROM members m
             LEFT JOIN savings s ON s.member_id = m.id
            WHERE m.status NOT IN ('Exited', 'Archived') AND """ + scope_filter_sql + """
            GROUP BY m.id, m.member_no, m.full_name, m.status
            ORDER BY m.member_no"""
    ).fetchall()

    # Ledger data
    members_for_ledger = db.execute(
        """SELECT id, member_no, full_name, status
             FROM members
            WHERE """ + scope_member_sql + """
            ORDER BY member_no"""
    ).fetchall()
    selected_member_id = request.args.get('member_id', type=int)
    allowed_ids = {m['id'] for m in members_for_ledger}
    if selected_member_id and selected_member_id not in allowed_ids:
        selected_member_id = None
    if not selected_member_id and members_for_ledger:
        selected_member_id = members_for_ledger[0]['id']

    selected_member = None
    ledger_rows = []
    ledger_total = 0
    ledger_months = 0
    if selected_member_id:
        selected_member = db.execute(
            "SELECT * FROM members WHERE id = ?",
            (selected_member_id,),
        ).fetchone()
        ledger_rows = db.execute(
            """SELECT * FROM savings
                 WHERE member_id = ?
                 ORDER BY period DESC""",
            (selected_member_id,),
        ).fetchall()
        ledger_total = sum(r['amount'] for r in ledger_rows)
        ledger_months = len(ledger_rows)

    # Calendar data
    start_year = int(Config.SAVINGS_START_PERIOD.split('-')[0])
    max_paid_period = db.execute("SELECT MAX(period) AS p FROM savings").fetchone()['p']
    max_paid_year = int(max_paid_period[:4]) if max_paid_period else start_year
    current_year = date.today().year
    year = request.args.get('year', type=int) or current_year
    year = max(start_year, min(year, max(current_year + 5, max_paid_year)))
    years = list(range(start_year, max(current_year + 2, max_paid_year) + 1))

    start_month = int(Config.SAVINGS_START_PERIOD.split('-')[1])
    first_month = start_month if year == start_year else 1
    month_periods = [f"{year}-{m:02d}" for m in range(first_month, 13)]
    calendar_members = db.execute(
        """SELECT id, member_no, full_name
             FROM members
            WHERE status = 'Active'
            ORDER BY member_no"""
    ).fetchall()
    paid_rows = db.execute(
        """SELECT member_id, period, amount
             FROM savings
            WHERE SUBSTR(period, 1, 4) = ?""",
        (str(year),),
    ).fetchall()
    paid_map = {}
    for r in paid_rows:
        paid_map.setdefault(r['member_id'], {})[r['period']] = r['amount']

    calendar_rows = []
    for m in calendar_members:
        cells = []
        member_paid = paid_map.get(m['id'], {})
        for p in month_periods:
            amt = member_paid.get(p)
            if amt is not None:
                status = 'paid'
            elif p <= cur_period:
                status = 'missed'
            else:
                status = 'future'
            cells.append({'period': p, 'status': status, 'amount': amt})
        calendar_rows.append({'member': m, 'cells': cells})

    # Amendments (latest raw entries for correction/delete review)
    amendments_rows = db.execute(
        """SELECT s.*, m.member_no, m.full_name, u.username AS recorder_name
             FROM savings s
             JOIN members m ON m.id = s.member_id
             LEFT JOIN users u ON u.id = s.recorded_by
            WHERE """ + scope_filter_sql + """
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT 200"""
    ).fetchall()

    non_active_count = db.execute(
        "SELECT COUNT(*) c FROM members WHERE status != 'Active' AND status != 'Exited' AND status != 'Archived'"
    ).fetchone()['c']

    return render_template(
        'admin/savings_list.html',
        view=view,
        member_scope=member_scope,
        scope_label=scope_label,
        scope_count=scope_count,
        non_active_count=non_active_count,
        total_savings_all=total_savings_all,
        expected_to_date=expected_to_date,
        active_count=active_count,
        member_totals=member_totals,
        members_for_ledger=members_for_ledger,
        selected_member_id=selected_member_id,
        selected_member=selected_member,
        ledger_rows=ledger_rows,
        ledger_total=ledger_total,
        ledger_months=ledger_months,
        year=year,
        years=years,
        month_periods=month_periods,
        calendar_rows=calendar_rows,
        amendments_rows=amendments_rows,
    )


@bp.route('/savings/record', methods=['GET', 'POST'])
@role_required('IT_ADMIN', 'TREASURER')
def savings_record():
    db = get_db()
    if request.method == 'POST':
        member_id = int(request.form.get('member_id'))
        amount = int(request.form.get('amount') or Config.MONTHLY_SAVINGS_AMOUNT)
        payment_date = request.form.get('payment_date') or date.today().isoformat()
        method = request.form.get('payment_method') or 'Bank Transfer'
        ref = request.form.get('reference_no') or ''
        notes = request.form.get('notes') or ''

        if amount < Config.MONTHLY_SAVINGS_AMOUNT:
            flash(f"Minimum deposit is {fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)}.", 'danger')
            return redirect(url_for('admin.savings_record'))

        months_to_cover = amount // Config.MONTHLY_SAVINGS_AMOUNT
        remainder = amount % Config.MONTHLY_SAVINGS_AMOUNT
        allocation_periods = _next_unallocated_periods(db, member_id, months_to_cover)

        if not allocation_periods:
            flash('No allocation periods found for this member.', 'warning')
            return redirect(url_for('admin.savings_record'))

        for p in allocation_periods:
            period_eom = end_of_month(f"{p}-01").isoformat()
            db.execute(
                """INSERT INTO savings
                   (member_id, period, amount, payment_date,
                    payment_method, reference_no, notes, recorded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (member_id, p, Config.MONTHLY_SAVINGS_AMOUNT,
                 period_eom, method, ref, notes, session['user_id']),
            )

        db.commit()
        m = db.execute("SELECT member_no FROM members WHERE id=?",
                       (member_id,)).fetchone()
        first_p = allocation_periods[0]
        last_p = allocation_periods[-1]
        log_action(
            'RECORD_SAVINGS',
            'savings',
            member_id,
            f"Allocated {fmt_money(amount)} for {m['member_no']} from {first_p} to {last_p}",
        )
        _notify_member(
            db,
            member_id,
            'Savings Deposit Recorded',
            f"A savings deposit of {fmt_money(amount)} was posted to your account covering {first_p} to {last_p}.",
            url_for('member.savings_statement'),
        )
        db.commit()

        msg = (
            f"Deposit allocated: {len(allocation_periods)} month(s)"
            f" ({first_p} to {last_p}) at {fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)} each."
        )
        if remainder:
            msg += f" Unallocated balance: {fmt_money(remainder)} (below one full month)."
            flash(msg, 'warning')
        else:
            flash(msg, 'success')
        return redirect(url_for('admin.savings_list', view='ledger', member_id=member_id))

    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    return render_template(
        'admin/savings_record.html',
        members=members,
        default_amount=Config.MONTHLY_SAVINGS_AMOUNT,
    )


@bp.route('/savings/bulk', methods=['GET', 'POST'])
@role_required('IT_ADMIN', 'TREASURER')
def savings_bulk():
    """Bulk-record savings for many members in a single period."""
    db = get_db()
    if request.method == 'POST':
        period = request.form.get('period')
        payment_date = request.form.get('payment_date') or date.today().isoformat()
        member_ids = request.form.getlist('member_ids')
        if not period or not member_ids:
            flash('Select a period and at least one member.', 'warning')
            return redirect(url_for('admin.savings_bulk'))

        recorded = 0
        notified_member_ids = []
        for mid in member_ids:
            mid = int(mid)
            if db.execute(
                "SELECT 1 FROM savings WHERE member_id=? AND period=?",
                (mid, period),
            ).fetchone():
                continue  # skip already paid
            db.execute(
                """INSERT INTO savings
                   (member_id, period, amount, payment_date, payment_method, recorded_by)
                   VALUES (?, ?, ?, ?, 'Cash', ?)""",
                (mid, period, Config.MONTHLY_SAVINGS_AMOUNT,
                 payment_date, session['user_id']),
            )
            recorded += 1
            notified_member_ids.append(mid)
        for mid in notified_member_ids:
            _notify_member(
                db,
                mid,
                'Savings Deposit Recorded',
                f"A savings deposit for {period_label(period)} ({fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)}) was posted to your account.",
                url_for('member.savings_statement'),
            )
        db.commit()
        log_action('BULK_SAVINGS', 'savings', None,
                   f"Bulk recorded {recorded} payments for {period}")
        flash(f'{recorded} savings entries recorded.', 'success')
        return redirect(url_for('admin.savings_list', view='amendments'))

    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    periods = all_savings_periods()
    cur_period = period_str(date.today())
    return render_template(
        'admin/savings_bulk.html',
        members=members, periods=periods, default_period=cur_period,
    )


@bp.route('/savings/<int:savings_id>/delete', methods=['POST'])
@role_required('IT_ADMIN', 'TREASURER')
def savings_delete(savings_id):
    db = get_db()
    s = db.execute("SELECT * FROM savings WHERE id=?", (savings_id,)).fetchone()
    if s:
        db.execute("DELETE FROM savings WHERE id=?", (savings_id,))
        db.commit()
        log_action('DELETE_SAVINGS', 'savings', savings_id,
                   f"Deleted savings {s['period']} for member {s['member_id']}")
        _notify_member(
            db,
            s['member_id'],
            'Savings Entry Reversed',
            f"A savings entry for {s['period']} ({fmt_money(s['amount'])}) was removed by admin.",
            url_for('member.savings_statement'),
        )
        db.commit()
        flash('Savings entry deleted.', 'success')
    return redirect(request.referrer or url_for('admin.savings_list', view='amendments'))


# ===========================================================================
# LOANS
# ===========================================================================
@bp.route('/loans')
@admin_required
def loans_list():
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()
    status = request.args.get('status') or ''
    sort_by = request.args.get('sort') or 'overdue'
    sort_dir = request.args.get('dir') or 'desc'  # 'asc' or 'desc'
    pool = request.args.get('pool') or ''

    sql = """SELECT l.*, m.member_no, m.full_name AS borrower_name,
                    m1.full_name AS g1_name, m2.full_name AS g2_name
               FROM loans l
               JOIN members m ON m.id = l.member_id
               LEFT JOIN members m1 ON m1.id = l.guarantor1_id
               LEFT JOIN members m2 ON m2.id = l.guarantor2_id
              WHERE 1=1"""
    params = []
    if status:
        sql += " AND l.status = ?"
        params.append(status)
    sql += " ORDER BY l.issued_date DESC, l.id DESC"
    loans = db.execute(sql, params).fetchall()

    all_loans = db.execute(
        """SELECT l.*, m.member_no, m.full_name AS borrower_name,
                  m1.full_name AS g1_name, m2.full_name AS g2_name
             FROM loans l
             JOIN members m ON m.id = l.member_id
             LEFT JOIN members m1 ON m1.id = l.guarantor1_id
             LEFT JOIN members m2 ON m2.id = l.guarantor2_id
            ORDER BY l.issued_date DESC, l.id DESC"""
    ).fetchall()

    enriched = []
    for loan in loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        d = dict(loan)
        d.update(pos)
        enriched.append(d)

    # Optional pool-card filter from dashboard tiles
    if pool == 'deployed_active':
        enriched = [l for l in enriched if l['status'] == 'Active']
    elif pool == 'interest_pending_active':
        enriched = [
            l for l in enriched
            if l['status'] == 'Active' and (l['outstanding_interest'] + l['outstanding_penalty']) > 0
        ]
    elif pool == 'total_loans_interest':
        enriched = [l for l in enriched if (l['contract_interest'] + l['recorded_penalty']) > 0]
    elif pool == 'principal_all_loans':
        enriched = [l for l in enriched if int(l['principal']) > 0]
    elif pool == 'closed_loans_count':
        enriched = [l for l in enriched if l['status'] == 'Cleared']
    elif pool == 'interest_on_closed':
        enriched = [l for l in enriched if l['status'] == 'Cleared' and l['paid_interest'] > 0]

    if sort_by == 'outstanding':
        enriched.sort(key=lambda x: x['total_outstanding'], reverse=(sort_dir != 'asc'))
    elif sort_by == 'due':
        enriched.sort(key=lambda x: (x['due_date'] or '9999-12-31', -x['total_outstanding']),
                      reverse=(sort_dir == 'asc'))
    elif sort_by == 'newest':
        enriched.sort(key=lambda x: ((x['issued_date'] or '0000-00-00'), x['id']),
                      reverse=(sort_dir != 'asc'))
    elif sort_by == 'principal':
        enriched.sort(key=lambda x: int(x['principal']), reverse=(sort_dir != 'asc'))
    elif sort_by == 'interest':
        enriched.sort(key=lambda x: x['contract_interest'], reverse=(sort_dir != 'asc'))
    elif sort_by == 'rate':
        enriched.sort(key=lambda x: float(x['interest_rate']), reverse=(sort_dir != 'asc'))
    elif sort_by == 'status':
        enriched.sort(key=lambda x: x['status'] or '', reverse=(sort_dir == 'desc'))
    elif sort_by == 'borrower':
        enriched.sort(key=lambda x: (x['borrower_name'] or '').lower(), reverse=(sort_dir == 'desc'))
    elif sort_by == 'loan_no':
        enriched.sort(key=lambda x: x['loan_no'] or '', reverse=(sort_dir == 'desc'))
    else:  # default: overdue first
        enriched.sort(
            key=lambda x: (
                0 if x['is_overdue'] else 1,
                x['due_date'] or '9999-12-31',
                -x['total_outstanding'],
            )
        )

    all_enriched = []
    for loan in all_loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        d = dict(loan)
        d.update(pos)
        all_enriched.append(d)

    total_savings = db.execute(
        """SELECT COALESCE(SUM(s.amount),0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE m.status = 'Active'"""
    ).fetchone()['s'] or 0
    deployed_active = sum(l['outstanding_principal'] for l in all_enriched if l['status'] == 'Active')
    interest_pending_active_base = sum(
        l['outstanding_interest'] for l in all_enriched if l['status'] == 'Active'
    )
    interest_pending_active_penalty = sum(
        l['outstanding_penalty'] for l in all_enriched if l['status'] == 'Active'
    )
    interest_pending_active = interest_pending_active_base + interest_pending_active_penalty
    total_loans_interest_base = sum(l['contract_interest'] for l in all_enriched)
    total_loans_interest_penalty = sum(l['recorded_penalty'] for l in all_enriched)
    total_loans_interest = total_loans_interest_base + total_loans_interest_penalty
    principal_all_loans = sum(int(l['principal']) for l in all_enriched)
    closed_loans_count = sum(1 for l in all_enriched if l['status'] == 'Cleared')
    interest_on_closed = sum(l['paid_interest'] for l in all_enriched if l['status'] == 'Cleared')
    available_to_deploy = max(0, total_savings - deployed_active)
    pool_utilization = round((deployed_active / total_savings) * 100, 1) if total_savings else 0

    return render_template(
        'admin/loans_list.html',
        loans=enriched,
        status=status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        pool=pool,
        statuses=Config.LOAN_STATUSES,
        loan_pools={
            'available_to_deploy': available_to_deploy,
            'deployed_active': deployed_active,
            'interest_pending_active': interest_pending_active,
            'interest_pending_active_base': interest_pending_active_base,
            'interest_pending_active_penalty': interest_pending_active_penalty,
            'total_loans_interest': total_loans_interest,
            'total_loans_interest_base': total_loans_interest_base,
            'total_loans_interest_penalty': total_loans_interest_penalty,
            'principal_all_loans': principal_all_loans,
            'closed_loans_count': closed_loans_count,
            'interest_on_closed': interest_on_closed,
            'pool_utilization': pool_utilization,
            'total_savings': total_savings,
        },
    )


@bp.route('/loans/new', methods=['GET', 'POST'])
@role_required('IT_ADMIN', 'TREASURER')
def loan_create():
    db = get_db()
    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    # enrich with available savings
    enriched_members = []
    for m in members:
        avail = get_member_available_savings(db, m['id'])
        d = dict(m)
        d['available_savings'] = avail
        enriched_members.append(d)

    if request.method == 'POST':
        member_id = int(request.form.get('member_id'))
        principal = int(request.form.get('principal') or 0)
        term_months = int(request.form.get('term_months') or 1)
        purpose = (request.form.get('purpose') or '').strip()
        guarantor1_id = request.form.get('guarantor1_id')
        guarantor2_id = request.form.get('guarantor2_id')
        issued_date = request.form.get('issued_date') or date.today().isoformat()
        capture_mode = request.form.get('capture_mode') or 'workflow_pending'
        disbursed_date = (request.form.get('disbursed_date') or '').strip()
        app_file = _save_loan_application('application_file')

        # Validation
        errors = []
        if principal <= 0:
            errors.append("Principal must be positive.")
        if term_months < 1 or term_months > Config.LOAN_MAX_TERM_MONTHS:
            errors.append(f"Term must be between 1 and "
                          f"{Config.LOAN_MAX_TERM_MONTHS} months.")
        if guarantor1_id:
            guarantor1_id = int(guarantor1_id)
        else:
            guarantor1_id = None
        if guarantor2_id:
            guarantor2_id = int(guarantor2_id)
        else:
            guarantor2_id = None
        if not guarantor1_id or not guarantor2_id:
            errors.append("Two guarantors are always required for any loan.")
        if guarantor1_id == member_id or guarantor2_id == member_id:
            errors.append("A member cannot guarantee their own loan.")
        if guarantor1_id and guarantor2_id and guarantor1_id == guarantor2_id:
            errors.append("Both guarantors must be distinct members.")
        if not app_file:
            errors.append("Please upload the signed loan application (PDF/JPG/PNG/WEBP).")

        # Legacy/manual import option for old already-disbursed loans
        import_as_active = (capture_mode == 'legacy_active')
        disbursed_dt = None
        if import_as_active:
            if not disbursed_date:
                errors.append("Disbursed date is required for historical manual import.")
            else:
                try:
                    disbursed_dt = datetime.strptime(disbursed_date, '%Y-%m-%d').date()
                    if disbursed_dt > date.today():
                        errors.append("Disbursed date cannot be in the future.")
                except ValueError:
                    errors.append("Invalid disbursed date format.")

        # Check open loans
        open_loan = db.execute(
            "SELECT 1 FROM loans WHERE member_id=? AND status IN ('Pending','Active')",
            (member_id,),
        ).fetchone()
        if open_loan:
            errors.append("This member already has an active or pending loan.")

        # Determine collateral
        sec = determine_loan_security(db, member_id, principal,
                                      guarantor1_id, guarantor2_id)
        if not sec['sufficient']:
            errors.append(sec['message'])

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('admin/loan_form.html',
                                   members=enriched_members,
                                   max_term=Config.LOAN_MAX_TERM_MONTHS,
                                   rate_pct=int(Config.LOAN_INTEREST_RATE_MONTHLY * 100))

        if import_as_active and disbursed_dt is not None:
            # Historical import: due by exact month anniversary from disbursed date.
            due_date = (disbursed_dt + relativedelta(months=term_months)).isoformat()
        else:
            due_date = calculate_due_date(issued_date, term_months)
        loan_no = next_loan_no(db)

        initial_status = 'Active' if import_as_active else 'Pending'
        initial_approved_date = disbursed_date if import_as_active else None
        initial_disbursed_date = disbursed_date if import_as_active else None
        initial_approved_by = session['user_id'] if import_as_active else None

        db.execute(
            """INSERT INTO loans
               (loan_no, member_id, principal, interest_rate, term_months,
                purpose, issued_date, approved_date, disbursed_date, due_date,
                status, guarantor1_id, guarantor2_id,
                g1_locked_amount, g2_locked_amount, self_locked_amount,
                approved_by, application_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (loan_no, member_id, principal, Config.LOAN_INTEREST_RATE_MONTHLY,
             term_months, purpose, issued_date, initial_approved_date, initial_disbursed_date,
             due_date, initial_status, guarantor1_id, guarantor2_id,
             sec['g1_locked'], sec['g2_locked'], sec['self_locked'], initial_approved_by, app_file),
        )
        db.commit()
        loan_id = db.execute("SELECT last_insert_rowid() AS i").fetchone()['i']

        if import_as_active:
            # Immediately assess any missed overdue penalties for old imported loans
            _auto_apply_penalties(db, loan_id)
        else:
            _notify_roles(
                db,
                ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
                'Loan approval required',
                f"{loan_no} needs 3 administrative approvals.",
                url_for('admin.loan_detail', loan_id=loan_id),
                exclude_user_id=None,
            )
            db.commit()

        log_action('CREATE_LOAN', 'loan', loan_id,
                   f"Created loan {loan_no} {fmt_money(principal)} for member {member_id}")
        if import_as_active:
            flash(f'Historical loan {loan_no} imported as Active using disbursed date {disbursed_date}.', 'success')
        else:
            flash(f'Loan {loan_no} submitted. Waiting for 3 approvals.', 'success')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    return render_template(
        'admin/loan_form.html',
        members=enriched_members,
        max_term=Config.LOAN_MAX_TERM_MONTHS,
        rate_pct=int(Config.LOAN_INTEREST_RATE_MONTHLY * 100),
    )


@bp.route('/loans/<int:loan_id>')
@admin_required
def loan_detail(loan_id):
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()
    loan = db.execute(
        """SELECT l.*, m.full_name AS borrower_name, m.member_no, m.phone,
                  m1.full_name AS g1_name, m1.member_no AS g1_no,
                  m2.full_name AS g2_name, m2.member_no AS g2_no
             FROM loans l
             JOIN members m ON m.id = l.member_id
             LEFT JOIN members m1 ON m1.id = l.guarantor1_id
             LEFT JOIN members m2 ON m2.id = l.guarantor2_id
            WHERE l.id = ?""",
        (loan_id,),
    ).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))

    # Auto-apply any outstanding penalties before rendering
    _auto_apply_penalties(db, loan_id)

    repays = db.execute(
        "SELECT * FROM loan_repayments WHERE loan_id=? ORDER BY payment_date",
        (loan_id,),
    ).fetchall()
    pens = db.execute(
        "SELECT * FROM loan_penalties WHERE loan_id=? ORDER BY period",
        (loan_id,),
    ).fetchall()
    pos = calculate_loan_position(loan, repays, pens)

    new_approval_count = db.execute(
        """SELECT COUNT(DISTINCT user_id) c FROM loan_approvals
            WHERE loan_id=? AND stage='NEW' AND decision='APPROVE'""",
        (loan_id,),
    ).fetchone()['c']
    approved_by_me_new = db.execute(
        """SELECT 1 FROM loan_approvals
            WHERE loan_id=? AND stage='NEW' AND user_id=?""",
        (loan_id, session['user_id']),
    ).fetchone() is not None

    pending_amendment = db.execute(
        """SELECT * FROM loan_amendments
            WHERE loan_id=? AND status='Pending'
            ORDER BY id DESC LIMIT 1""",
        (loan_id,),
    ).fetchone()
    amendment_approval_count = 0
    approved_by_me_amendment = False
    if pending_amendment:
        amendment_approval_count = db.execute(
            """SELECT COUNT(DISTINCT user_id) c FROM loan_approvals
                WHERE loan_id=? AND amendment_id=? AND stage='AMENDMENT' AND decision='APPROVE'""",
            (loan_id, pending_amendment['id']),
        ).fetchone()['c']
        approved_by_me_amendment = db.execute(
            """SELECT 1 FROM loan_approvals
                WHERE loan_id=? AND amendment_id=? AND stage='AMENDMENT' AND user_id=?""",
            (loan_id, pending_amendment['id'], session['user_id']),
        ).fetchone() is not None

    approved_by_name = None
    if loan['approved_by']:
        approved_row = db.execute(
            """SELECT u.username, m.full_name
                 FROM users u
                 LEFT JOIN members m ON m.id = u.member_id
                WHERE u.id = ?""",
            (loan['approved_by'],),
        ).fetchone()
        if approved_row:
            approved_by_name = approved_row['full_name'] or approved_row['username']

    # All approvers (name, role, decision, timestamp) for this loan
    approvers_list = db.execute(
        """SELECT COALESCE(m.full_name, u.username) AS name,
                  m.role, la.decision, la.stage, la.created_at
             FROM loan_approvals la
             JOIN users u ON u.id = la.user_id
             LEFT JOIN members m ON m.id = u.member_id
            WHERE la.loan_id = ?
            ORDER BY la.created_at""",
        (loan_id,),
    ).fetchall()

    total_repayable = int(loan['principal']) + pos['contract_interest'] + pos['recorded_penalty']
    monthly_installment = int(round(total_repayable / max(1, int(loan['term_months'] or 1))))
    paid_off_date = repays[-1]['payment_date'] if repays and pos['total_outstanding'] == 0 else None
    approval_progress_text = f"{new_approval_count}/3 approvals"
    if loan['status'] != 'Pending':
        approval_progress_text = 'Fully approved'

    total_savings = db.execute(
        """SELECT COALESCE(SUM(s.amount),0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE m.status = 'Active'"""
    ).fetchone()['s'] or 0
    active_loans = db.execute("SELECT * FROM loans WHERE status='Active'").fetchall()
    deployed_active = 0
    recovered = 0
    for l in active_loans:
        rs = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (l['id'],)).fetchall()
        ps = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (l['id'],)).fetchall()
        lp = calculate_loan_position(l, rs, ps)
        deployed_active += lp['outstanding_principal']
        recovered += lp['total_paid']
    projected_deployed = deployed_active + (int(loan['principal']) if loan['status'] == 'Pending' else 0)
    remaining_pool_after_loan = max(0, total_savings - projected_deployed)
    utilization = round((projected_deployed / total_savings) * 100, 1) if total_savings else 0

    schedule_rows = [
        {
            'idx': 1,
            'due': loan['due_date'],
            'amount': total_repayable,
            'status': 'Paid' if pos['total_outstanding'] == 0 else ('Overdue' if pos['is_overdue'] else 'Pending'),
            'paid': pos['total_paid'],
        }
    ]

    repayment_progress = 0
    if total_repayable > 0:
        repayment_progress = min(100, int(round((pos['total_paid'] / total_repayable) * 100)))

    return render_template(
        'admin/loan_detail.html',
        loan=loan, repays=repays, penalties=pens, pos=pos,
        new_approval_count=new_approval_count,
        approved_by_me_new=approved_by_me_new,
        pending_amendment=pending_amendment,
        amendment_approval_count=amendment_approval_count,
        approved_by_me_amendment=approved_by_me_amendment,
        approvers_list=approvers_list,
        detail_meta={
            'approved_by_name': approved_by_name,
            'total_repayable': total_repayable,
            'monthly_installment': monthly_installment,
            'paid_off_date': paid_off_date,
            'approval_progress_text': approval_progress_text,
            'remaining_pool_after_loan': remaining_pool_after_loan,
            'active_pool': total_savings,
            'projected_deployed': projected_deployed,
            'recovered': recovered,
            'utilization': utilization,
            'repayment_progress': repayment_progress,
            'schedule_rows': schedule_rows,
        },
    )


@bp.route('/loans/<int:loan_id>/delete', methods=['POST'])
@role_required('IT_ADMIN')
def loan_delete(loan_id):
    """Permanently delete a loan and all related records. IT_ADMIN only."""
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))
    loan_no = loan['loan_no']
    db.execute("DELETE FROM loan_repayments WHERE loan_id=?", (loan_id,))
    db.execute("DELETE FROM loan_penalties WHERE loan_id=?", (loan_id,))
    db.execute("DELETE FROM loan_approvals WHERE loan_id=?", (loan_id,))
    db.execute("DELETE FROM loan_amendments WHERE loan_id=?", (loan_id,))
    db.execute("DELETE FROM loans WHERE id=?", (loan_id,))
    db.commit()
    log_action('DELETE_LOAN', 'loan', loan_id, f"Deleted loan {loan_no} by IT_ADMIN")
    flash(f'Loan {loan_no} and all related records permanently deleted.', 'warning')
    return redirect(url_for('admin.loans_list'))


@bp.route('/loans/<int:loan_id>/application', methods=['POST'])
@role_required('IT_ADMIN', 'TREASURER')
def loan_application_upload(loan_id):
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))

    app_file = _save_loan_application('application_file')
    if not app_file:
        flash('Upload failed. Please attach PDF/JPG/PNG/WEBP file.', 'danger')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    db.execute(
        "UPDATE loans SET application_file=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (app_file, loan_id),
    )
    db.commit()
    log_action('UPLOAD_LOAN_FORM', 'loan', loan_id, f"Uploaded/updated signed form for {loan['loan_no']}")
    flash('Signed loan form uploaded.', 'success')
    return redirect(url_for('admin.loan_detail', loan_id=loan_id))


@bp.route('/loans/<int:loan_id>/edit', methods=['GET', 'POST'])
@role_required('IT_ADMIN', 'TREASURER')
def loan_edit(loan_id):
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))

    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    enriched_members = []
    for m in members:
        avail = get_member_available_savings(db, m['id'])
        d = dict(m)
        d['available_savings'] = avail
        enriched_members.append(d)

    if request.method == 'POST':
        member_id = int(request.form.get('member_id'))
        principal = int(request.form.get('principal') or 0)
        term_months = int(request.form.get('term_months') or 1)
        purpose = (request.form.get('purpose') or '').strip()
        guarantor1_id = request.form.get('guarantor1_id')
        guarantor2_id = request.form.get('guarantor2_id')
        issued_date = request.form.get('issued_date') or date.today().isoformat()
        reason = (request.form.get('reason') or '').strip()

        if guarantor1_id:
            guarantor1_id = int(guarantor1_id)
        else:
            guarantor1_id = None
        if guarantor2_id:
            guarantor2_id = int(guarantor2_id)
        else:
            guarantor2_id = None

        errors = []
        if principal <= 0:
            errors.append("Principal must be positive.")
        if term_months < 1 or term_months > Config.LOAN_MAX_TERM_MONTHS:
            errors.append(f"Term must be between 1 and {Config.LOAN_MAX_TERM_MONTHS} months.")
        if not guarantor1_id or not guarantor2_id:
            errors.append("Two guarantors are always required for any loan.")
        if guarantor1_id == member_id or guarantor2_id == member_id:
            errors.append("A member cannot guarantee their own loan.")
        if guarantor1_id and guarantor2_id and guarantor1_id == guarantor2_id:
            errors.append("Both guarantors must be distinct members.")

        sec = determine_loan_security(db, member_id, principal, guarantor1_id, guarantor2_id)
        if not sec['sufficient']:
            errors.append(sec['message'])

        app_file = _save_loan_application('application_file')
        application_file = app_file or loan['application_file']
        if not application_file:
            errors.append('Signed loan application file is required.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template(
                'admin/loan_form.html',
                members=enriched_members,
                max_term=Config.LOAN_MAX_TERM_MONTHS,
                rate_pct=int(Config.LOAN_INTEREST_RATE_MONTHLY * 100),
                loan=loan,
                edit_mode=True,
            )

        # Keep legacy historical loans consistent: if already disbursed, due date follows
        # exact month-anniversary from disbursed date; otherwise use standard rule.
        if loan['status'] == 'Active' and loan['disbursed_date']:
            disbursed_base = loan['disbursed_date']
            if isinstance(disbursed_base, str):
                disbursed_base = datetime.strptime(disbursed_base, '%Y-%m-%d').date()
            due_date = (disbursed_base + relativedelta(months=term_months)).isoformat()
        else:
            due_date = calculate_due_date(issued_date, term_months)

        # Pending loans can be edited directly by maker roles.
        if loan['status'] == 'Pending':
            db.execute(
                """UPDATE loans SET
                    member_id=?, principal=?, term_months=?, purpose=?, issued_date=?, due_date=?,
                    guarantor1_id=?, guarantor2_id=?,
                    g1_locked_amount=?, g2_locked_amount=?, self_locked_amount=?,
                    application_file=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    member_id, principal, term_months, purpose, issued_date, due_date,
                    guarantor1_id, guarantor2_id,
                    sec['g1_locked'], sec['g2_locked'], sec['self_locked'],
                    application_file, loan_id,
                ),
            )
            db.commit()
            log_action('EDIT_PENDING_LOAN', 'loan', loan_id, f"Edited pending loan {loan['loan_no']}")
            flash('Pending loan updated.', 'success')
            return redirect(url_for('admin.loan_detail', loan_id=loan_id))

        payload = {
            'member_id': member_id,
            'principal': principal,
            'term_months': term_months,
            'purpose': purpose,
            'issued_date': issued_date,
            'due_date': due_date,
            'guarantor1_id': guarantor1_id,
            'guarantor2_id': guarantor2_id,
            'g1_locked_amount': sec['g1_locked'],
            'g2_locked_amount': sec['g2_locked'],
            'self_locked_amount': sec['self_locked'],
            'application_file': application_file,
        }

        db.execute(
            """INSERT INTO loan_amendments
               (loan_id, requested_by, status, payload_json, reason)
               VALUES (?, ?, 'Pending', ?, ?)""",
            (loan_id, session['user_id'], json.dumps(payload), reason),
        )
        amendment_id = db.execute("SELECT last_insert_rowid() AS i").fetchone()['i']
        _notify_roles(
            db,
            ['CHAIRMAN', 'SECRETARY', 'COMMITTEE'],
            'Loan amendment approval required',
            f"Amendment request on {loan['loan_no']} needs 2 checker approvals.",
            url_for('admin.loan_detail', loan_id=loan_id),
            exclude_user_id=session['user_id'],
        )
        db.commit()
        log_action('REQUEST_LOAN_AMENDMENT', 'loan', loan_id,
                   f"Requested amendment #{amendment_id} on {loan['loan_no']}")
        flash('Amendment submitted for checker approvals (2 required).', 'success')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    return render_template(
        'admin/loan_form.html',
        members=enriched_members,
        max_term=Config.LOAN_MAX_TERM_MONTHS,
        rate_pct=int(Config.LOAN_INTEREST_RATE_MONTHLY * 100),
        loan=loan,
        edit_mode=True,
    )


@bp.route('/loans/<int:loan_id>/amendments/<int:amendment_id>/approve', methods=['POST'])
@role_required('CHAIRMAN', 'SECRETARY', 'COMMITTEE')
def loan_amendment_approve(loan_id, amendment_id):
    db = get_db()
    amend = db.execute(
        "SELECT * FROM loan_amendments WHERE id=? AND loan_id=?",
        (amendment_id, loan_id),
    ).fetchone()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan or not amend:
        flash('Loan amendment not found.', 'danger')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))
    if amend['status'] != 'Pending':
        flash('This amendment is already processed.', 'info')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))
    if amend['requested_by'] == session['user_id']:
        flash('Maker cannot approve their own amendment.', 'danger')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    already = db.execute(
        """SELECT 1 FROM loan_approvals
            WHERE loan_id=? AND amendment_id=? AND stage='AMENDMENT' AND user_id=?""",
        (loan_id, amendment_id, session['user_id']),
    ).fetchone()
    if already:
        flash('You already approved this amendment.', 'info')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    db.execute(
        """INSERT INTO loan_approvals (loan_id, amendment_id, stage, user_id, decision)
           VALUES (?, ?, 'AMENDMENT', ?, 'APPROVE')""",
        (loan_id, amendment_id, session['user_id']),
    )
    approvals = db.execute(
        """SELECT COUNT(DISTINCT user_id) c FROM loan_approvals
            WHERE loan_id=? AND amendment_id=? AND stage='AMENDMENT' AND decision='APPROVE'""",
        (loan_id, amendment_id),
    ).fetchone()['c']

    if approvals >= int(amend['approvals_required'] or 2):
        payload = json.loads(amend['payload_json'])
        db.execute(
            """UPDATE loans SET
                  member_id=?, principal=?, term_months=?, purpose=?, issued_date=?, due_date=?,
                  guarantor1_id=?, guarantor2_id=?,
                  g1_locked_amount=?, g2_locked_amount=?, self_locked_amount=?,
                  application_file=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (
                payload['member_id'], payload['principal'], payload['term_months'], payload['purpose'],
                payload['issued_date'], payload['due_date'],
                payload['guarantor1_id'], payload['guarantor2_id'],
                payload['g1_locked_amount'], payload['g2_locked_amount'], payload['self_locked_amount'],
                payload['application_file'], loan_id,
            ),
        )
        db.execute(
            """UPDATE loan_amendments
                  SET status='Approved', approved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (amendment_id,),
        )
        _notify_roles(
            db,
            ['IT_ADMIN', 'TREASURER'],
            'Loan amendment approved',
            f"Amendment for {loan['loan_no']} is fully approved and applied.",
            url_for('admin.loan_detail', loan_id=loan_id),
        )
        flash('Amendment fully approved and applied.', 'success')
    else:
        remaining = int(amend['approvals_required'] or 2) - approvals
        flash(f'Amendment approval recorded. {remaining} more checker approval(s) required.', 'success')

    db.commit()
    log_action('APPROVE_LOAN_AMENDMENT', 'loan', loan_id,
               f"Approval vote on amendment #{amendment_id} for {loan['loan_no']} ({approvals}/2)")
    return redirect(url_for('admin.loan_detail', loan_id=loan_id))


@bp.route('/loans/<int:loan_id>/approve', methods=['POST'])
@admin_required
def loan_approve(loan_id):
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))
    if loan['status'] != 'Pending':
        flash('Only pending loans can be approved.', 'warning')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    already = db.execute(
        """SELECT 1 FROM loan_approvals
            WHERE loan_id=? AND stage='NEW' AND user_id=?""",
        (loan_id, session['user_id']),
    ).fetchone()
    if already:
        flash('You already approved this loan request.', 'info')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    db.execute(
        """INSERT INTO loan_approvals (loan_id, stage, user_id, decision)
           VALUES (?, 'NEW', ?, 'APPROVE')""",
        (loan_id, session['user_id']),
    )

    approvals = db.execute(
        """SELECT COUNT(DISTINCT user_id) c FROM loan_approvals
            WHERE loan_id=? AND stage='NEW' AND decision='APPROVE'""",
        (loan_id,),
    ).fetchone()['c']

    if approvals >= 3:
        db.execute(
            """UPDATE loans SET status='Active', approved_date=?, approved_by=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (date.today().isoformat(), session['user_id'], loan_id),
        )
        _notify_roles(
            db,
            ['IT_ADMIN', 'TREASURER'],
            'Loan fully approved',
            f"{loan['loan_no']} reached 3 approvals and is now Active.",
            url_for('admin.loan_detail', loan_id=loan_id),
        )
        flash('Loan reached 3 approvals and is now active.', 'success')
    else:
        remaining = 3 - approvals
        flash(f'Approval recorded. {remaining} more approval(s) required.', 'success')

    db.commit()
    log_action('APPROVE_LOAN', 'loan', loan_id,
               f"Approval vote on {loan['loan_no']} ({approvals}/3)")
    return redirect(url_for('admin.loan_detail', loan_id=loan_id))


@bp.route('/loans/<int:loan_id>/disburse', methods=['POST'])
@admin_required
def loan_disburse(loan_id):
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))
    if loan['status'] != 'Active':
        flash('Loan must be active before disbursement.', 'warning')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))
    db.execute(
        "UPDATE loans SET disbursed_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (date.today().isoformat(), loan_id),
    )
    db.commit()
    log_action('DISBURSE_LOAN', 'loan', loan_id, f"Disbursed {loan['loan_no']}")
    flash('Loan disbursed.', 'success')
    return redirect(url_for('admin.loan_detail', loan_id=loan_id))


@bp.route('/loans/<int:loan_id>/decline', methods=['POST'])
@admin_required
def loan_decline(loan_id):
    db = get_db()
    reason = request.form.get('reason', 'Declined by committee')
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))
    db.execute(
        """UPDATE loans SET status='Declined', declined_reason=?,
              updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (reason, loan_id),
    )
    db.commit()
    log_action('DECLINE_LOAN', 'loan', loan_id,
               f"Declined {loan['loan_no']}: {reason}")
    flash('Loan declined.', 'info')
    return redirect(url_for('admin.loan_detail', loan_id=loan_id))


@bp.route('/loans/<int:loan_id>/repay', methods=['POST'])
@admin_required
def loan_repay(loan_id):
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('admin.loans_list'))

    amount = int(request.form.get('amount') or 0)
    payment_date = request.form.get('payment_date') or date.today().isoformat()
    method = request.form.get('payment_method') or 'Cash'
    ref = request.form.get('reference_no') or ''
    notes = request.form.get('notes') or ''

    if amount <= 0:
        flash('Amount must be positive.', 'danger')
        return redirect(url_for('admin.loan_detail', loan_id=loan_id))

    # Ensure all accrued penalties are recorded before calculating position
    _auto_apply_penalties(db, loan_id)

    # Auto-allocate against penalty -> interest -> principal
    repays = db.execute(
        "SELECT * FROM loan_repayments WHERE loan_id=?", (loan_id,)
    ).fetchall()
    pens = db.execute(
        "SELECT * FROM loan_penalties WHERE loan_id=?", (loan_id,)
    ).fetchall()
    pos = calculate_loan_position(loan, repays, pens)

    remaining = amount
    pay_penalty = min(remaining, pos['outstanding_penalty'])
    remaining -= pay_penalty
    pay_interest = min(remaining, pos['outstanding_interest'])
    remaining -= pay_interest
    pay_principal = min(remaining, pos['outstanding_principal'])
    remaining -= pay_principal

    if remaining > 0:
        flash(f"Note: {fmt_money(remaining)} of payment exceeds outstanding "
              f"balance. Recorded full amount anyway.", 'warning')

    db.execute(
        """INSERT INTO loan_repayments
           (loan_id, amount, principal_part, interest_part, penalty_part,
            payment_date, payment_method, reference_no, notes, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (loan_id, amount, pay_principal, pay_interest, pay_penalty,
         payment_date, method, ref, notes, session['user_id']),
    )

    # Re-check if loan is fully cleared
    repays2 = db.execute(
        "SELECT * FROM loan_repayments WHERE loan_id=?", (loan_id,)
    ).fetchall()
    pos2 = calculate_loan_position(loan, repays2, pens)
    if pos2['total_outstanding'] == 0:
        db.execute(
            """UPDATE loans SET status='Cleared', cleared_date=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (payment_date, loan_id),
        )

    db.commit()
    log_action('LOAN_REPAYMENT', 'loan', loan_id,
               f"Repayment {fmt_money(amount)} on {loan['loan_no']}")
    flash(f'Repayment of {fmt_money(amount)} recorded.', 'success')
    return redirect(url_for('admin.loan_detail', loan_id=loan_id))


@bp.route('/loans/penalties/run', methods=['POST'])
@admin_required
def penalties_run():
    """Run penalty assessment - applies 5% on overdue active loans for any month
       past the due date that hasn't been recorded yet."""
    db = get_db()
    today = date.today()
    active = db.execute(
        "SELECT * FROM loans WHERE status='Active'"
    ).fetchall()
    new_penalties = 0
    for loan in active:
        due_date = loan['due_date']
        if isinstance(due_date, str):
            due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()
        else:
            due_date_obj = due_date
        overdue_months = calculate_overdue_months(due_date_obj, today)
        if overdue_months <= 0:
            continue

        # Loan position to know outstanding principal
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, [])
        if pos['outstanding_principal'] <= 0:
            continue

        # Apply penalty for each overdue month not yet recorded
        for k in range(1, overdue_months + 1):
            penalty_period = (
                date(due_date_obj.year, due_date_obj.month, 1)
                + relativedelta(months=k)
            ).strftime('%Y-%m')
            existing = db.execute(
                "SELECT 1 FROM loan_penalties WHERE loan_id=? AND period=?",
                (loan['id'], penalty_period),
            ).fetchone()
            if existing:
                continue
            penalty_amount = int(
                pos['outstanding_principal'] * Config.LOAN_PENALTY_RATE_MONTHLY
            )
            db.execute(
                """INSERT INTO loan_penalties
                   (loan_id, period, penalty_amount, applied_date, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (loan['id'], penalty_period, penalty_amount,
                 date(due_date_obj.year + (due_date_obj.month + k - 1)//12,
                      ((due_date_obj.month + k - 1) % 12) + 1, 1).isoformat(),
                 f'Auto-applied penalty for {penalty_period}'),
            )
            new_penalties += 1
    db.commit()
    log_action('RUN_PENALTIES', 'loan', None,
               f"Applied {new_penalties} new penalties")
    flash(f'{new_penalties} new penalty entries applied.', 'success')
    return redirect(url_for('admin.loans_list'))


# ===========================================================================
# DIVIDENDS / INTEREST DISTRIBUTION
# ===========================================================================
@bp.route('/dividends')
@admin_required
def dividends_list():
    db = get_db()
    runs = db.execute(
        """SELECT dr.*, m.full_name AS top_saver_name, m.member_no AS top_saver_no
             FROM dividend_runs dr
             LEFT JOIN members m ON m.id = dr.top_saver_id
            ORDER BY dr.year DESC"""
    ).fetchall()

    # Compute the prospective pool for current/next year
    current_year = date.today().year
    pool = compute_interest_pool(db, current_year)
    return render_template('admin/dividends_list.html',
                           runs=runs, current_year=current_year, pool=pool)


def compute_interest_pool(db, year):
    """Compute collected + projected interest pool metrics for a calendar year."""
    # Keep penalties current before computing pool metrics.
    active_ids = db.execute("SELECT id FROM loans WHERE status='Active'").fetchall()
    for r in active_ids:
        _auto_apply_penalties(db, r['id'])

    # Collected (cash-realized) values in the selected year.
    rows = db.execute(
        """SELECT COALESCE(SUM(interest_part),0) i,
                  COALESCE(SUM(penalty_part),0) p
             FROM loan_repayments
            WHERE strftime('%Y', payment_date) = ?""",
        (str(year),),
    ).fetchone()

    interest_collected = int(rows['i'] or 0)
    penalty_collected = int(rows['p'] or 0)
    collected_total = interest_collected + penalty_collected

    # Projected/accrued values based on loans disbursed/issued in this year.
    loans_in_year = db.execute(
        """SELECT * FROM loans
            WHERE strftime('%Y', COALESCE(disbursed_date, issued_date)) = ?""",
        (str(year),),
    ).fetchall()
    projected_interest = 0
    for l in loans_in_year:
        projected_interest += int(l['principal']) * float(l['interest_rate'] or Config.LOAN_INTEREST_RATE_MONTHLY) * int(l['term_months'] or 1)
    projected_interest = int(projected_interest)

    projected_penalty_row = db.execute(
        """SELECT COALESCE(SUM(penalty_amount),0) p
             FROM loan_penalties
            WHERE substr(period, 1, 4) = ?""",
        (str(year),),
    ).fetchone()
    projected_penalty = int(projected_penalty_row['p'] or 0)

    projected_total = projected_interest + projected_penalty
    coverage_pct = round((collected_total / projected_total) * 100, 1) if projected_total > 0 else 0

    return {
        'year': year,
        'interest_collected': interest_collected,
        'penalty_collected': penalty_collected,
        'collected_total': collected_total,
        'projected_interest': projected_interest,
        'projected_penalty': projected_penalty,
        'projected_total': projected_total,
        'coverage_pct': coverage_pct,
        'total_pool': projected_total,
        'top_saver_award': Config.TOP_SAVER_AWARD,
        'distributable': max(0, projected_total - Config.TOP_SAVER_AWARD),
        'distributable_collected': max(0, collected_total - Config.TOP_SAVER_AWARD),
    }


@bp.route('/dividends/preview/<int:year>')
@admin_required
def dividends_preview(year):
    db = get_db()
    pool = compute_interest_pool(db, year)
    members = db.execute(
        """SELECT m.*, COALESCE(s.total,0) AS year_savings
             FROM members m
             LEFT JOIN (
                 SELECT member_id, SUM(amount) AS total
                   FROM savings
                  WHERE strftime('%Y', payment_date) = ?
                  GROUP BY member_id
             ) s ON s.member_id = m.id
            WHERE m.status='Active'
            ORDER BY year_savings DESC, m.member_no""",
        (str(year),),
    ).fetchall()

    if not members:
        flash('No active members to distribute to.', 'warning')
        return redirect(url_for('admin.dividends_list'))

    top_saver = max(members, key=lambda m: m['year_savings'] or 0)
    per_member = pool['distributable'] // len(members) if pool['distributable'] > 0 else 0

    payouts = []
    for m in members:
        amt = per_member
        is_top = (m['id'] == top_saver['id']) and (top_saver['year_savings'] or 0) > 0
        if is_top:
            amt += pool['top_saver_award']
        payouts.append({
            'member_id': m['id'],
            'member_no': m['member_no'],
            'full_name': m['full_name'],
            'year_savings': m['year_savings'] or 0,
            'amount': amt,
            'is_top_saver': is_top,
        })

    existing = db.execute(
        "SELECT * FROM dividend_runs WHERE year=?", (year,)
    ).fetchone()

    return render_template(
        'admin/dividends_preview.html',
        year=year, pool=pool, payouts=payouts,
        top_saver=top_saver, per_member=per_member,
        existing=existing,
    )


@bp.route('/dividends/run', methods=['POST'])
@role_required('IT_ADMIN', 'TREASURER')
def dividends_run():
    db = get_db()
    year = int(request.form.get('year') or date.today().year)
    distribution_date = request.form.get('distribution_date') or \
        f"{year}-12-31"

    existing = db.execute("SELECT * FROM dividend_runs WHERE year=?",
                          (year,)).fetchone()

    pool = compute_interest_pool(db, year)
    if pool['total_pool'] <= 0:
        flash(f'No interest collected in {year}. Cannot distribute.', 'warning')
        return redirect(url_for('admin.dividends_list'))

    members = db.execute(
        """SELECT m.*, COALESCE(s.total,0) AS year_savings
             FROM members m
             LEFT JOIN (
                 SELECT member_id, SUM(amount) AS total
                   FROM savings
                  WHERE strftime('%Y', payment_date) = ?
                  GROUP BY member_id
             ) s ON s.member_id = m.id
            WHERE m.status='Active'""",
        (str(year),),
    ).fetchall()

    if not members:
        flash('No active members.', 'warning')
        return redirect(url_for('admin.dividends_list'))

    top_saver = max(members, key=lambda m: m['year_savings'] or 0)
    per_member = pool['distributable'] // len(members)

    # Before year-end, keep manual runs hidden from members until explicitly published.
    year_end_reached = date.today() >= date(year, 12, 31)
    run_status = existing['status'] if existing else ('Published' if year_end_reached else 'Hidden')

    if existing:
        run_id = existing['id']
        db.execute(
            """UPDATE dividend_runs SET
                  total_interest_pool=?, top_saver_award=?, distributable_pool=?,
                  member_count=?, per_member_amount=?, top_saver_id=?, top_saver_total=?,
                  distribution_date=?, status=?, processed_by=?
                WHERE id=?""",
            (pool['total_pool'], Config.TOP_SAVER_AWARD, pool['distributable'],
             len(members), per_member, top_saver['id'], top_saver['year_savings'] or 0,
             distribution_date, run_status, session['user_id'], run_id),
        )
        db.execute("DELETE FROM dividend_payouts WHERE dividend_run_id=?", (run_id,))
    else:
        cur = db.execute(
            """INSERT INTO dividend_runs
               (year, total_interest_pool, top_saver_award, distributable_pool,
                member_count, per_member_amount, top_saver_id, top_saver_total,
                distribution_date, status, processed_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (year, pool['total_pool'], Config.TOP_SAVER_AWARD, pool['distributable'],
             len(members), per_member, top_saver['id'],
             top_saver['year_savings'] or 0,
             distribution_date, run_status, session['user_id']),
        )
        run_id = cur.lastrowid

    for m in members:
        amt = per_member
        is_top = (m['id'] == top_saver['id']) and (top_saver['year_savings'] or 0) > 0
        if is_top:
            amt += Config.TOP_SAVER_AWARD
        db.execute(
            """INSERT INTO dividend_payouts
               (dividend_run_id, member_id, amount, is_top_saver)
               VALUES (?, ?, ?, ?)""",
            (run_id, m['id'], amt, 1 if is_top else 0),
        )

    db.commit()
    action_name = 'RERUN_DIVIDENDS' if existing else 'RUN_DIVIDENDS'
    log_action(action_name, 'dividend_run', run_id,
               f"Distributed {fmt_money(pool['total_pool'])} for {year}")
    _notify_roles(
        db,
        ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
        'Dividends Processed',
        f"Dividend run for {year} was processed ({fmt_money(pool['total_pool'])} pool).",
        url_for('admin.dividends_detail', run_id=run_id),
        exclude_user_id=session['user_id'],
    )
    if run_status == 'Published':
        member_ids = db.execute("SELECT id FROM members WHERE status='Active'").fetchall()
        for mr in member_ids:
            _notify_member(
                db,
                mr['id'],
                f"Dividend Update {year}",
                f"Dividend results for {year} were published to your portal.",
                url_for('member.profile'),
            )
    db.commit()
    if run_status == 'Published':
        flash(f'Dividends for {year} processed and published: '
              f'{fmt_money(pool["total_pool"])} pool.', 'success')
    else:
        flash(f'Dividends for {year} processed and saved as hidden draft. '
              'Publish when ready.', 'info')
    return redirect(url_for('admin.dividends_detail', run_id=run_id))


@bp.route('/dividends/<int:run_id>/visibility', methods=['POST'])
@role_required('IT_ADMIN', 'TREASURER')
def dividends_visibility(run_id):
    db = get_db()
    run = db.execute("SELECT * FROM dividend_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        flash('Dividend run not found.', 'danger')
        return redirect(url_for('admin.dividends_list'))

    action = (request.form.get('action') or '').strip().lower()
    if action == 'publish':
        new_status = 'Published'
        msg = 'Dividend run published to members.'
        audit_msg = f"Published dividend run {run['year']}"
    elif action == 'hide':
        new_status = 'Hidden'
        msg = 'Dividend run hidden from members.'
        audit_msg = f"Hid dividend run {run['year']}"
    else:
        flash('Invalid visibility action.', 'warning')
        return redirect(url_for('admin.dividends_detail', run_id=run_id))

    db.execute("UPDATE dividend_runs SET status=? WHERE id=?", (new_status, run_id))
    db.commit()
    log_action('DIVIDEND_VISIBILITY', 'dividend_run', run_id, audit_msg)
    if new_status == 'Published':
        member_ids = db.execute("SELECT id FROM members WHERE status='Active'").fetchall()
        for mr in member_ids:
            _notify_member(
                db,
                mr['id'],
                f"Dividend Published {run['year']}",
                f"Dividend run {run['year']} is now visible in your member portal.",
                url_for('member.profile'),
            )
        db.commit()
    flash(msg, 'success')
    return redirect(url_for('admin.dividends_detail', run_id=run_id))


@bp.route('/dividends/<int:run_id>/undo', methods=['POST'])
@role_required('IT_ADMIN', 'TREASURER')
def dividends_undo(run_id):
    db = get_db()
    run = db.execute("SELECT * FROM dividend_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        flash('Dividend run not found.', 'danger')
        return redirect(url_for('admin.dividends_list'))

    db.execute("DELETE FROM dividend_payouts WHERE dividend_run_id=?", (run_id,))
    db.execute("DELETE FROM dividend_runs WHERE id=?", (run_id,))
    db.commit()

    log_action('UNDO_DIVIDENDS', 'dividend_run', run_id,
               f"Removed dividend run for {run['year']}")
    flash(f"Dividend run for {run['year']} has been undone and hidden from members.", 'success')
    return redirect(url_for('admin.dividends_list'))


@bp.route('/dividends/<int:run_id>')
@admin_required
def dividends_detail(run_id):
    db = get_db()
    run = db.execute(
        """SELECT dr.*, m.full_name AS top_saver_name, m.member_no AS top_saver_no
             FROM dividend_runs dr
             LEFT JOIN members m ON m.id = dr.top_saver_id
            WHERE dr.id = ?""",
        (run_id,),
    ).fetchone()
    if not run:
        flash('Dividend run not found.', 'danger')
        return redirect(url_for('admin.dividends_list'))

    payouts = db.execute(
        """SELECT dp.*, m.full_name, m.member_no
             FROM dividend_payouts dp
             JOIN members m ON m.id = dp.member_id
            WHERE dp.dividend_run_id = ?
            ORDER BY dp.is_top_saver DESC, m.member_no""",
        (run_id,),
    ).fetchall()
    return render_template('admin/dividends_detail.html',
                           run=run, payouts=payouts)


# ===========================================================================
# MEETING MINUTES
# ===========================================================================
@bp.route('/minutes')
@admin_required
def minutes_list():
    db = get_db()
    rows = db.execute(
        """SELECT m.*, mem.full_name AS chair_name
             FROM minutes m
             LEFT JOIN members mem ON mem.id = m.chaired_by
            ORDER BY m.meeting_date DESC"""
    ).fetchall()
    return render_template('admin/minutes_list.html', minutes=rows)


@bp.route('/minutes/new', methods=['GET', 'POST'])
@role_required('SECRETARY', 'IT_ADMIN')
def minutes_create():
    db = get_db()
    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    if request.method == 'POST':
        meeting_date = request.form.get('meeting_date')
        title = (request.form.get('title') or '').strip()
        venue = request.form.get('venue') or ''
        chaired_by = request.form.get('chaired_by') or None
        agenda = request.form.get('agenda') or ''
        discussion = request.form.get('discussion') or ''
        resolutions = request.form.get('resolutions') or ''
        signed_file = _save_signed_minutes('signed_file')
        next_meeting = request.form.get('next_meeting') or None
        attendance = ','.join(request.form.getlist('attendance'))

        if not (meeting_date and title):
            flash('Meeting date and title are required.', 'danger')
            return render_template('admin/minutes_form.html',
                                   minutes=None, members=members,
                                   selected_attendees=[])

        cur = db.execute(
            """INSERT INTO minutes
               (meeting_date, title, venue, chaired_by, recorded_by,
                attendance, agenda, discussion, resolutions, signed_file, next_meeting)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meeting_date, title, venue,
             int(chaired_by) if chaired_by else None,
             session['user_id'], attendance, agenda, discussion, resolutions,
             signed_file,
             next_meeting),
        )
        db.commit()
        log_action('CREATE_MINUTES', 'minutes', cur.lastrowid,
                   f"Minutes for {meeting_date} - {title}")
        _notify_roles(
            db,
            ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
            'Minutes Uploaded',
            f"New meeting minutes were recorded: {title}",
            url_for('admin.minutes_detail', minutes_id=cur.lastrowid),
            exclude_user_id=session['user_id'],
        )
        db.commit()
        flash('Meeting minutes saved.', 'success')
        return redirect(url_for('admin.minutes_detail',
                                minutes_id=cur.lastrowid))
    return render_template('admin/minutes_form.html',
                           minutes=None, members=members,
                           selected_attendees=[])


@bp.route('/minutes/<int:minutes_id>')
@admin_required
def minutes_detail(minutes_id):
    db = get_db()
    m = db.execute(
        """SELECT m.*, mem.full_name AS chair_name, mem.member_no AS chair_no
             FROM minutes m
             LEFT JOIN members mem ON mem.id = m.chaired_by
            WHERE m.id = ?""",
        (minutes_id,),
    ).fetchone()
    if not m:
        flash('Minutes not found.', 'danger')
        return redirect(url_for('admin.minutes_list'))

    attendees = []
    if m['attendance']:
        ids = [int(x) for x in m['attendance'].split(',') if x.strip().isdigit()]
        if ids:
            placeholders = ','.join('?' * len(ids))
            attendees = db.execute(
                f"SELECT id, full_name, member_no FROM members WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
    return render_template('admin/minutes_detail.html',
                           m=m, attendees=attendees)


@bp.route('/minutes/<int:minutes_id>/edit', methods=['GET', 'POST'])
@role_required('SECRETARY', 'IT_ADMIN')
def minutes_edit(minutes_id):
    db = get_db()
    m = db.execute("SELECT * FROM minutes WHERE id=?", (minutes_id,)).fetchone()
    if not m:
        flash('Minutes not found.', 'danger')
        return redirect(url_for('admin.minutes_list'))
    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    if request.method == 'POST':
        meeting_date = request.form.get('meeting_date')
        title = (request.form.get('title') or '').strip()
        venue = request.form.get('venue') or ''
        chaired_by = request.form.get('chaired_by') or None
        agenda = request.form.get('agenda') or ''
        discussion = request.form.get('discussion') or ''
        resolutions = request.form.get('resolutions') or ''
        signed_file = _save_signed_minutes('signed_file') or m['signed_file']
        next_meeting = request.form.get('next_meeting') or None
        attendance = ','.join(request.form.getlist('attendance'))
        db.execute(
            """UPDATE minutes SET meeting_date=?, title=?, venue=?, chaired_by=?,
              attendance=?, agenda=?, discussion=?, resolutions=?, signed_file=?, next_meeting=?
               WHERE id=?""",
            (meeting_date, title, venue,
             int(chaired_by) if chaired_by else None,
                         attendance, agenda, discussion, resolutions, signed_file,
             next_meeting, minutes_id),
        )
        db.commit()
        log_action('UPDATE_MINUTES', 'minutes', minutes_id,
                   f"Updated minutes {minutes_id}")
        flash('Minutes updated.', 'success')
        return redirect(url_for('admin.minutes_detail',
                                minutes_id=minutes_id))
    selected = []
    if m['attendance']:
        selected = [int(x) for x in m['attendance'].split(',')
                    if x.strip().isdigit()]
    return render_template('admin/minutes_form.html', minutes=m,
                           members=members, selected_attendees=selected)


@bp.route('/minutes/<int:minutes_id>/upload', methods=['POST'])
@role_required('SECRETARY', 'IT_ADMIN')
def minutes_upload(minutes_id):
    db = get_db()
    m = db.execute("SELECT id, title, signed_file FROM minutes WHERE id=?", (minutes_id,)).fetchone()
    if not m:
        flash('Minutes not found.', 'danger')
        return redirect(url_for('admin.minutes_list'))

    signed_file = _save_signed_minutes('signed_file')
    if not signed_file:
        flash('Upload failed. Please attach PDF/JPG/PNG/WEBP file.', 'danger')
        return redirect(url_for('admin.minutes_detail', minutes_id=minutes_id))

    db.execute("UPDATE minutes SET signed_file=? WHERE id=?", (signed_file, minutes_id))
    db.commit()
    log_action('UPLOAD_MINUTES_FILE', 'minutes', minutes_id,
               f"Uploaded signed minutes file for {m['title']}")
    _notify_roles(
        db,
        ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
        'Minutes File Updated',
        f"A signed file was uploaded for minutes: {m['title']}",
        url_for('admin.minutes_detail', minutes_id=minutes_id),
        exclude_user_id=session['user_id'],
    )
    db.commit()
    flash('Signed minutes uploaded.', 'success')
    return redirect(url_for('admin.minutes_detail', minutes_id=minutes_id))


@bp.route('/minutes/<int:minutes_id>/publish', methods=['POST'])
@role_required('SECRETARY', 'IT_ADMIN')
def minutes_publish(minutes_id):
    db = get_db()
    m = db.execute(
        "SELECT id, title, is_published FROM minutes WHERE id=?",
        (minutes_id,),
    ).fetchone()
    if not m:
        flash('Minutes not found.', 'danger')
        return redirect(url_for('admin.minutes_list'))

    action = (request.form.get('action') or '').strip().lower()
    if action not in ('publish', 'unpublish'):
        action = 'unpublish' if m['is_published'] else 'publish'

    new_status = 1 if action == 'publish' else 0
    if int(m['is_published'] or 0) == new_status:
        flash('Minutes publishing status is already set.', 'info')
        return redirect(request.referrer or url_for('admin.minutes_detail', minutes_id=minutes_id))

    db.execute(
        "UPDATE minutes SET is_published=? WHERE id=?",
        (new_status, minutes_id),
    )
    db.commit()

    if new_status:
        log_action('PUBLISH_MINUTES', 'minutes', minutes_id,
                   f"Published minutes {minutes_id} - {m['title']}")
        member_ids = db.execute("SELECT id FROM members WHERE status='Active'").fetchall()
        for mr in member_ids:
            _notify_member(
                db,
                mr['id'],
                'New Minutes Published',
                f"New published minutes are available: {m['title']}",
                url_for('member.minutes_detail', minutes_id=minutes_id),
            )
        db.commit()
        flash('Minutes published to member portal.', 'success')
    else:
        log_action('UNPUBLISH_MINUTES', 'minutes', minutes_id,
                   f"Unpublished minutes {minutes_id} - {m['title']}")
        flash('Minutes unpublished from member portal.', 'warning')

    return redirect(request.referrer or url_for('admin.minutes_detail', minutes_id=minutes_id))


# ===========================================================================
# FINES
# ===========================================================================
@bp.route('/fines')
@admin_required
def fines_list():
    db = get_db()
    rows = db.execute(
        """SELECT f.*, m.member_no, m.full_name
             FROM fines f
             JOIN members m ON m.id = f.member_id
            ORDER BY f.fine_date DESC"""
    ).fetchall()
    return render_template('admin/fines_list.html', fines=rows)


@bp.route('/fines/new', methods=['GET', 'POST'])
@admin_required
def fines_create():
    db = get_db()
    if request.method == 'POST':
        member_id = int(request.form.get('member_id'))
        fine_date = request.form.get('fine_date') or date.today().isoformat()
        violation = (request.form.get('violation') or '').strip()
        amount = int(request.form.get('amount') or 0)
        notes = request.form.get('notes') or ''
        if not violation or amount <= 0:
            flash('Violation and a positive amount are required.', 'danger')
            return redirect(url_for('admin.fines_create'))
        cur = db.execute(
            """INSERT INTO fines
               (member_id, fine_date, violation, amount, notes, issued_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (member_id, fine_date, violation, amount, notes, session['user_id']),
        )
        db.commit()
        log_action('CREATE_FINE', 'fine', cur.lastrowid,
                   f"Fine {fmt_money(amount)} for member {member_id}")
        _notify_member(
            db,
            member_id,
            'Fine Recorded',
            f"A fine of {fmt_money(amount)} was recorded: {violation}.",
            url_for('member.profile'),
        )
        db.commit()
        flash('Fine recorded.', 'success')
        return redirect(url_for('admin.fines_list'))
    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    return render_template(
        'admin/fines_form.html',
        members=members,
        mode='create',
        fine=None,
        form_action=url_for('admin.fines_create'),
    )


@bp.route('/fines/<int:fine_id>/edit', methods=['GET', 'POST'])
@role_required('CHAIRMAN', 'TREASURER', 'SECRETARY', 'IT_ADMIN')
def fines_edit(fine_id):
    db = get_db()
    fine = db.execute(
        """SELECT f.*, m.member_no, m.full_name
             FROM fines f
             JOIN members m ON m.id = f.member_id
            WHERE f.id=?""",
        (fine_id,),
    ).fetchone()
    if not fine:
        flash('Fine record not found.', 'danger')
        return redirect(url_for('admin.fines_list'))

    if request.method == 'POST':
        member_id = int(request.form.get('member_id') or fine['member_id'])
        fine_date = request.form.get('fine_date') or fine['fine_date']
        violation = (request.form.get('violation') or '').strip()
        amount = int(request.form.get('amount') or 0)
        notes = request.form.get('notes') or ''
        if not violation or amount <= 0:
            flash('Violation and a positive amount are required.', 'danger')
            return redirect(url_for('admin.fines_edit', fine_id=fine_id))

        db.execute(
            """UPDATE fines
                  SET member_id=?, fine_date=?, violation=?, amount=?, notes=?, issued_by=?
                WHERE id=?""",
            (member_id, fine_date, violation, amount, notes, session['user_id'], fine_id),
        )
        db.commit()
        log_action('EDIT_FINE', 'fine', fine_id,
                   f"Edited fine {fine_id}: {fmt_money(amount)} for member {member_id}")
        _notify_member(
            db,
            member_id,
            'Fine Updated',
            f"A fine record was updated: {fmt_money(amount)} for {violation}.",
            url_for('member.profile'),
        )
        db.commit()
        flash('Fine record updated.', 'success')
        return redirect(url_for('admin.fines_list'))

    members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    return render_template(
        'admin/fines_form.html',
        members=members,
        mode='edit',
        fine=fine,
        form_action=url_for('admin.fines_edit', fine_id=fine_id),
    )


@bp.route('/fines/<int:fine_id>/pay', methods=['POST'])
@admin_required
def fines_pay(fine_id):
    db = get_db()
    fine = db.execute(
        "SELECT id, member_id, amount FROM fines WHERE id=?",
        (fine_id,),
    ).fetchone()
    db.execute(
        "UPDATE fines SET status='Paid', paid_date=? WHERE id=?",
        (date.today().isoformat(), fine_id),
    )
    db.commit()
    log_action('PAY_FINE', 'fine', fine_id, f"Marked fine {fine_id} as paid")
    if fine:
        _notify_member(
            db,
            fine['member_id'],
            'Fine Marked Paid',
            f"Your fine payment of {fmt_money(fine['amount'])} has been marked as paid.",
            url_for('member.profile'),
        )
        db.commit()
    flash('Fine marked paid.', 'success')
    return redirect(request.referrer or url_for('admin.fines_list'))


@bp.route('/fines/<int:fine_id>/delete', methods=['POST'])
@role_required('TREASURER', 'IT_ADMIN')
def fines_delete(fine_id):
    db = get_db()
    fine = db.execute(
        "SELECT id, member_id, amount FROM fines WHERE id=?",
        (fine_id,),
    ).fetchone()
    if not fine:
        flash('Fine record not found.', 'danger')
        return redirect(url_for('admin.fines_list'))

    db.execute("DELETE FROM fines WHERE id=?", (fine_id,))
    db.commit()
    log_action('DELETE_FINE', 'fine', fine_id,
               f"Deleted fine {fine_id} ({fmt_money(fine['amount'])}) for member {fine['member_id']}")
    _notify_member(
        db,
        fine['member_id'],
        'Fine Removed',
        f"A fine entry of {fmt_money(fine['amount'])} was removed by administration.",
        url_for('member.profile'),
    )
    db.commit()
    flash('Fine record deleted.', 'success')
    return redirect(url_for('admin.fines_list'))


# ===========================================================================
# ANNUAL FEES
# ===========================================================================
@bp.route('/fees')
@admin_required
def fees_list():
    db = get_db()
    year = int(request.args.get('year') or date.today().year)
    rows = db.execute(
        """SELECT f.*, m.member_no, m.full_name
             FROM annual_fees f
             JOIN members m ON m.id = f.member_id
            WHERE f.year = ? ORDER BY m.member_no""",
        (year,),
    ).fetchall()

    # Members without a fee record this year
    paid_ids = {r['member_id'] for r in rows}
    pending = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    pending = [m for m in pending if m['id'] not in paid_ids]
    
    # Calculate days remaining until deadline (30 June)
    deadline = date(year, 6, 30)
    days_remaining = max(0, (deadline - date.today()).days)
    
    return render_template('admin/fees_list.html', rows=rows, pending=pending,
                           year=year, fee_amount=Config.ANNUAL_FEE, days_remaining=days_remaining)


@bp.route('/fees/record', methods=['POST'])
@admin_required
def fees_record():
    db = get_db()
    member_id = int(request.form.get('member_id'))
    year = int(request.form.get('year'))
    amount = int(request.form.get('amount') or Config.ANNUAL_FEE)
    paid_date = request.form.get('paid_date') or date.today().isoformat()
    deadline = request.form.get('deadline') or f"{year}-06-30"

    existing = db.execute(
        "SELECT id FROM annual_fees WHERE member_id=? AND year=?",
        (member_id, year),
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE annual_fees SET amount=?, status='Paid', paid_date=?,
                  deadline=?, recorded_by=? WHERE id=?""",
            (amount, paid_date, deadline, session['user_id'], existing['id']),
        )
    else:
        db.execute(
            """INSERT INTO annual_fees
               (member_id, year, amount, status, paid_date, deadline, recorded_by)
               VALUES (?, ?, ?, 'Paid', ?, ?, ?)""",
            (member_id, year, amount, paid_date, deadline, session['user_id']),
        )
    db.commit()
    log_action('RECORD_FEE', 'annual_fee', member_id,
               f"Annual fee {year} - {fmt_money(amount)} for member {member_id}")
    _notify_member(
        db,
        member_id,
        'Annual Fee Recorded',
        f"Your annual fee payment for {year} ({fmt_money(amount)}) has been recorded.",
        url_for('member.profile'),
    )
    db.commit()
    flash('Annual fee recorded.', 'success')
    return redirect(url_for('admin.fees_list', year=year))


@bp.route('/fees/<int:fee_id>/delete', methods=['POST'])
@admin_required
def fees_delete(fee_id):
    """Delete a fee record. Treasurer and IT_ADMIN only."""
    db = get_db()
    fee = db.execute("SELECT * FROM annual_fees WHERE id=?", (fee_id,)).fetchone()
    if not fee:
        flash('Fee record not found.', 'danger')
        return redirect(url_for('admin.fees_list'))
    
    year = fee['year']
    member_id = fee['member_id']
    log_action('DELETE_FEE', 'annual_fee', member_id,
               f"Deleted annual fee {year} - {fmt_money(fee['amount'])} for member {member_id}")
    db.execute("DELETE FROM annual_fees WHERE id=?", (fee_id,))
    db.commit()
    _notify_member(
        db,
        member_id,
        'Annual Fee Entry Removed',
        f"A fee entry for {year} ({fmt_money(fee['amount'])}) was removed by administration.",
        url_for('member.profile'),
    )
    db.commit()
    flash('Fee record deleted.', 'success')
    return redirect(url_for('admin.fees_list', year=year))


# ===========================================================================
# EXPENSES (Operational fund management)
# ===========================================================================
@bp.route('/expenses')
@admin_required
def expenses_list():
    db = get_db()
    tab = (request.args.get('tab') or 'all').strip().lower()
    if tab not in {'all', 'income', 'pending', 'summary'}:
        tab = 'all'

    ops = _operational_fund_snapshot(db)

    expenses_rows = db.execute(
        """SELECT e.*, req.username AS requested_by_name,
                  COALESCE(reqm.full_name, req.username) AS requested_by_full,
                  appr.username AS approver_name,
                  COALESCE(apprm.full_name, appr.username) AS approver_full
             FROM expenses e
             LEFT JOIN users req ON req.id = e.requested_by
             LEFT JOIN members reqm ON reqm.id = req.member_id
             LEFT JOIN users appr ON appr.id = e.approved_by
             LEFT JOIN members apprm ON apprm.id = appr.member_id
            ORDER BY e.created_at DESC, e.id DESC"""
    ).fetchall()

    incomes = db.execute(
        """SELECT oi.*, u.username, m.full_name
             FROM operational_incomes oi
             LEFT JOIN users u ON u.id = oi.recorded_by
             LEFT JOIN members m ON m.id = u.member_id
            ORDER BY oi.income_date DESC, oi.id DESC"""
    ).fetchall()

    pending_rows = [r for r in expenses_rows if r['status'] == 'Pending']
    approved_rows = [r for r in expenses_rows if r['status'] == 'Approved']
    rejected_rows = [r for r in expenses_rows if r['status'] == 'Rejected']

    chairman_approvers = db.execute(
        """SELECT u.id, u.username, m.full_name
             FROM users u
             JOIN members m ON m.id = u.member_id
            WHERE u.is_active = 1 AND m.role = 'CHAIRMAN' AND m.status='Active'
            ORDER BY m.member_no"""
    ).fetchall()

    return render_template(
        'admin/expenses_list.html',
        tab=tab,
        ops=ops,
        expenses_rows=expenses_rows,
        pending_rows=pending_rows,
        approved_rows=approved_rows,
        rejected_rows=rejected_rows,
        incomes=incomes,
        chairman_approvers=chairman_approvers,
    )


@bp.route('/expenses/income/record', methods=['POST'])
@role_required('TREASURER', 'IT_ADMIN')
def expenses_income_record():
    db = get_db()
    amount = int(request.form.get('amount') or 0)
    income_date = (request.form.get('income_date') or date.today().isoformat()).strip()
    source = (request.form.get('source') or '').strip()
    notes = (request.form.get('notes') or '').strip()

    if amount <= 0 or not source:
        flash('Income source and positive amount are required.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='income'))

    income_no = _next_doc_no(db, 'operational_incomes', 'income_no', 'INC')
    db.execute(
        """INSERT INTO operational_incomes
           (income_no, income_date, amount, source, notes, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (income_no, income_date, amount, source, notes, session['user_id']),
    )
    db.commit()
    log_action('RECORD_OPERATIONAL_INCOME', 'operational_income', None,
               f"Recorded {income_no} {fmt_money(amount)} from {source}")
    flash(f'Operational income {income_no} recorded.', 'success')
    return redirect(url_for('admin.expenses_list', tab='income'))


@bp.route('/expenses/request', methods=['POST'])
@role_required('TREASURER', 'IT_ADMIN')
def expenses_request():
    db = get_db()
    amount = int(request.form.get('amount') or 0)
    expense_date = (request.form.get('expense_date') or date.today().isoformat()).strip()
    purpose = (request.form.get('purpose') or '').strip()
    category = (request.form.get('category') or 'Operations').strip()
    notes = (request.form.get('notes') or '').strip()
    approver_user_id = request.form.get('approver_user_id', type=int)

    if amount <= 0 or not purpose:
        flash('Purpose and positive amount are required.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='pending'))

    approver = db.execute(
        """SELECT u.id
             FROM users u
             JOIN members m ON m.id = u.member_id
            WHERE u.id = ? AND u.is_active = 1 AND m.role='CHAIRMAN' AND m.status='Active'""",
        (approver_user_id,),
    ).fetchone()
    if not approver:
        flash('A valid active chairman approver is required.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='pending'))

    ops = _operational_fund_snapshot(db)
    if amount > ops['available_after_pending']:
        flash(
            f"Amount exceeds available operational balance after pending requests ({fmt_money(ops['available_after_pending'])}).",
            'danger',
        )
        return redirect(url_for('admin.expenses_list', tab='pending'))

    expense_no = _next_doc_no(db, 'expenses', 'expense_no', 'EXP')
    receipt_path = _save_expense_receipt('receipt_file')
    db.execute(
        """INSERT INTO expenses
           (expense_no, expense_date, amount, purpose, category, notes, status,
            requested_by, requested_approver_user_id, receipt_path)
           VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?)""",
        (expense_no, expense_date, amount, purpose, category, notes,
         session['user_id'], approver_user_id, receipt_path),
    )
    db.commit()

    _notify_roles(
        db,
        ['CHAIRMAN'],
        'Expense approval required',
        f"{expense_no} for {fmt_money(amount)} is waiting your decision.",
        url_for('admin.expenses_list', tab='pending'),
        exclude_user_id=None,
    )
    db.commit()
    log_action('REQUEST_EXPENSE', 'expense', None,
               f"Submitted {expense_no} for approval ({fmt_money(amount)})")
    flash(f'Expense request {expense_no} submitted for chairman approval.', 'success')
    return redirect(url_for('admin.expenses_list', tab='pending'))


@bp.route('/expenses/<int:expense_id>/approve', methods=['POST'])
@role_required('CHAIRMAN')
def expenses_approve(expense_id):
    db = get_db()
    row = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not row:
        flash('Expense request not found.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='pending'))
    if row['status'] != 'Pending':
        flash('Only pending requests can be approved.', 'warning')
        return redirect(url_for('admin.expenses_list', tab='pending'))
    if row['requested_approver_user_id'] and int(row['requested_approver_user_id']) != int(session['user_id']):
        flash('This request was assigned to another chairman approver.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='pending'))

    ops = _operational_fund_snapshot(db)
    if int(row['amount'] or 0) > int(ops['operational_fund_total']):
        flash(
            f"Insufficient operational fund. Available: {fmt_money(ops['operational_fund_total'])}.",
            'danger',
        )
        return redirect(url_for('admin.expenses_list', tab='pending'))

    decision_notes = (request.form.get('decision_notes') or '').strip()
    db.execute(
        """UPDATE expenses
              SET status='Approved', approved_by=?, approved_at=CURRENT_TIMESTAMP,
                  decision_notes=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (session['user_id'], decision_notes, expense_id),
    )
    db.commit()
    # Notify the requesting treasurer (and all treasurers)
    _notify_roles(
        db, ['TREASURER'],
        f"Expense {row['expense_no']} Approved",
        f"{row['expense_no']} for {fmt_money(row['amount'])} was approved by the Chairman." +
        (f" Notes: {decision_notes}" if decision_notes else ''),
        url_for('admin.expenses_list', tab='all'),
    )
    _notify_user(
        db,
        row['requested_by'],
        f"Expense {row['expense_no']} Approved",
        f"Your expense request for {fmt_money(row['amount'])} was approved." +
        (f" Notes: {decision_notes}" if decision_notes else ''),
        url_for('admin.expenses_list', tab='all'),
    )
    db.commit()
    log_action('APPROVE_EXPENSE', 'expense', expense_id,
               f"Approved {row['expense_no']} for {fmt_money(row['amount'])}")
    flash(f"Expense {row['expense_no']} approved and treasurer notified.", 'success')
    return redirect(url_for('admin.expenses_list', tab='pending'))


@bp.route('/expenses/<int:expense_id>/reject', methods=['POST'])
@role_required('CHAIRMAN')
def expenses_reject(expense_id):
    db = get_db()
    row = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not row:
        flash('Expense request not found.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='pending'))
    if row['status'] != 'Pending':
        flash('Only pending requests can be rejected.', 'warning')
        return redirect(url_for('admin.expenses_list', tab='pending'))
    if row['requested_approver_user_id'] and int(row['requested_approver_user_id']) != int(session['user_id']):
        flash('This request was assigned to another chairman approver.', 'danger')
        return redirect(url_for('admin.expenses_list', tab='pending'))

    decision_notes = (request.form.get('decision_notes') or '').strip()
    if not decision_notes:
        decision_notes = 'Rejected by chairman.'

    db.execute(
        """UPDATE expenses
              SET status='Rejected', approved_by=?, approved_at=CURRENT_TIMESTAMP,
                  decision_notes=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (session['user_id'], decision_notes, expense_id),
    )
    db.commit()
    # Notify the requesting treasurer (and all treasurers)
    _notify_roles(
        db, ['TREASURER'],
        f"Expense {row['expense_no']} Rejected",
        f"{row['expense_no']} for {fmt_money(row['amount'])} was rejected by the Chairman. Reason: {decision_notes}",
        url_for('admin.expenses_list', tab='all'),
    )
    _notify_user(
        db,
        row['requested_by'],
        f"Expense {row['expense_no']} Rejected",
        f"Your expense request for {fmt_money(row['amount'])} was rejected. Reason: {decision_notes}",
        url_for('admin.expenses_list', tab='all'),
    )
    db.commit()
    log_action('REJECT_EXPENSE', 'expense', expense_id,
               f"Rejected {row['expense_no']} ({fmt_money(row['amount'])})")
    flash(f"Expense {row['expense_no']} rejected and treasurer notified.", 'warning')
    return redirect(url_for('admin.expenses_list', tab='pending'))


@bp.route('/expenses/<int:expense_id>/edit', methods=['POST'])
@role_required('TREASURER', 'IT_ADMIN')
def expenses_edit(expense_id):
    db = get_db()
    row = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not row:
        flash('Expense not found.', 'danger')
        return redirect(url_for('admin.expenses_list'))
    if row['status'] != 'Pending':
        flash('Only pending expenses can be edited.', 'warning')
        return redirect(url_for('admin.expenses_list'))

    amount = int(request.form.get('amount') or 0)
    expense_date = (request.form.get('expense_date') or date.today().isoformat()).strip()
    purpose = (request.form.get('purpose') or '').strip()
    category = (request.form.get('category') or 'Operations').strip()
    notes = (request.form.get('notes') or '').strip()
    approver_user_id = request.form.get('approver_user_id', type=int)

    if amount <= 0 or not purpose:
        flash('Purpose and positive amount are required.', 'danger')
        return redirect(url_for('admin.expenses_list'))

    new_receipt = _save_expense_receipt('receipt_file')
    receipt_path = new_receipt if new_receipt else row['receipt_path']

    db.execute(
        """UPDATE expenses
              SET amount=?, expense_date=?, purpose=?, category=?, notes=?,
                  requested_approver_user_id=?, receipt_path=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (amount, expense_date, purpose, category, notes, approver_user_id, receipt_path, expense_id),
    )
    db.commit()
    log_action('EDIT_EXPENSE', 'expense', expense_id,
               f"Edited {row['expense_no']} → amount {fmt_money(amount)}, purpose: {purpose}")
    flash(f"Expense {row['expense_no']} updated.", 'success')
    return redirect(url_for('admin.expenses_list', tab='all'))


@bp.route('/expenses/<int:expense_id>/delete', methods=['POST'])
@role_required('TREASURER', 'IT_ADMIN')
def expenses_delete(expense_id):
    db = get_db()
    row = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not row:
        flash('Expense not found.', 'danger')
        return redirect(url_for('admin.expenses_list'))
    if row['status'] != 'Pending':
        flash('Only pending expenses can be deleted.', 'warning')
        return redirect(url_for('admin.expenses_list'))

    db.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    db.commit()
    log_action('DELETE_EXPENSE', 'expense', expense_id,
               f"Deleted {row['expense_no']} ({fmt_money(row['amount'])})")
    flash(f"Expense {row['expense_no']} deleted.", 'success')
    return redirect(url_for('admin.expenses_list', tab='all'))


# ===========================================================================
# SYSTEM USERS (Admin user management - IT Admin only)
# ===========================================================================
@bp.route('/users')
@admin_required
def users_list():
    """List all system users. Only accessible to admins."""
    db = get_db()
    
    # Separate admin users from member users
    admin_users = db.execute(
        """SELECT u.*, m.member_no, m.full_name AS member_name, m.role AS member_role
             FROM users u
             LEFT JOIN members m ON m.id = u.member_id
            WHERE m.role IN ('IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE')
            ORDER BY u.username"""
    ).fetchall()
    
    member_users = db.execute(
        """SELECT u.*, m.member_no, m.full_name AS member_name, m.role AS member_role
             FROM users u
             LEFT JOIN members m ON m.id = u.member_id
            WHERE m.role IS NULL OR m.role = 'MEMBER'
            ORDER BY u.username"""
    ).fetchall()
    
    all_members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    
    # Find members without user accounts
    linked_member_ids = {u['member_id'] for u in admin_users + member_users if u['member_id']}
    unlinked_members = [m for m in all_members if m['id'] not in linked_member_ids]
    
    return render_template(
        'admin/users.html',
        admin_users=admin_users,
        member_users=member_users,
        unlinked_members=unlinked_members,
        all_members=all_members,
        roles=Config.ROLES,
    )


@bp.route('/users/add', methods=['GET', 'POST'])
@admin_required
def users_add():
    """Add a new system user or link a member to existing user."""
    db = get_db()
    
    if request.method == 'POST':
        action = request.form.get('action', 'create')
        
        if action == 'create':
            username = (request.form.get('username') or '').strip().lower()
            password = request.form.get('password') or 'gazebo123'
            member_id = request.form.get('member_id') or None
            
            if not username:
                flash('Username is required.', 'danger')
                return redirect(url_for('admin.users_add'))
            
            if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                flash('Username already exists.', 'danger')
                return redirect(url_for('admin.users_add'))
            
            if member_id:
                member_id = int(member_id)
                # Check if member already has a user
                if db.execute("SELECT 1 FROM users WHERE member_id=?", (member_id,)).fetchone():
                    flash('This member already has a user account.', 'warning')
                    return redirect(url_for('admin.users_add'))
            
            db.execute(
                """INSERT INTO users (username, password_hash, member_id, must_change_pw, is_active)
                   VALUES (?, ?, ?, 1, 1)""",
                (username, generate_password_hash(password), member_id if member_id else None),
            )
            db.commit()
            log_action('CREATE_USER', 'user', None, f"Created user {username}")
            flash(f'User {username} created. Default password: {password}', 'success')
            return redirect(url_for('admin.users_list'))
    
    all_members = db.execute(
        "SELECT * FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    
    return render_template('admin/users_add.html', members=all_members)


@bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def users_edit(user_id):
    """Edit user details (password, active status, etc.)."""
    db = get_db()
    user = db.execute(
        """SELECT u.*, m.member_no, m.full_name FROM users u
           LEFT JOIN members m ON m.id = u.member_id
          WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        is_active = request.form.get('is_active') == 'on'
        must_change = request.form.get('must_change_pw') == 'on'
        
        if new_password:
            db.execute(
                "UPDATE users SET password_hash=?, must_change_pw=1 WHERE id=?",
                (generate_password_hash(new_password), user_id),
            )
        
        db.execute(
            "UPDATE users SET is_active=?, must_change_pw=? WHERE id=?",
            (1 if is_active else 0, 1 if must_change else 0, user_id),
        )
        db.commit()
        log_action('UPDATE_USER', 'user', user_id, f"Updated user {user['username']}")
        flash('User updated.', 'success')
        return redirect(url_for('admin.users_list'))
    
    return render_template('admin/users_edit.html', user=user)


@bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def users_delete(user_id):
    """Delete a system login account only."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    
    # Prevent deletion if it's the current logged-in user
    if user_id == session.get('user_id'):
        flash('Cannot delete your own account.', 'danger')
        return redirect(url_for('admin.users_list'))
    
    username = user['username']
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    log_action('DELETE_USER', 'user', user_id, f"Deleted user {username}")
    flash(f'Login account {username} deleted. The linked member record remains under Members.', 'success')
    return redirect(url_for('admin.users_list'))


@bp.route('/users/<int:user_id>/delete-member', methods=['POST'])
@admin_required
def users_delete_member(user_id):
    db = get_db()
    user = db.execute(
        """SELECT u.*, m.id AS linked_member_id, m.member_no, m.full_name
             FROM users u
             LEFT JOIN members m ON m.id = u.member_id
            WHERE u.id = ?""",
        (user_id,),
    ).fetchone()

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    if not user['linked_member_id']:
        flash('This login is not linked to a member record.', 'warning')
        return redirect(url_for('admin.users_list'))
    if user_id == session.get('user_id') or user['linked_member_id'] == session.get('member_id'):
        flash('You cannot remove your own linked member record while logged in.', 'danger')
        return redirect(url_for('admin.users_list'))

    member = db.execute("SELECT * FROM members WHERE id = ?", (user['linked_member_id'],)).fetchone()
    if not member:
        flash('Linked member record not found.', 'danger')
        return redirect(url_for('admin.users_list'))

    result = _remove_member_record(db, member)
    db.commit()
    log_action('DELETE_MEMBER_FROM_USERS', 'member', member['id'],
               f"{result['mode'].capitalize()} member {member['member_no']} from System Users")
    flash(result['message'], 'success')
    return redirect(url_for('admin.users_list'))


@bp.route('/users/<int:user_id>/toggle-active', methods=['POST'])
@admin_required
def users_toggle_active(user_id):
    """Toggle user active/inactive status."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    
    if user_id == session.get('user_id'):
        flash('Cannot deactivate your own account.', 'danger')
        return redirect(url_for('admin.users_list'))
    
    new_status = 1 - user['is_active']
    db.execute("UPDATE users SET is_active=? WHERE id=?", (new_status, user_id))
    db.commit()
    
    status_text = 'activated' if new_status else 'deactivated'
    log_action('TOGGLE_USER', 'user', user_id, f"{status_text.capitalize()} user {user['username']}")
    flash(f'User {status_text}.', 'success')
    return redirect(url_for('admin.users_list'))


# ===========================================================================
# AUDIT LOG
# ===========================================================================
@bp.route('/audit')
@admin_required
def audit_list():
    db = get_db()
    rows = db.execute(
        """SELECT al.*, u.username, m.full_name
             FROM audit_log al
             LEFT JOIN users u ON u.id = al.user_id
             LEFT JOIN members m ON m.id = u.member_id
            ORDER BY al.id DESC LIMIT 200"""
    ).fetchall()
    return render_template('admin/audit_list.html', rows=rows)


# ===========================================================================
# REPORTS
# ===========================================================================
def _csv_response(filename, headers, rows):
    buff = io.StringIO()
    writer = csv.writer(buff)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return Response(
        buff.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"'
        },
    )


@bp.route('/reports')
@admin_required
def reports():
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()
    today = date.today()
    current_year = today.year

    # Balance Sheet snapshot
    total_savings = db.execute(
        """SELECT COALESCE(SUM(s.amount),0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE m.status='Active'"""
    ).fetchone()['s']

    active_loans = db.execute(
        "SELECT * FROM loans WHERE status IN ('Active','Pending')"
    ).fetchall()
    out_principal = 0
    out_interest = 0
    out_penalty = 0
    for loan in active_loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        out_principal += pos['outstanding_principal']
        out_interest += pos['outstanding_interest']
        out_penalty += pos['outstanding_penalty']

    fines_receivable = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM fines WHERE status='Unpaid'"
    ).fetchone()['s']
    fees_receivable = db.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM annual_fees
           WHERE status='Unpaid'"""
    ).fetchone()['s']
    # Income (operating)
    income_fees = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM annual_fees WHERE status='Paid'"
    ).fetchone()['s']
    income_fines = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM fines WHERE status='Paid'"
    ).fetchone()['s']
    interest_collected = db.execute(
        "SELECT COALESCE(SUM(interest_part),0) s FROM loan_repayments"
    ).fetchone()['s']

    ops = _operational_fund_snapshot(db)
    loan_interest = _loan_interest_snapshot(db)
    expenses_spent = ops['approved_expenses']
    expenses_pending = ops['pending_expenses']
    expenses_count = db.execute(
        "SELECT COUNT(*) c FROM expenses WHERE status='Approved'"
    ).fetchone()['c']

    cash_available = total_savings - out_principal
    total_assets = (
        cash_available + out_principal + out_interest + out_penalty +
        fines_receivable + fees_receivable
    )
    operating_income = int(income_fees or 0) + int(income_fines or 0) - int(expenses_spent or 0)

    # Savings trends (last 6 months for dashboard tile/chart)
    periods = all_savings_periods()[-6:]
    trend = []
    for p in periods:
        row = db.execute(
            """SELECT COALESCE(SUM(s.amount),0) s, COUNT(*) c
                 FROM savings s
                 JOIN members m ON m.id = s.member_id
                WHERE s.period=? AND m.status='Active'""",
            (p,),
        ).fetchone()
        trend.append({
            'period': p, 'label': period_label(p),
            'total': row['s'] or 0, 'count': row['c'] or 0,
        })

    # Member stats
    active_members = db.execute(
        "SELECT COUNT(*) c FROM members WHERE status='Active'"
    ).fetchone()['c']
    exited_members = db.execute(
        "SELECT COUNT(*) c FROM members WHERE status='Exited'"
    ).fetchone()['c']

    # Loan stats
    loan_totals = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(principal),0) s FROM loans"
    ).fetchone()
    pending_loans = db.execute(
        "SELECT COUNT(*) c FROM loans WHERE status='Pending'"
    ).fetchone()['c']
    active_loan_count = db.execute(
        "SELECT COUNT(*) c FROM loans WHERE status='Active'"
    ).fetchone()['c']

    overdue_loans_count = 0
    for loan in active_loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        if pos['is_overdue']:
            overdue_loans_count += 1

    # Fees/Fines stats (current year)
    fees_year = db.execute(
        """SELECT
                 COALESCE(SUM(CASE WHEN status='Paid' THEN amount ELSE 0 END),0) paid_amount,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN amount ELSE 0 END),0) unpaid_amount,
                 COALESCE(SUM(CASE WHEN status='Paid' THEN 1 ELSE 0 END),0) paid_count,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN 1 ELSE 0 END),0) unpaid_count
              FROM annual_fees
              WHERE year=?""",
        (current_year,),
    ).fetchone()
    fines_year = db.execute(
        """SELECT
                 COALESCE(SUM(CASE WHEN status='Paid' THEN amount ELSE 0 END),0) paid_amount,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN amount ELSE 0 END),0) unpaid_amount,
                 COALESCE(SUM(CASE WHEN status='Paid' THEN 1 ELSE 0 END),0) paid_count,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN 1 ELSE 0 END),0) unpaid_count
              FROM fines
              WHERE strftime('%Y', fine_date)=?""",
        (str(current_year),),
    ).fetchone()

    # Minutes & dividends stats
    minutes_stats = db.execute(
        """SELECT
                 COUNT(*) total,
                 COALESCE(SUM(CASE WHEN is_published=1 THEN 1 ELSE 0 END),0) published
              FROM minutes"""
    ).fetchone()
    dividend_stats = db.execute(
        """SELECT
                 COUNT(*) total,
                 COALESCE(SUM(CASE WHEN status IN ('Published','Distributed') THEN 1 ELSE 0 END),0) published,
                 COALESCE(SUM(total_interest_pool),0) total_pool
              FROM dividend_runs"""
    ).fetchone()

    audit_recent = db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE created_at >= datetime('now','-30 day')"
    ).fetchone()['c']

    report_tiles = [
        # ── Financial Overview ────────────────────────────────────────
        {
            'key': 'position', 'group': 'Financial Overview',
            'title': 'Financial Position',
            'icon': 'bi bi-bank2',
            'value': fmt_money(total_assets),
            'sub': f"Cash {fmt_money(cash_available)} | Equity {fmt_money(total_savings)}",
            'href': url_for('admin.reports'),
            'tone': 'info',
        },
        {
            'key': 'members', 'group': 'Financial Overview',
            'title': 'Members Register',
            'icon': 'bi bi-people-fill',
            'value': str(active_members),
            'sub': f"Active members | Exited: {exited_members}",
            'href': url_for('admin.members_list'),
            'tone': 'gold',
        },
        # ── Savings ───────────────────────────────────────────────────
        {
            'key': 'savings-trend', 'group': 'Savings',
            'title': 'Savings Trend',
            'icon': 'bi bi-graph-up-arrow',
            'value': fmt_money(sum(t['total'] for t in trend)),
            'sub': 'Last 6 months collection performance',
            'href': url_for('admin.savings_list'),
            'tone': 'accent',
        },
        # ── Loans ─────────────────────────────────────────────────────
        {
            'key': 'loans', 'group': 'Loans',
            'title': 'Loans Portfolio',
            'icon': 'bi bi-cash-stack',
            'value': fmt_money(loan_totals['s'] or 0),
            'sub': f"Active: {active_loan_count} | Pending: {pending_loans} | Overdue: {overdue_loans_count}",
            'href': url_for('admin.loans_list'),
            'tone': 'warn',
        },
        {
            'key': 'loan-interest-collected', 'group': 'Loans',
            'title': 'Loan Interest Collected',
            'icon': 'bi bi-cash-coin',
            'value': fmt_money(loan_interest['collected']),
            'sub': f"Cash-realized interest to date | Projection open: {fmt_money(loan_interest['projected_open'])}",
            'href': url_for('admin.loans_list'),
            'tone': 'ok',
        },
        {
            'key': 'loan-interest-projection', 'group': 'Loans',
            'title': 'Loan Interest Projection',
            'icon': 'bi bi-graph-up-arrow',
            'value': fmt_money(loan_interest['projected_open']),
            'sub': f"Outstanding interest {fmt_money(loan_interest['projected_interest'])} + penalties {fmt_money(loan_interest['projected_penalty'])} | Collected: {fmt_money(loan_interest['collected'])}",
            'href': url_for('admin.loans_list'),
            'tone': 'accent',
        },
        # ── Fees & Fines ──────────────────────────────────────────────
        {
            'key': 'fees', 'group': 'Fees & Fines',
            'title': f'Annual Fees {current_year}',
            'icon': 'bi bi-patch-check-fill',
            'value': fmt_money(fees_year['paid_amount'] or 0),
            'sub': f"Paid records: {fees_year['paid_count']} | Unpaid records: {fees_year['unpaid_count']}",
            'href': url_for('admin.fees_list'),
            'tone': 'ok',
        },
        {
            'key': 'fines', 'group': 'Fees & Fines',
            'title': f'Fines {current_year}',
            'icon': 'bi bi-exclamation-octagon-fill',
            'value': fmt_money(fines_year['paid_amount'] or 0),
            'sub': f"Collected: {fines_year['paid_count']} | Outstanding: {fines_year['unpaid_count']}",
            'href': url_for('admin.fines_list'),
            'tone': 'danger',
        },
        # ── Operations ────────────────────────────────────────────────
        {
            'key': 'expenses', 'group': 'Operations',
            'title': 'Expenses (Approved)',
            'icon': 'bi bi-wallet2',
            'value': fmt_money(expenses_spent),
            'sub': f"{expenses_count} approved | Pending requests: {fmt_money(expenses_pending)}",
            'href': url_for('admin.expenses_list'),
            'tone': 'warn',
        },
        # ── Governance ────────────────────────────────────────────────
        {
            'key': 'minutes', 'group': 'Governance',
            'title': 'Minutes & Governance',
            'icon': 'bi bi-journal-richtext',
            'value': str(minutes_stats['published'] or 0),
            'sub': f"Published minutes | Total records: {minutes_stats['total']}",
            'href': url_for('admin.minutes_list'),
            'tone': 'info',
        },
        {
            'key': 'dividends', 'group': 'Governance',
            'title': 'Dividends Runs',
            'icon': 'bi bi-award-fill',
            'value': str(dividend_stats['published'] or 0),
            'sub': f"Published runs | Total pool: {fmt_money(dividend_stats['total_pool'] or 0)}",
            'href': url_for('admin.dividends_list'),
            'tone': 'gold',
        },
        {
            'key': 'audit', 'group': 'Governance',
            'title': 'Audit & Controls',
            'icon': 'bi bi-shield-check',
            'value': str(audit_recent),
            'sub': 'Audit entries in last 30 days',
            'href': url_for('admin.audit_list'),
            'tone': 'accent',
        },
    ]

    return render_template(
        'admin/reports.html',
        current_year=current_year,
        total_savings=total_savings,
        total_assets=total_assets,
        out_principal=out_principal,
        out_interest=out_interest,
        out_penalty=out_penalty,
        fines_receivable=fines_receivable,
        fees_receivable=fees_receivable,
        income_fees=income_fees,
        income_fines=income_fines,
        interest_collected=interest_collected,
        expenses_spent=expenses_spent,
        operating_income=operating_income,
        cash_available=cash_available,
        loan_total_count=loan_totals['c'] or 0,
        active_members=active_members,
        report_tiles=report_tiles,
        trend=trend,
    )


@bp.route('/reports/export/<report_key>')
@admin_required
def reports_export(report_key):
    db = get_db()
    _normalize_legacy_archived_status(db)
    db.commit()

    if report_key == 'position':
        total_savings = db.execute(
            """SELECT COALESCE(SUM(s.amount),0) s
                 FROM savings s
                 JOIN members m ON m.id = s.member_id
                WHERE m.status='Active'"""
        ).fetchone()['s']
        active_loans = db.execute("SELECT * FROM loans WHERE status IN ('Active','Pending')").fetchall()
        out_principal = out_interest = out_penalty = 0
        for loan in active_loans:
            repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)).fetchall()
            pens = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)).fetchall()
            pos = calculate_loan_position(loan, repays, pens)
            out_principal += pos['outstanding_principal']
            out_interest += pos['outstanding_interest']
            out_penalty += pos['outstanding_penalty']
        fines_receivable = db.execute("SELECT COALESCE(SUM(amount),0) s FROM fines WHERE status='Unpaid'").fetchone()['s']
        fees_receivable = db.execute("SELECT COALESCE(SUM(amount),0) s FROM annual_fees WHERE status='Unpaid'").fetchone()['s']
        cash_available = total_savings - out_principal
        rows = [
            ('Cash available', cash_available),
            ('Loans outstanding principal', out_principal),
            ('Interest receivable', out_interest),
            ('Penalty receivable', out_penalty),
            ('Fines receivable', fines_receivable),
            ('Annual fees receivable', fees_receivable),
            ('Total member savings (equity)', total_savings),
        ]
        return _csv_response('financial_position.csv', ['Metric', 'Amount'], rows)

    if report_key == 'savings-trend':
        rows = []
        for p in all_savings_periods():
            r = db.execute(
                """SELECT COALESCE(SUM(s.amount),0) s, COUNT(*) c
                     FROM savings s
                     JOIN members m ON m.id = s.member_id
                    WHERE s.period=? AND m.status='Active'""",
                (p,),
            ).fetchone()
            rows.append((period_label(p), r['c'] or 0, r['s'] or 0))
        return _csv_response('savings_trend.csv', ['Period', 'Contributors', 'Total Amount'], rows)

    if report_key == 'members':
        rows = db.execute(
            """SELECT member_no, full_name, role, status, join_date, phone, email
                 FROM members
                ORDER BY member_no"""
        ).fetchall()
        out = [(r['member_no'], r['full_name'], r['role'], r['status'], r['join_date'], r['phone'], r['email']) for r in rows]
        return _csv_response('members_register.csv', ['Member No', 'Full Name', 'Role', 'Status', 'Join Date', 'Phone', 'Email'], out)

    if report_key == 'loans':
        loans = db.execute(
            """SELECT l.*, m.member_no, m.full_name
                 FROM loans l
                 LEFT JOIN members m ON m.id = l.member_id
                ORDER BY l.issued_date DESC"""
        ).fetchall()
        out = []
        for loan in loans:
            repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)).fetchall()
            pens = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)).fetchall()
            pos = calculate_loan_position(loan, repays, pens)
            out.append((
                loan['loan_no'], loan['member_no'], loan['full_name'], loan['status'],
                loan['principal'], loan['issued_date'], loan['due_date'], pos['total_outstanding']
            ))
        return _csv_response(
            'loans_portfolio.csv',
            ['Loan No', 'Member No', 'Member Name', 'Status', 'Principal', 'Issued Date', 'Due Date', 'Outstanding Total'],
            out,
        )

    if report_key == 'fees':
        rows = db.execute(
            """SELECT af.year, m.member_no, m.full_name, af.amount, af.status, af.paid_date, af.deadline
                 FROM annual_fees af
                 LEFT JOIN members m ON m.id = af.member_id
                ORDER BY af.year DESC, m.member_no"""
        ).fetchall()
        out = [(r['year'], r['member_no'], r['full_name'], r['amount'], r['status'], r['paid_date'], r['deadline']) for r in rows]
        return _csv_response('annual_fees.csv', ['Year', 'Member No', 'Member Name', 'Amount', 'Status', 'Payment Date', 'Deadline'], out)

    if report_key == 'fines':
        rows = db.execute(
            """SELECT f.fine_date, m.member_no, m.full_name, f.violation, f.amount, f.status
                 FROM fines f
                 LEFT JOIN members m ON m.id = f.member_id
                ORDER BY f.fine_date DESC"""
        ).fetchall()
        out = [(r['fine_date'], r['member_no'], r['full_name'], r['violation'], r['amount'], r['status']) for r in rows]
        return _csv_response('fines_report.csv', ['Fine Date', 'Member No', 'Member Name', 'Violation', 'Amount', 'Status'], out)

    if report_key == 'expenses':
        rows = db.execute(
            """SELECT e.expense_no, e.expense_date, e.amount, e.purpose, e.category, e.status,
                       COALESCE(reqm.full_name, req.username) requested_by,
                       COALESCE(apprm.full_name, appr.username) approved_by,
                       e.approved_at
                 FROM expenses e
                 LEFT JOIN users req ON req.id = e.requested_by
                 LEFT JOIN members reqm ON reqm.id = req.member_id
                 LEFT JOIN users appr ON appr.id = e.approved_by
                 LEFT JOIN members apprm ON apprm.id = appr.member_id
                ORDER BY e.expense_date DESC, e.id DESC"""
        ).fetchall()
        out = [(
            r['expense_no'], r['expense_date'], r['amount'], r['purpose'], r['category'],
            r['status'], r['requested_by'], r['approved_by'], r['approved_at']
        ) for r in rows]
        return _csv_response(
            'expenses.csv',
            ['Expense No', 'Date', 'Amount', 'Purpose', 'Category', 'Status', 'Requested By', 'Approved By', 'Approved At'],
            out,
        )

    if report_key == 'loan-interest-collected':
        rows = db.execute(
            """SELECT lr.payment_date, l.loan_no, m.member_no, m.full_name,
                       lr.amount, lr.principal_part, lr.interest_part, lr.penalty_part
                 FROM loan_repayments lr
                 JOIN loans l ON l.id = lr.loan_id
                 LEFT JOIN members m ON m.id = l.member_id
                ORDER BY lr.payment_date DESC, lr.id DESC"""
        ).fetchall()
        out = [(
            r['payment_date'], r['loan_no'], r['member_no'], r['full_name'],
            r['amount'], r['principal_part'], r['interest_part'], r['penalty_part']
        ) for r in rows]
        return _csv_response(
            'loan_interest_collected.csv',
            ['Payment Date', 'Loan No', 'Member No', 'Member Name', 'Total Payment', 'Principal Part', 'Interest Part', 'Penalty Part'],
            out,
        )

    if report_key == 'loan-interest-projection':
        loans = db.execute(
            """SELECT l.*, m.member_no, m.full_name
                 FROM loans l
                 LEFT JOIN members m ON m.id = l.member_id
                WHERE l.status IN ('Active','Pending')
                ORDER BY l.issued_date DESC, l.id DESC"""
        ).fetchall()
        out = []
        for loan in loans:
            repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)).fetchall()
            pens = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)).fetchall()
            pos = calculate_loan_position(loan, repays, pens)
            out.append((
                loan['loan_no'], loan['member_no'], loan['full_name'], loan['status'],
                int(loan['principal'] or 0), pos['outstanding_principal'],
                pos['outstanding_interest'], pos['outstanding_penalty'],
            ))
        return _csv_response(
            'loan_interest_projection.csv',
            ['Loan No', 'Member No', 'Member Name', 'Status', 'Principal', 'Outstanding Principal', 'Projected Interest', 'Projected Penalty'],
            out,
        )

    if report_key == 'minutes':
        rows = db.execute(
            """SELECT m.meeting_date, m.title, mem.member_no AS chair_no,
                       mem.full_name AS chair_name, m.is_published
                 FROM minutes m
                 LEFT JOIN members mem ON mem.id = m.chaired_by
                ORDER BY m.meeting_date DESC"""
        ).fetchall()
        out = [(r['meeting_date'], r['title'], r['chair_no'], r['chair_name'], 'Published' if r['is_published'] else 'Hidden') for r in rows]
        return _csv_response('minutes_governance.csv', ['Meeting Date', 'Title', 'Chaired By No', 'Chaired By Name', 'Visibility'], out)

    if report_key == 'dividends':
        rows = db.execute(
            """SELECT year, total_interest_pool, distributable_pool, member_count,
                       per_member_amount, status, distribution_date
                 FROM dividend_runs
                ORDER BY year DESC"""
        ).fetchall()
        out = [(r['year'], r['total_interest_pool'], r['distributable_pool'], r['member_count'], r['per_member_amount'], r['status'], r['distribution_date']) for r in rows]
        return _csv_response('dividends_runs.csv', ['Year', 'Total Pool', 'Distributable Pool', 'Members', 'Per Member', 'Status', 'Distribution Date'], out)

    if report_key == 'audit':
        rows = db.execute(
            """SELECT al.created_at, COALESCE(m.full_name, u.username, 'System') actor,
                       al.action, al.entity_type, al.entity_id, al.description
                 FROM audit_log al
                 LEFT JOIN users u ON u.id = al.user_id
                 LEFT JOIN members m ON m.id = u.member_id
                ORDER BY al.id DESC
                LIMIT 1000"""
        ).fetchall()
        out = [(r['created_at'], r['actor'], r['action'], r['entity_type'], r['entity_id'], r['description']) for r in rows]
        return _csv_response('audit_controls.csv', ['Created At', 'Actor', 'Action', 'Entity Type', 'Entity ID', 'Detail'], out)

    flash('Unknown report export requested.', 'warning')
    return redirect(url_for('admin.reports'))
