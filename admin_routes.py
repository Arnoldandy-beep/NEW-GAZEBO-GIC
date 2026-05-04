"""
GAZEBO Investment Club - Administrative routes
Handles members, savings, loans, dividends, minutes, fees, fines, settings
"""
import os
import uuid
import json
import csv
import io
import struct
from urllib.parse import quote
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
    next_member_no, next_loan_no, build_whatsapp_link, send_telegram_test_message,
    notify_user as _notify_user_util,
    notify_member as _notify_member_util,
    notify_roles as _notify_roles_util,
    deduct_member_savings,
)
from config import Config

bp = Blueprint('admin', __name__, url_prefix='/admin')

# Magic-byte signatures for allowed file types
_IMAGE_MAGIC = {
    b'\xff\xd8\xff':           'jpg',   # JPEG
    b'\x89PNG\r\n\x1a\n':     'png',   # PNG
    b'GIF87a':                 'gif',   # GIF87
    b'GIF89a':                 'gif',   # GIF89
    b'RIFF':                   'webp',  # WEBP (RIFF....WEBP)
}
_PDF_MAGIC = b'%PDF'


def _file_magic_ok(file_obj, allowed_exts):
    """Read first 12 bytes and verify against known signatures. Rewinds stream afterwards."""
    header = file_obj.read(12)
    file_obj.seek(0)
    ext_set = set(allowed_exts)

    if 'pdf' in ext_set and header[:4] == _PDF_MAGIC:
        return True
    for sig, ftype in _IMAGE_MAGIC.items():
        if ftype in ext_set and header[:len(sig)] == sig:
            return True
        # WEBP special: bytes 0-3 are RIFF, bytes 8-11 are WEBP
        if ftype == 'webp' and 'webp' in ext_set:
            if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                return True
    return False


def _save_upload(file_field_name, subfolder=''):
    """Save an uploaded image. Returns relative path like 'uploads/photos/abc.jpg' or None."""
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in Config.ALLOWED_IMAGE_EXTENSIONS:
        return None
    if not _file_magic_ok(f, Config.ALLOWED_IMAGE_EXTENSIONS):
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
    if not _file_magic_ok(f, ALLOWED):
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
    if not _file_magic_ok(f, allowed):
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
    if not _file_magic_ok(f, allowed):
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    dest_dir = os.path.join(Config.UPLOAD_FOLDER, 'minutes')
    os.makedirs(dest_dir, exist_ok=True)
    f.save(os.path.join(dest_dir, filename))
    return f"uploads/minutes/{filename}"


def _save_membership_form(file_field_name='membership_form_file'):
    """Save signed membership registration form (image or PDF)."""
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    allowed = {'pdf', 'jpg', 'jpeg', 'png', 'webp'}
    if ext not in allowed:
        return None
    if not _file_magic_ok(f, allowed):
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    dest_dir = os.path.join(Config.UPLOAD_FOLDER, 'membership-forms')
    os.makedirs(dest_dir, exist_ok=True)
    f.save(os.path.join(dest_dir, filename))
    return f"uploads/membership-forms/{filename}"


def _notify_roles(db, roles, title, message, link=None, exclude_user_id=None):
    _notify_roles_util(db, roles, title, message, link, exclude_user_id)


def _notify_user(db, user_id, title, message, link=None):
    _notify_user_util(db, user_id, title, message, link)


def _notify_member(db, member_id, title, message, link=None):
    _notify_member_util(db, member_id, title, message, link)


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


_DOC_NO_QUERIES = {
    ('operational_incomes', 'income_no'):  "SELECT income_no  AS no FROM operational_incomes ORDER BY id DESC LIMIT 1",
    ('expenses',            'expense_no'): "SELECT expense_no AS no FROM expenses           ORDER BY id DESC LIMIT 1",
}


def _next_doc_no(db, table_name, column_name, prefix):
    key = (table_name, column_name)
    sql = _DOC_NO_QUERIES.get(key)
    if sql is None:
        raise ValueError(f"_next_doc_no: unknown table/column pair ({table_name!r}, {column_name!r})")
    row = db.execute(sql).fetchone()
    seq = 1
    if row and row['no']:
        try:
            seq = int(str(row['no']).split('-')[-1]) + 1
        except (TypeError, ValueError):
            seq = 1
    return f"{prefix}-{seq:04d}"


def _loan_formal_reminder_text(loan_no, borrower_name, amount_owed, term_months, days_overdue, penalty):
    return (
        "FORMAL REPAYMENT NOTICE\n"
        f"Borrower: {borrower_name}\n"
        f"Loan Reference: {loan_no}\n"
        f"Amount Owed (Outstanding Balance): {fmt_money(amount_owed)}\n"
        f"Initial Loan Period Requested (Months): {term_months}\n"
        f"Overdue Duration (Days Late): {days_overdue}\n"
        f"Penalty Interest Accrued: {fmt_money(penalty)}\n\n"
        "This is a formal demand for repayment under the lending terms and conditions of the club. "
        "You are required to settle the outstanding balance immediately. Continued default may result "
        "in additional charges, recovery action, and administrative sanctions in line with approved "
        "lending standards."
    )


def _password_reset_whatsapp_message(full_name, username, temp_password, reason):
    reason_text = (reason or 'Administrative account recovery').strip()
    return (
        f"Hello {full_name},\n\n"
        f"Your password was reset because: {reason_text}.\n"
        f"Username: {username}\n"
        f"Temporary Password: {temp_password}\n\n"
        "Please sign in and set your preferred password immediately.\n"
        "Password guide: use at least 8 characters with uppercase, lowercase, and a number.\n"
        f"App URL: {Config.APP_URL}\n\n"
        "Thank you."
    )


def _render_whatsapp_reset_redirect(wa_url, return_url):
    return render_template(
        'admin/whatsapp_open.html',
        wa_url=wa_url,
        return_url=return_url,
    )


def _registration_credentials_whatsapp_message(full_name, member_no, username, temp_password):
    return (
        f"Hello {full_name},\n\n"
        f"Welcome to {Config.CLUB_NAME}. Your membership registration has been approved.\n"
        f"Member Number: {member_no}\n"
        f"Username: {username}\n"
        f"Temporary Password: {temp_password}\n\n"
        "Please sign in and change your password immediately before using the system.\n"
        "Password guide: use at least 8 characters with uppercase, lowercase, and a number.\n"
        f"App URL: {Config.APP_URL}\n\n"
        "We are glad to have you on board."
    )


def _create_member_and_user_from_registration_request(db, reg_request):
    try:
        payload = json.loads(reg_request['payload_json'] or '{}')
    except (TypeError, ValueError):
        payload = {}

    national_id = (payload.get('national_id') or '').strip()
    phone = (payload.get('phone') or '').strip()
    email = (payload.get('email') or '').strip()
    existing_member = db.execute(
        """SELECT id FROM members
             WHERE national_id=? OR phone=? OR (email <> '' AND LOWER(email)=LOWER(?))
             LIMIT 1""",
        (national_id, phone, email),
    ).fetchone()
    if existing_member:
        raise ValueError('A member already exists with the same National ID, phone, or email.')

    member_no = next_member_no(db)
    full_name = (payload.get('full_name') or reg_request['full_name'] or '').strip()
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
        (
            member_no,
            full_name,
            (payload.get('surname') or '').strip().upper(),
            (payload.get('first_name') or '').strip().upper(),
            (payload.get('other_names') or '').strip().upper(),
            payload.get('gender') or '',
            payload.get('date_of_birth') or None,
            (payload.get('nationality') or 'Ugandan').strip(),
            payload.get('marital_status') or '',
            national_id,
            (payload.get('tin') or '').strip(),
            payload.get('nid_copy_url'),
            phone,
            (payload.get('alt_phone') or '').strip(),
            (payload.get('whatsapp_no') or '').strip(),
            email,
            (payload.get('district') or 'Kampala').strip(),
            (payload.get('village_street') or '').strip(),
            payload.get('employment_status') or '',
            (payload.get('occupation') or '').strip(),
            (payload.get('employer_business') or '').strip(),
            payload.get('monthly_income'),
            float(payload.get('monthly_contribution') or 100000),
            int(payload.get('preferred_pay_date') or 5),
            (payload.get('mobile_money_no') or '').strip(),
            (payload.get('bank_account_no') or '').strip(),
            (payload.get('next_of_kin') or '').strip(),
            (payload.get('nok_relationship') or '').strip(),
            (payload.get('nok_phone') or '').strip(),
            (payload.get('nok_address') or '').strip(),
            'MEMBER',
            date.today().isoformat(),
            '',
            'Active',
            ((payload.get('notes') or '').strip() + f"\n\nOnline registration request #{reg_request['id']} submitted via public portal.").strip(),
            payload.get('photo_url'),
        ),
    )
    member_id = cur.lastrowid

    username = member_no.lower()
    base = username
    i = 1
    while db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        i += 1
        username = f"{base}-{i}"
    temp_password = 'gazebo123'
    cur = db.execute(
        """INSERT INTO users (username, password_hash, member_id, must_change_pw, is_active)
           VALUES (?, ?, ?, 1, 1)""",
        (username, generate_password_hash(temp_password), member_id),
    )
    user_id = cur.lastrowid
    return {
        'member_id': member_id,
        'user_id': user_id,
        'member_no': member_no,
        'username': username,
        'temp_password': temp_password,
        'full_name': full_name,
        'phone': (payload.get('whatsapp_no') or phone or '').strip(),
    }


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
    row = db.execute(
        "SELECT COALESCE(SUM(interest_part),0) i, COALESCE(SUM(penalty_part),0) p FROM loan_repayments"
    ).fetchone()
    collected     = int(row['i'] or 0)
    pen_collected = int(row['p'] or 0)

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
        projected_penalty  += int(pos['outstanding_penalty'] or 0)

    return {
        'collected':          collected,
        'penalty_collected':  pen_collected,
        'total_income':       collected + pen_collected,
        'projected_open':     projected_interest + projected_penalty,
        'projected_interest': projected_interest,
        'projected_penalty':  projected_penalty,
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

    # Total savings collected from active members
    total_savings = db.execute(
        """SELECT COALESCE(SUM(s.amount), 0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE m.status = 'Active'"""
    ).fetchone()['s']

    # Loan stats — grouped by status
    loan_stats = db.execute(
        """SELECT status, COUNT(*) c, COALESCE(SUM(principal),0) p
             FROM loans GROUP BY status"""
    ).fetchall()
    loan_summary = {row['status']: dict(row) for row in loan_stats}

    # Extended loan metrics
    loans_total_count = sum(v.get('c', 0) for v in loan_summary.values())
    loans_all_disbursed_amount = db.execute(
        "SELECT COALESCE(SUM(principal),0) s FROM loans WHERE disbursed_date IS NOT NULL"
    ).fetchone()['s']
    loans_cleared_count  = loan_summary.get('Cleared',  {}).get('c', 0)
    loans_cleared_amount = loan_summary.get('Cleared',  {}).get('p', 0)
    loans_active_count   = loan_summary.get('Active',   {}).get('c', 0)
    loans_active_amount  = loan_summary.get('Active',   {}).get('p', 0)
    loans_pending_count  = loan_summary.get('Pending',  {}).get('c', 0)

    # Outstanding from active/pending loans
    active_loans = db.execute(
        """SELECT l.*, m.member_no, m.full_name, m.phone
             FROM loans l
             LEFT JOIN members m ON m.id = l.member_id
            WHERE l.status IN ('Active','Pending')"""
    ).fetchall()

    outstanding_principal = 0
    outstanding_interest  = 0
    outstanding_penalty   = 0
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
        outstanding_interest  += pos['outstanding_interest']
        outstanding_penalty   += pos['outstanding_penalty']
        if pos['is_overdue']:
            overdue_count += 1
            wa_msg = (
                f"FORMAL REPAYMENT NOTICE\n"
                f"Borrower: {loan['full_name']}\n"
                f"Loan Ref: {loan['loan_no']}\n"
                f"Outstanding Balance: {fmt_money(pos['total_outstanding'])}\n"
                f"Overdue: {pos['overdue_months']} month(s) / {pos['days_overdue']} day(s)\n"
                f"Penalty Accrued: {fmt_money(pos['outstanding_penalty'])}\n\n"
                f"This is a formal demand for repayment under the lending terms of "
                f"{Config.CLUB_SHORT_NAME}. Please settle your outstanding balance immediately."
            )
            overdue_loans.append({
                'id':            loan['id'],
                'loan_no':       loan['loan_no'],
                'member_no':     loan['member_no'],
                'full_name':     loan['full_name'],
                'phone':         loan['phone'],
                'amount_owed':   pos['total_outstanding'],
                'overdue_months':pos['overdue_months'],
                'days_overdue':  pos['days_overdue'],
                'penalty':       pos['outstanding_penalty'],
                'wa_notice_url': build_whatsapp_link(loan['phone'], wa_msg),
            })

    loans_overdue_total_owed = sum(l['amount_owed'] for l in overdue_loans)

    # Date context
    today = date.today()
    cur_period   = period_str(today)
    current_year = today.year

    # Current month savings total
    month_savings_total = db.execute(
        """SELECT COALESCE(SUM(s.amount), 0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE s.period = ? AND m.status = 'Active'""",
        (cur_period,),
    ).fetchone()['s']

    # Build paid-periods map
    active_members_rows = db.execute(
        "SELECT id, member_no, full_name, phone, join_date FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()
    all_paid_rows = db.execute("SELECT member_id, period FROM savings").fetchall()
    paid_map = {}
    for r in all_paid_rows:
        paid_map.setdefault(r['member_id'], set()).add(r['period'])

    periods_to_date = all_savings_periods(today)

    # Single pass: current-month defaulters + total arrears across all members
    current_month_defaulters  = []
    total_arrears_members_count = 0
    total_arrears_amount_all    = 0

    for m in active_members_rows:
        paid_periods = paid_map.get(m['id'], set())
        join_date = m['join_date']
        if isinstance(join_date, str):
            join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
        else:
            join_d = join_date
        join_period = period_str(join_d)
        expected = [p for p in periods_to_date if p >= join_period]

        # Overdue = past months only (strictly < current month).
        # Current month is a reminder — not yet in arrears until the month closes.
        overdue_missing  = [p for p in expected if p < cur_period and p not in paid_periods]
        cur_month_unpaid = cur_period in expected and cur_period not in paid_periods
        arrears_amount   = len(overdue_missing) * Config.MONTHLY_SAVINGS_AMOUNT

        if overdue_missing:
            total_arrears_members_count += 1
            total_arrears_amount_all    += arrears_amount

        # Show in current-month defaulters table if current month is unpaid
        if cur_month_unpaid:
            if overdue_missing:
                overdue_labels = ', '.join(period_label(p) for p in overdue_missing[:5])
                if len(overdue_missing) > 5:
                    overdue_labels += f' + {len(overdue_missing) - 5} more'
                total_outstanding = arrears_amount + Config.MONTHLY_SAVINGS_AMOUNT
                wa_msg = (
                    f"Hello {m['full_name']}, this is a savings reminder from "
                    f"{Config.CLUB_SHORT_NAME}.\n\n"
                    f"OVERDUE ({len(overdue_missing)} month{'s' if len(overdue_missing) != 1 else ''}):\n"
                    f"{overdue_labels}\n"
                    f"Overdue arrears: {fmt_money(arrears_amount)}\n\n"
                    f"CURRENT MONTH ({period_label(cur_period)}): "
                    f"{fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)}\n\n"
                    f"Total outstanding: {fmt_money(total_outstanding)}\n\n"
                    "Please urgently settle your outstanding balance. Thank you."
                )
            else:
                wa_msg = (
                    f"Hello {m['full_name']}, this is a savings reminder from "
                    f"{Config.CLUB_SHORT_NAME}.\n\n"
                    f"Your savings for {period_label(cur_period)} "
                    f"({fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)}) have not yet been received.\n\n"
                    "Kindly make your deposit at the earliest. Thank you."
                )
            current_month_defaulters.append({
                'id':            m['id'],
                'member_no':     m['member_no'],
                'full_name':     m['full_name'],
                'phone':         m['phone'],
                'arrears_amount':arrears_amount,
                'missing_count': len(overdue_missing),
                'wa_notice_url': build_whatsapp_link(m['phone'], wa_msg),
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
    month_outstanding_amount  = month_outstanding_members * Config.MONTHLY_SAVINGS_AMOUNT

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
    loan_interest_collected          = loan_interest['collected']
    loan_penalty_collected           = loan_interest['penalty_collected']
    loan_income_total                = loan_interest['total_income']
    loan_interest_projected          = loan_interest['projected_open']
    loan_interest_projected_interest = loan_interest['projected_interest']
    loan_interest_projected_penalty  = loan_interest['projected_penalty']

    # Bank account balance estimate:
    #   IN  = savings + fees + fines + loan repayments received
    #   OUT = loans disbursed + approved expenses
    total_repayments_in = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM loan_repayments"
    ).fetchone()['s']
    bank_balance_estimate = (
        int(total_savings)
        + int(ops['fees_collected'])
        + int(ops['fines_collected'])
        + int(total_repayments_in)
        - int(loans_all_disbursed_amount)
        - int(ops['approved_expenses'])
    )

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

    # Loan pool = savings not yet lent out (available to lend)
    cash_available   = total_savings - outstanding_principal
    pool_utilization = round((outstanding_principal / total_savings) * 100, 1) if total_savings > 0 else 0

    # Savings trend — last 6 months
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
        trend.append({'period': p, 'label': period_label(p), 'total': row['s'] or 0, 'count': row['c'] or 0})

    # Savings trend — last 5 years
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
        yearly_trend.append({'year': y, 'total': row['s'] or 0})

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
        overdue_loans=overdue_loans,
        loans_overdue_total_owed=loans_overdue_total_owed,
        loans_total_count=loans_total_count,
        loans_all_disbursed_amount=loans_all_disbursed_amount,
        loans_cleared_count=loans_cleared_count,
        loans_cleared_amount=loans_cleared_amount,
        loans_active_count=loans_active_count,
        loans_active_amount=loans_active_amount,
        loans_pending_count=loans_pending_count,
        bank_balance_estimate=bank_balance_estimate,
        total_arrears_members_count=total_arrears_members_count,
        total_arrears_amount_all=total_arrears_amount_all,
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
        loan_penalty_collected=loan_penalty_collected,
        loan_income_total=loan_income_total,
        loan_interest_projected=loan_interest_projected,
        loan_interest_projected_interest=loan_interest_projected_interest,
        loan_interest_projected_penalty=loan_interest_projected_penalty,
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
# APPROVALS
# ===========================================================================
@bp.route('/approvals')
@admin_required
def approvals():
    db = get_db()
    role = session.get('role')
    user_id = session.get('user_id')

    def _approval_names(loan_id, stage, amendment_id=None):
        if amendment_id is None:
            rows = db.execute(
                """SELECT COALESCE(m.full_name, u.username) AS approver_name
                     FROM loan_approvals la
                     JOIN users u ON u.id = la.user_id
                     LEFT JOIN members m ON m.id = u.member_id
                    WHERE la.loan_id=? AND la.stage=? AND la.decision='APPROVE'
                    ORDER BY la.created_at ASC""",
                (loan_id, stage),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT COALESCE(m.full_name, u.username) AS approver_name
                     FROM loan_approvals la
                     JOIN users u ON u.id = la.user_id
                     LEFT JOIN members m ON m.id = u.member_id
                    WHERE la.loan_id=? AND la.amendment_id=? AND la.stage=? AND la.decision='APPROVE'
                    ORDER BY la.created_at ASC""",
                (loan_id, amendment_id, stage),
            ).fetchall()
        return [r['approver_name'] for r in rows]

    # Pending loan amendments this user can approve (not their own)
    pending_amendments = []
    if role in ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE', 'IT_ADMIN'):
        pending_amendments = db.execute(
            """SELECT la.*, l.loan_no, mbr.full_name AS borrower_name, mbr.member_no,
                      rmb.full_name AS requested_by_name,
                      (SELECT COUNT(DISTINCT user_id) FROM loan_approvals lap
                        WHERE lap.loan_id=la.loan_id AND lap.amendment_id=la.id
                          AND lap.stage='AMENDMENT' AND lap.decision='APPROVE') AS approval_count,
                      EXISTS(SELECT 1 FROM loan_approvals lax
                              WHERE lax.loan_id=la.loan_id AND lax.amendment_id=la.id
                                AND lax.stage='AMENDMENT' AND lax.user_id=?) AS already_approved
               FROM loan_amendments la
               JOIN loans l ON l.id = la.loan_id
               JOIN members mbr ON mbr.id = l.member_id
               JOIN users ru ON ru.id = la.requested_by
               JOIN members rmb ON rmb.id = ru.member_id
              WHERE la.status = 'Pending' AND la.requested_by != ?
              ORDER BY la.created_at DESC""",
            (user_id, user_id),
        ).fetchall()

    # Pending expense requests (Chairman and IT_ADMIN approve)
    pending_expenses = []
    if role in ('CHAIRMAN', 'IT_ADMIN'):
        pending_expenses = db.execute(
            """SELECT e.*, rm.full_name AS req_name
               FROM expenses e
               JOIN users ru ON ru.id = e.requested_by
               JOIN members rm ON rm.id = ru.member_id
              WHERE e.status = 'Pending'
              ORDER BY e.created_at DESC""",
        ).fetchall()

    # Pending new loan applications any admin can approve
    pending_loans = []
    if role in ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE', 'IT_ADMIN'):
        pending_loans = db.execute(
            """SELECT l.*, m.full_name AS borrower_name, m.member_no,
                      (SELECT COUNT(DISTINCT user_id) FROM loan_approvals la
                        WHERE la.loan_id=l.id AND la.stage='NEW' AND la.decision='APPROVE') AS approval_count,
                      EXISTS(SELECT 1 FROM loan_approvals la
                              WHERE la.loan_id=l.id AND la.stage='NEW' AND la.user_id=?) AS already_approved
               FROM loans l
               JOIN members m ON m.id = l.member_id
              WHERE l.status = 'Pending'
              ORDER BY l.created_at DESC""",
            (user_id,),
        ).fetchall()

    # Pending member Next of Kin change requests (Treasurer approves)
    pending_profile_changes = []
    if role in ('TREASURER', 'IT_ADMIN'):
        rows = db.execute(
            """SELECT r.*, m.full_name, m.member_no, m.next_of_kin AS current_next_of_kin,
                      m.nok_relationship AS current_nok_relationship,
                      m.nok_phone AS current_nok_phone,
                      m.nok_address AS current_nok_address,
                      u.username AS requester_username
                 FROM member_profile_change_requests r
                 JOIN members m ON m.id = r.member_id
                 JOIN users u ON u.id = r.requested_by_user_id
                WHERE r.status='Pending' AND r.request_type='NEXT_OF_KIN'
                ORDER BY r.created_at DESC"""
        ).fetchall()
        for row in rows:
            d = dict(row)
            try:
                payload = json.loads(d.get('payload_json') or '{}')
            except (TypeError, ValueError):
                payload = {}
            d['request_next_of_kin'] = (payload.get('next_of_kin') or '').strip()
            d['request_nok_relationship'] = (payload.get('nok_relationship') or '').strip()
            d['request_nok_phone'] = (payload.get('nok_phone') or '').strip()
            d['request_nok_address'] = (payload.get('nok_address') or '').strip()
            pending_profile_changes.append(d)

    pending_registration_requests = []
    if role in ('SECRETARY', 'IT_ADMIN'):
        pending_registration_requests = db.execute(
            """SELECT rr.*, 
                      (SELECT COUNT(DISTINCT user_id) FROM registration_request_approvals ra
                        WHERE ra.request_id=rr.id AND ra.decision='APPROVE') AS approval_count,
                      EXISTS(SELECT 1 FROM registration_request_approvals ra
                              WHERE ra.request_id=rr.id AND ra.user_id=?) AS already_approved
                 FROM registration_requests rr
                WHERE rr.status='Pending'
                ORDER BY rr.created_at DESC""",
            (user_id,),
        ).fetchall()

    approved_loan_history = []
    if role in ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE', 'IT_ADMIN'):
        rows = db.execute(
            """SELECT l.id, l.loan_no, l.status, l.approved_date, l.updated_at,
                      m.full_name AS borrower_name, m.member_no,
                      (SELECT COUNT(DISTINCT user_id) FROM loan_approvals la
                        WHERE la.loan_id=l.id AND la.stage='NEW' AND la.decision='APPROVE') AS approval_count
                 FROM loans l
                 JOIN members m ON m.id = l.member_id
                WHERE l.status != 'Pending'
                  AND EXISTS (SELECT 1 FROM loan_approvals la WHERE la.loan_id=l.id AND la.stage='NEW')
                ORDER BY COALESCE(l.approved_date, l.updated_at) DESC
                LIMIT 10"""
        ).fetchall()
        for row in rows:
            item = dict(row)
            item['approver_names'] = _approval_names(row['id'], 'NEW')
            approved_loan_history.append(item)

    approved_amendment_history = []
    if role in ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE', 'IT_ADMIN'):
        rows = db.execute(
            """SELECT la.id, la.loan_id, la.status, la.approved_at, la.updated_at,
                      l.loan_no, m.full_name AS borrower_name, m.member_no,
                      (SELECT COUNT(DISTINCT user_id) FROM loan_approvals lap
                        WHERE lap.loan_id=la.loan_id AND lap.amendment_id=la.id
                          AND lap.stage='AMENDMENT' AND lap.decision='APPROVE') AS approval_count,
                      la.approvals_required
                 FROM loan_amendments la
                 JOIN loans l ON l.id = la.loan_id
                 JOIN members m ON m.id = l.member_id
                WHERE la.status='Approved'
                ORDER BY COALESCE(la.approved_at, la.updated_at) DESC
                LIMIT 10"""
        ).fetchall()
        for row in rows:
            item = dict(row)
            item['approver_names'] = _approval_names(row['loan_id'], 'AMENDMENT', row['id'])
            approved_amendment_history.append(item)

    approved_expense_history = []
    if role in ('CHAIRMAN', 'IT_ADMIN', 'TREASURER'):
        approved_expense_history = db.execute(
            """SELECT e.id, e.expense_no, e.amount, e.purpose, e.approved_at,
                      COALESCE(am.full_name, au.username) AS approver_name,
                      COALESCE(rm.full_name, ru.username) AS requester_name
                 FROM expenses e
                 LEFT JOIN users au ON au.id = e.approved_by
                 LEFT JOIN members am ON am.id = au.member_id
                 LEFT JOIN users ru ON ru.id = e.requested_by
                 LEFT JOIN members rm ON rm.id = ru.member_id
                WHERE e.status='Approved'
                ORDER BY e.approved_at DESC
                LIMIT 10"""
        ).fetchall()

    approved_profile_history = []
    if role in ('TREASURER', 'IT_ADMIN', 'SECRETARY'):
        approved_profile_history = db.execute(
            """SELECT r.id, r.request_type, r.approved_at, m.full_name, m.member_no,
                      COALESCE(am.full_name, au.username) AS approver_name
                 FROM member_profile_change_requests r
                 JOIN members m ON m.id = r.member_id
                 LEFT JOIN users au ON au.id = r.approved_by_user_id
                 LEFT JOIN members am ON am.id = au.member_id
                WHERE r.status='Approved'
                ORDER BY r.approved_at DESC
                LIMIT 10"""
        ).fetchall()

    approved_registration_history = []
    if role in ('SECRETARY', 'IT_ADMIN'):
        rows = db.execute(
            """SELECT rr.*, COALESCE(m.member_no, '-') AS member_no,
                      COALESCE(m.full_name, rr.full_name) AS enrolled_name
                 FROM registration_requests rr
                 LEFT JOIN members m ON m.id = rr.created_member_id
                WHERE rr.status='Approved'
                ORDER BY rr.approved_at DESC
                LIMIT 10"""
        ).fetchall()
        for row in rows:
            item = dict(row)
            approvals = db.execute(
                """SELECT COALESCE(m.full_name, u.username) AS approver_name
                     FROM registration_request_approvals ra
                     JOIN users u ON u.id = ra.user_id
                     LEFT JOIN members m ON m.id = u.member_id
                    WHERE ra.request_id=? AND ra.decision='APPROVE'
                    ORDER BY ra.created_at ASC""",
                (row['id'],),
            ).fetchall()
            item['approver_names'] = [r['approver_name'] for r in approvals]
            approved_registration_history.append(item)

    # Recent notifications (last 30, including already-read)
    notifications = db.execute(
        """SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 30""",
        (user_id,),
    ).fetchall()

    # Mark unread notifications as read
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (user_id,))
    db.commit()

    total_pending = (
        sum(1 for a in pending_amendments if not a['already_approved']) +
        len(pending_expenses) +
        sum(1 for l in pending_loans if not l['already_approved']) +
        len(pending_profile_changes) +
        sum(1 for r in pending_registration_requests if not r['already_approved'])
    )

    return render_template(
        'admin/approvals.html',
        pending_amendments=pending_amendments,
        pending_expenses=pending_expenses,
        pending_loans=pending_loans,
        pending_profile_changes=pending_profile_changes,
        pending_registration_requests=pending_registration_requests,
        approved_loan_history=approved_loan_history,
        approved_amendment_history=approved_amendment_history,
        approved_expense_history=approved_expense_history,
        approved_profile_history=approved_profile_history,
        approved_registration_history=approved_registration_history,
        notifications=notifications,
        total_pending=total_pending,
    )


@bp.route('/approvals/registration-requests/<int:request_id>/approve', methods=['POST'])
@role_required('SECRETARY', 'IT_ADMIN')
def approve_registration_request(request_id):
    db = get_db()
    reg_request = db.execute(
        "SELECT * FROM registration_requests WHERE id=? AND status='Pending'",
        (request_id,),
    ).fetchone()
    if not reg_request:
        flash('Registration request not found or already processed.', 'warning')
        return redirect(url_for('admin.approvals'))

    already = db.execute(
        "SELECT 1 FROM registration_request_approvals WHERE request_id=? AND user_id=?",
        (request_id, session['user_id']),
    ).fetchone()
    if already:
        flash('You already approved this registration request.', 'info')
        return redirect(url_for('admin.approvals'))

    db.execute(
        """INSERT INTO registration_request_approvals (request_id, user_id, decision)
           VALUES (?, ?, 'APPROVE')""",
        (request_id, session['user_id']),
    )
    approvals = db.execute(
        "SELECT COUNT(DISTINCT user_id) c FROM registration_request_approvals WHERE request_id=? AND decision='APPROVE'",
        (request_id,),
    ).fetchone()['c']

    if approvals >= int(reg_request['approvals_required'] or 2):
        try:
            created = _create_member_and_user_from_registration_request(db, reg_request)
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'danger')
            return redirect(url_for('admin.approvals'))

        db.execute(
            """UPDATE registration_requests
                  SET status='Approved', approved_at=CURRENT_TIMESTAMP,
                      created_member_id=?, created_user_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (created['member_id'], created['user_id'], request_id),
        )
        db.commit()
        log_action(
            'APPROVE_REGISTRATION_REQUEST',
            'member',
            created['member_id'],
            f"Approved registration request #{request_id} and created {created['member_no']}",
        )

        wa_message = _registration_credentials_whatsapp_message(
            created['full_name'],
            created['member_no'],
            created['username'],
            created['temp_password'],
        )
        wa_url = build_whatsapp_link(created['phone'], wa_message)
        if not wa_url:
            flash('Registration approved and account created, but no valid WhatsApp number was available for credentials delivery.', 'warning')
            return redirect(url_for('admin.approvals'))
        flash('Registration approved. WhatsApp credentials message is opening.', 'success')
        return _render_whatsapp_reset_redirect(wa_url, url_for('admin.approvals'))

    remaining = int(reg_request['approvals_required'] or 2) - approvals
    db.commit()
    flash(f'Registration approval recorded. {remaining} more approval(s) required.', 'success')
    return redirect(url_for('admin.approvals'))


@bp.route('/approvals/profile-requests/<int:request_id>/approve', methods=['POST'])
@role_required('TREASURER', 'IT_ADMIN')
def approve_member_profile_change(request_id):
    db = get_db()
    req = db.execute(
        """SELECT * FROM member_profile_change_requests
             WHERE id=? AND status='Pending'""",
        (request_id,),
    ).fetchone()
    if not req:
        flash('Request not found or already processed.', 'warning')
        return redirect(url_for('admin.approvals'))

    if req['request_type'] != 'NEXT_OF_KIN':
        flash('Unsupported profile request type.', 'danger')
        return redirect(url_for('admin.approvals'))

    try:
        payload = json.loads(req['payload_json'] or '{}')
    except (TypeError, ValueError):
        payload = {}

    next_of_kin = (payload.get('next_of_kin') or '').strip()
    nok_relationship = (payload.get('nok_relationship') or '').strip()
    nok_phone = (payload.get('nok_phone') or '').strip()
    nok_address = (payload.get('nok_address') or '').strip()
    if not next_of_kin or not nok_relationship or not nok_phone:
        flash('Submitted request data is incomplete.', 'danger')
        return redirect(url_for('admin.approvals'))

    member = db.execute(
        "SELECT id, full_name, member_no FROM members WHERE id=?",
        (req['member_id'],),
    ).fetchone()
    if not member:
        flash('Member record not found.', 'danger')
        return redirect(url_for('admin.approvals'))

    db.execute(
        """UPDATE members
              SET next_of_kin=?, nok_relationship=?, nok_phone=?, nok_address=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (next_of_kin, nok_relationship, nok_phone, nok_address, req['member_id']),
    )
    db.execute(
        """UPDATE member_profile_change_requests
              SET status='Approved', approved_by_user_id=?, approved_at=CURRENT_TIMESTAMP,
                  updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (session['user_id'], request_id),
    )

    _notify_member(
        db,
        req['member_id'],
        'Next of Kin details approved',
        'Your Next of Kin details were approved and saved successfully.',
        url_for('member.profile'),
    )
    db.commit()
    log_action(
        'APPROVE_MEMBER_PROFILE_CHANGE',
        'member',
        req['member_id'],
        f"Approved Next of Kin update request #{request_id} for {member['member_no']}",
    )
    flash('Request approved and member details updated.', 'success')
    return redirect(url_for('admin.approvals'))


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

    # Activity log: admin actions on this member + this member's own login/password events
    activity_log = []
    if not is_exited_member:
        if user_row:
            activity_log = db.execute(
                """SELECT al.*, COALESCE(m.full_name, u.username, 'System') AS actor_name
                   FROM audit_log al
                   LEFT JOIN users u ON u.id = al.user_id
                   LEFT JOIN members m ON m.id = u.member_id
                  WHERE (al.entity_type = 'member' AND al.entity_id = ?)
                     OR (al.entity_type = 'user' AND al.entity_id = ?)
                  ORDER BY al.created_at DESC LIMIT 15""",
                (member_id, user_row['id']),
            ).fetchall()
        else:
            activity_log = db.execute(
                """SELECT al.*, COALESCE(m.full_name, u.username, 'System') AS actor_name
                   FROM audit_log al
                   LEFT JOIN users u ON u.id = al.user_id
                   LEFT JOIN members m ON m.id = u.member_id
                  WHERE al.entity_type = 'member' AND al.entity_id = ?
                  ORDER BY al.created_at DESC LIMIT 15""",
                (member_id,),
            ).fetchall()

    return render_template(
        'admin/member_detail.html',
        member=member, savings=savings, loans=loans,
        fines=fines, fees=fees, user_row=user_row,
        total_savings=total_savings, locked=locked, available=available,
        is_exited_member=is_exited_member,
        history=history, activity_log=activity_log,
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


@bp.route('/members/<int:member_id>/national-id/approve', methods=['POST'])
@role_required('IT_ADMIN', 'SECRETARY')
def member_approve_national_id_copy(member_id):
    db = get_db()
    member = db.execute(
        "SELECT id, full_name, member_no, nid_copy_url, nid_copy_approved FROM members WHERE id=?",
        (member_id,),
    ).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))
    if not member['nid_copy_url']:
        flash('No National ID copy has been uploaded for this member.', 'warning')
        return redirect(url_for('admin.member_detail', member_id=member_id))
    if int(member['nid_copy_approved'] or 0) == 1:
        flash('National ID copy is already approved.', 'info')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    db.execute(
        "UPDATE members SET nid_copy_approved=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (member_id,),
    )
    _notify_member(
        db,
        member_id,
        'National ID copy approved',
        'Your National ID copy has been approved. Upload changes are now locked.',
        url_for('member.profile'),
    )
    db.commit()
    log_action(
        'APPROVE_MEMBER_NID_COPY',
        'member',
        member_id,
        f"Approved National ID copy for {member['member_no']}",
    )
    flash('National ID copy approved. Member upload is now locked.', 'success')
    return redirect(url_for('admin.member_detail', member_id=member_id))


@bp.route('/members/<int:member_id>/national-id', methods=['POST'])
@role_required('IT_ADMIN', 'SECRETARY')
def member_upload_national_id_copy(member_id):
    db = get_db()
    member = db.execute(
        "SELECT id, full_name, member_no FROM members WHERE id=?",
        (member_id,),
    ).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))

    nid_copy_url = _save_upload('nid_copy', 'nid_copies')
    if not nid_copy_url:
        flash('Upload a valid National ID image (JPG, PNG, GIF, or WEBP).', 'warning')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    db.execute(
        """UPDATE members
              SET nid_copy_url=?, nid_copy_approved=1, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (nid_copy_url, member_id),
    )
    _notify_member(
        db,
        member_id,
        'National ID copy updated',
        'Your National ID copy was updated by administration.',
        url_for('member.profile'),
    )
    db.commit()
    log_action(
        'UPLOAD_MEMBER_NID_COPY',
        'member',
        member_id,
        f"Uploaded National ID copy for {member['member_no']}",
    )
    flash('National ID copy uploaded successfully.', 'success')
    return redirect(url_for('admin.member_detail', member_id=member_id))


@bp.route('/members/<int:member_id>/membership-form', methods=['POST'])
@role_required('IT_ADMIN', 'SECRETARY')
def member_upload_membership_form(member_id):
    db = get_db()
    member = db.execute(
        "SELECT id, full_name, member_no FROM members WHERE id=?",
        (member_id,),
    ).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('admin.members_list'))

    membership_form_url = _save_membership_form('membership_form_file')
    if not membership_form_url:
        flash('Upload a valid registration form file (PDF, JPG, JPEG, PNG, or WEBP).', 'warning')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    db.execute(
        "UPDATE members SET membership_form_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (membership_form_url, member_id),
    )
    _notify_member(
        db,
        member_id,
        'Registration form uploaded',
        'Your signed membership registration form hard copy was uploaded by administration.',
        url_for('member.profile'),
    )
    db.commit()
    log_action(
        'UPLOAD_MEMBER_REGISTRATION_FORM',
        'member',
        member_id,
        f"Uploaded registration form hard copy for {member['member_no']}",
    )
    flash('Membership registration hard copy uploaded successfully.', 'success')
    return redirect(url_for('admin.member_detail', member_id=member_id))


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
@role_required('IT_ADMIN', 'SECRETARY')
def member_reset_password(member_id):
    db = get_db()
    user = db.execute(
        """SELECT u.id, u.username, m.full_name, m.phone
             FROM users u
             JOIN members m ON m.id = u.member_id
            WHERE u.member_id = ?""",
        (member_id,),
    ).fetchone()
    if not user:
        flash('No login account found for this member.', 'danger')
        return redirect(url_for('admin.member_detail', member_id=member_id))

    new_pw = request.form.get('new_password') or 'gazebo123'
    reason = (request.form.get('reset_reason') or 'Administrative account recovery').strip()
    db.execute(
        """UPDATE users SET password_hash=?, must_change_pw=1
           WHERE member_id = ?""",
        (generate_password_hash(new_pw), member_id),
    )
    db.commit()
    log_action('RESET_PASSWORD', 'user', member_id,
               f"Password reset for member {member_id}. Reason: {reason}")
    flash('Password reset complete. A WhatsApp credentials message is opening.', 'success')

    wa_message = _password_reset_whatsapp_message(
        user['full_name'] or user['username'],
        user['username'],
        new_pw,
        reason,
    )
    wa_url = build_whatsapp_link(user['phone'], wa_message)
    if not wa_url:
        wa_url = f"https://wa.me/?text={quote(wa_message)}"
    return _render_whatsapp_reset_redirect(
        wa_url,
        url_for('admin.member_detail', member_id=member_id),
    )


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

    # Loan interest snapshot for savings page interest tiles
    loan_snap = _loan_interest_snapshot(db)

    # Arrears computation for savings overview
    all_paid_rows_sav = db.execute("SELECT member_id, period FROM savings").fetchall()
    paid_map_sav = {}
    for r in all_paid_rows_sav:
        paid_map_sav.setdefault(r['member_id'], set()).add(r['period'])

    sav_arrears_members = 0
    sav_arrears_amount  = 0
    sav_cur_defaulters  = 0
    for m in active_members:
        join_date = m['join_date']
        if isinstance(join_date, str):
            join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
        else:
            join_d = join_date
        jp = period_str(join_d)
        exp = [p for p in periods_to_date if p >= jp]
        paid = paid_map_sav.get(m['id'], set())
        overdue = [p for p in exp if p < cur_period and p not in paid]
        if overdue:
            sav_arrears_members += 1
            sav_arrears_amount  += len(overdue) * Config.MONTHLY_SAVINGS_AMOUNT
        if cur_period in exp and cur_period not in paid:
            sav_cur_defaulters += 1

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
        # Interest tiles
        loan_interest_collected=loan_snap['collected'],
        loan_penalty_collected=loan_snap['penalty_collected'],
        loan_income_total=loan_snap['total_income'],
        loan_interest_projected=loan_snap['projected_interest'],
        loan_penalty_projected=loan_snap['projected_penalty'],
        # Arrears summary
        sav_arrears_members=sav_arrears_members,
        sav_arrears_amount=sav_arrears_amount,
        sav_cur_defaulters=sav_cur_defaulters,
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

    sql = """SELECT l.*, m.member_no, m.full_name AS borrower_name, m.phone,
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
        """SELECT l.*, m.member_no, m.full_name AS borrower_name, m.phone,
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

    overdue_reminders = []
    for l in enriched:
        if not l['is_overdue'] or int(l['total_outstanding'] or 0) <= 0:
            continue
        formal_text = _loan_formal_reminder_text(
            l['loan_no'],
            l['borrower_name'],
            l['total_outstanding'],
            int(l['term_months'] or 0),
            int(l['days_overdue'] or 0),
            int(l['outstanding_penalty'] or 0),
        )
        overdue_reminders.append({
            'id': l['id'],
            'loan_no': l['loan_no'],
            'member_no': l['member_no'],
            'borrower_name': l['borrower_name'],
            'phone': l.get('phone'),
            'amount_owed': l['total_outstanding'],
            'term_months': int(l['term_months'] or 0),
            'days_overdue': int(l['days_overdue'] or 0),
            'penalty': int(l['outstanding_penalty'] or 0),
            'formal_text': formal_text,
            'wa_contact_url': build_whatsapp_link(l.get('phone'), formal_text),
        })

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

    loan_snap = _loan_interest_snapshot(db)

    return render_template(
        'admin/loans_list.html',
        loans=enriched,
        status=status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        pool=pool,
        statuses=Config.LOAN_STATUSES,
        overdue_reminders=overdue_reminders,
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
        loan_interest_collected=loan_snap['collected'],
        loan_penalty_collected=loan_snap['penalty_collected'],
        loan_income_total=loan_snap['total_income'],
        loan_interest_projected=loan_snap['projected_interest'],
        loan_penalty_projected=loan_snap['projected_penalty'],
    )


@bp.route('/loans/reminders/send', methods=['POST'])
@admin_required
def loans_send_reminders():
    db = get_db()
    raw_ids = request.form.getlist('loan_ids')
    loan_ids = []
    for v in raw_ids:
        try:
            loan_ids.append(int(v))
        except (TypeError, ValueError):
            continue

    if not loan_ids:
        flash('No overdue loans were selected for reminders.', 'warning')
        return redirect(url_for('admin.loans_list', sort='overdue', dir='desc'))

    sent = 0
    for loan_id in loan_ids:
        loan = db.execute(
            """SELECT l.*, m.full_name AS borrower_name
                 FROM loans l
                 JOIN members m ON m.id = l.member_id
                WHERE l.id = ?""",
            (loan_id,),
        ).fetchone()
        if not loan:
            continue

        repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan_id,)).fetchall()
        pens = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (loan_id,)).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        if not pos['is_overdue'] or int(pos['total_outstanding'] or 0) <= 0:
            continue

        formal_text = _loan_formal_reminder_text(
            loan['loan_no'],
            loan['borrower_name'],
            pos['total_outstanding'],
            int(loan['term_months'] or 0),
            int(pos['days_overdue'] or 0),
            int(pos['outstanding_penalty'] or 0),
        )
        reminder_message = (
            f"Amount Owed: {fmt_money(pos['total_outstanding'])}\n"
            f"Initial Loan Period: {int(loan['term_months'] or 0)} month(s)\n"
            f"Overdue Duration: {int(pos['days_overdue'] or 0)} day(s)\n"
            f"Penalty Interest: {fmt_money(pos['outstanding_penalty'])}\n\n"
            f"{formal_text}"
        )
        _notify_member(
            db,
            loan['member_id'],
            'Loan Repayment Reminder',
            f"{loan['loan_no']} · {fmt_money(pos['total_outstanding'])} outstanding\n\n{reminder_message}",
            url_for('member.loans'),
        )
        sent += 1

    db.commit()
    if sent:
        log_action('SEND_LOAN_REMINDERS', 'loan', None, f"Sent {sent} overdue loan dashboard reminder(s)")
        flash(f'Sent {sent} overdue loan reminder(s) to member dashboards.', 'success')
    else:
        flash('No overdue reminders were sent. Selected loans may already be settled.', 'warning')
    return redirect(url_for('admin.loans_list', sort='overdue', dir='desc'))


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
            ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
            'Loan amendment approval required',
            f"Amendment request on {loan['loan_no']} needs 2 checker approvals.",
            url_for('admin.approvals'),
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
@role_required('IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE')
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
        _notify_member(
            db,
            loan['member_id'],
            'Loan amendment approved',
            f"Amendment for {loan['loan_no']} was fully approved and applied.",
            url_for('member.loans'),
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
        _notify_member(
            db,
            loan['member_id'],
            'Loan fully approved',
            f"{loan['loan_no']} reached the required approvals and is now Active.",
            url_for('member.loans'),
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
    """Compute the distributable interest pool for a calendar year.

    The pool is CASH-ONLY: all loan income actually received so far
    (interest + paid penalties).
    """
    # Keep penalties current before computing pool metrics.
    active_ids = db.execute("SELECT id FROM loans WHERE status='Active'").fetchall()
    for r in active_ids:
        _auto_apply_penalties(db, r['id'])

    # Collected (cash-realized) values on all loans so far.
    rows = db.execute(
        """SELECT COALESCE(SUM(interest_part),0) i,
                  COALESCE(SUM(penalty_part),0) p
             FROM loan_repayments"""
    ).fetchone()

    interest_collected = int(rows['i'] or 0)
    penalty_collected = int(rows['p'] or 0)
    loan_income_cash = interest_collected + penalty_collected

    # Dividends pool uses realized loan income (interest + penalties).
    total_pool = loan_income_cash

    # Fixed policy bonus to top saver.
    top_saver_award = 50000
    distributable = max(0, total_pool - top_saver_award)

    active_member_count = db.execute(
        "SELECT COUNT(*) c FROM members WHERE status='Active'"
    ).fetchone()['c']
    est_per_member = (distributable // active_member_count) if active_member_count > 0 else 0
    remainder_to_allocate = (distributable % active_member_count) if active_member_count > 0 else 0

    return {
        'year': year,
        # --- actual cash collected (all-time) ---
        'interest_collected': interest_collected,
        'penalty_collected': penalty_collected,
        'loan_income_cash': loan_income_cash,
        # --- distribution basis ---
        'total_pool': total_pool,
        'top_saver_award': top_saver_award,
        'distributable': distributable,
        'active_member_count': active_member_count,
        'est_per_member': est_per_member,
        'remainder_to_allocate': remainder_to_allocate,
    }


def _rank_members_for_dividends(members):
    """Deterministic ranking: highest savings first, then member number."""
    return sorted(
        members,
        key=lambda m: (
            -(m['year_savings'] or 0),
            str(m['member_no'] or ''),
            int(m['id']),
        ),
    )


def _build_dividend_payouts(members, distributable_pool, top_saver_id, top_saver_award):
    """Build payouts that fully reconcile to: distributable + top_saver_award."""
    ranked_members = _rank_members_for_dividends(members)
    count = len(ranked_members)
    if count == 0:
        return [], 0, 0

    base_share = (distributable_pool // count) if distributable_pool > 0 else 0
    remainder = (distributable_pool % count) if distributable_pool > 0 else 0
    remainder_ids = {m['id'] for m in ranked_members[:remainder]}

    payouts = []
    for m in ranked_members:
        share = base_share + (1 if m['id'] in remainder_ids else 0)
        is_top = m['id'] == top_saver_id
        amount = share + (top_saver_award if is_top else 0)
        payouts.append({
            'member_id': m['id'],
            'member_no': m['member_no'],
            'full_name': m['full_name'],
            'year_savings': m['year_savings'] or 0,
            'amount': amount,
            'is_top_saver': is_top,
        })

    return payouts, base_share, remainder


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

    ranked_members = _rank_members_for_dividends(members)
    top_saver = ranked_members[0]
    payouts, per_member, remainder = _build_dividend_payouts(
        ranked_members,
        pool['distributable'],
        top_saver['id'],
        pool['top_saver_award'],
    )
    pool['remainder_to_allocate'] = remainder
    pool['balance_check'] = pool['total_pool'] - sum(p['amount'] for p in payouts)

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
        flash(f'No loan income collected in {year}. Cannot distribute.', 'warning')
        return redirect(url_for('admin.dividends_list'))
    if pool['total_pool'] < pool['top_saver_award']:
        flash('Interest pool is below UGX 50,000 top saver bonus. Collect more interest before distribution.', 'warning')
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

    ranked_members = _rank_members_for_dividends(members)
    top_saver = ranked_members[0]
    payouts, per_member, _remainder = _build_dividend_payouts(
        ranked_members,
        pool['distributable'],
        top_saver['id'],
        pool['top_saver_award'],
    )
    if sum(p['amount'] for p in payouts) != pool['total_pool']:
        flash('Dividend math validation failed. No distribution posted.', 'danger')
        return redirect(url_for('admin.dividends_list'))

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
                        (pool['total_pool'], pool['top_saver_award'], pool['distributable'],
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
            (year, pool['total_pool'], pool['top_saver_award'], pool['distributable'],
             len(members), per_member, top_saver['id'],
             top_saver['year_savings'] or 0,
             distribution_date, run_status, session['user_id']),
        )
        run_id = cur.lastrowid

    for p in payouts:
        db.execute(
            """INSERT INTO dividend_payouts
               (dividend_run_id, member_id, amount, is_top_saver)
               VALUES (?, ?, ?, ?)""",
            (run_id, p['member_id'], p['amount'], 1 if p['is_top_saver'] else 0),
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
        """SELECT f.*, m.member_no, m.full_name, m.phone, m.whatsapp_no
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


@bp.route('/fines/<int:fine_id>/remind', methods=['POST'])
@role_required('CHAIRMAN', 'SECRETARY', 'TREASURER', 'IT_ADMIN')
def fines_remind(fine_id):
    db = get_db()
    fine = db.execute(
        """SELECT f.*, m.full_name, m.phone, m.whatsapp_no
             FROM fines f JOIN members m ON m.id = f.member_id
            WHERE f.id=?""",
        (fine_id,),
    ).fetchone()
    if not fine:
        flash('Fine not found.', 'danger')
        return redirect(url_for('admin.fines_list'))
    if fine['status'] == 'Paid':
        flash('This fine is already paid — no reminder needed.', 'info')
        return redirect(url_for('admin.fines_list'))

    msg = (
        f"Dear {fine['full_name']}, this is a reminder that you have an outstanding fine of "
        f"{fmt_money(fine['amount'])} for: {fine['violation']}. "
        f"Kindly settle this with the Treasurer at your earliest convenience. "
        f"Unresolved fines may affect your standing in the club. — GAZEBO Investment Club"
    )
    _notify_member(db, fine['member_id'], 'Fine Payment Reminder', msg, url_for('member.profile'))
    db.commit()
    log_action('FINE_REMINDER', 'fine', fine_id,
               f"Reminder sent for fine {fine_id} ({fmt_money(fine['amount'])}) to member {fine['member_id']}")
    flash(f"Portal notification sent to {fine['full_name']}.", 'success')

    phone = fine['whatsapp_no'] or fine['phone']
    wa_url = build_whatsapp_link(phone, msg) if phone else None
    if wa_url:
        return render_template(
            'admin/whatsapp_open.html',
            wa_url=wa_url, wa_url_g1=None, wa_url_g2=None,
            return_url=url_for('admin.fines_list'),
        )
    return redirect(url_for('admin.fines_list'))


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
        "SELECT id, member_no, full_name, phone, whatsapp_no FROM members WHERE status='Active' ORDER BY member_no"
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


@bp.route('/fees/remind/<int:member_id>', methods=['POST'])
@role_required('CHAIRMAN', 'SECRETARY', 'TREASURER', 'IT_ADMIN')
def fees_remind(member_id):
    year = int(request.form.get('year') or date.today().year)
    db = get_db()
    member = db.execute(
        "SELECT id, full_name, phone, whatsapp_no FROM members WHERE id=? AND status='Active'",
        (member_id,),
    ).fetchone()
    if not member:
        flash('Member not found or not active.', 'danger')
        return redirect(url_for('admin.fees_list', year=year))

    # Confirm they haven't already paid
    existing = db.execute(
        "SELECT id FROM annual_fees WHERE member_id=? AND year=? AND status='Paid'",
        (member_id, year),
    ).fetchone()
    if existing:
        flash('This member has already paid their fee — no reminder sent.', 'info')
        return redirect(url_for('admin.fees_list', year=year))

    msg = (
        f"Dear {member['full_name']}, this is a friendly reminder that your Annual Registration Fee "
        f"of {fmt_money(Config.ANNUAL_FEE)} for {year} is still outstanding. "
        f"Kindly make payment before 30 June {year} to maintain good standing. "
        f"Please contact the Treasurer if you need assistance. — GAZEBO Investment Club"
    )
    _notify_member(db, member_id, f"Annual Fee Reminder – {year}", msg, url_for('member.profile'))
    db.commit()
    log_action('FEE_REMINDER', 'annual_fee', member_id,
               f"Annual fee {year} reminder sent to member {member_id}")
    flash(f"Portal notification sent to {member['full_name']}.", 'success')

    phone = member['whatsapp_no'] or member['phone']
    wa_url = build_whatsapp_link(phone, msg) if phone else None
    if wa_url:
        return render_template(
            'admin/whatsapp_open.html',
            wa_url=wa_url, wa_url_g1=None, wa_url_g2=None,
            return_url=url_for('admin.fees_list', year=year),
        )
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
        ['IT_ADMIN', 'CHAIRMAN'],
        'Expense approval required',
        f"{expense_no} for {fmt_money(amount)} is waiting your decision.",
        url_for('admin.approvals'),
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
    _notify_user(
        db,
        row['requested_by'],
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

    settings_rows = db.execute(
        "SELECT key, value FROM settings WHERE key IN ('telegram_bot_token','telegram_chat_id')"
    ).fetchall()
    settings_map = {r['key']: (r['value'] or '').strip() for r in settings_rows}
    telegram_bot_token = settings_map.get('telegram_bot_token') or Config.TELEGRAM_BOT_TOKEN or ''
    telegram_chat_id = settings_map.get('telegram_chat_id') or Config.TELEGRAM_CHAT_ID or ''
    
    return render_template(
        'admin/users.html',
        admin_users=admin_users,
        member_users=member_users,
        unlinked_members=unlinked_members,
        all_members=all_members,
        roles=Config.ROLES,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
    )


@bp.route('/users/alerting/save', methods=['POST'])
@role_required('IT_ADMIN')
def users_alerting_save():
    db = get_db()
    bot_token = (request.form.get('telegram_bot_token') or '').strip()
    chat_id = (request.form.get('telegram_chat_id') or '').strip()

    entries = [
        ('telegram_bot_token', bot_token, 'Telegram bot token for system alerts'),
        ('telegram_chat_id', chat_id, 'Telegram destination chat id for alerts'),
    ]
    for key, value, description in entries:
        db.execute(
            """INSERT INTO settings (key, value, description, updated_by, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value,
                 description=excluded.description,
                 updated_by=excluded.updated_by,
                 updated_at=CURRENT_TIMESTAMP""",
            (key, value, description, session.get('user_id')),
        )
    db.commit()
    log_action('ALERT_SETTINGS_SAVE', 'settings', None, 'Updated Telegram alerting credentials')
    flash('Alerting configuration saved.', 'success')
    return redirect(url_for('admin.users_list'))


@bp.route('/users/alerting/test', methods=['POST'])
@role_required('IT_ADMIN')
def users_alerting_test():
    db = get_db()
    bot_token = (request.form.get('telegram_bot_token') or '').strip()
    chat_id = (request.form.get('telegram_chat_id') or '').strip()
    actor = session.get('full_name') or session.get('username') or 'IT Admin'
    text = (
        "GAZEBO GIC Test Alert\n"
        f"Triggered by: {actor}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        "Status: Telegram integration is working."
    )

    ok = send_telegram_test_message(db, text, bot_token=bot_token, chat_id=chat_id)
    if ok:
        log_action('ALERT_TEST', 'settings', None, 'Sent Telegram test alert from System Users')
        flash('Test alert sent successfully.', 'success')
    else:
        flash('Unable to send test alert. Check Telegram bot token and chat ID.', 'danger')
    return redirect(url_for('admin.users_list'))


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
        """SELECT u.*, m.member_no, m.full_name, m.phone FROM users u
           LEFT JOIN members m ON m.id = u.member_id
          WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        reset_reason = (request.form.get('reset_reason') or 'Administrative account recovery').strip()
        is_active = request.form.get('is_active') == 'on'
        must_change = request.form.get('must_change_pw') == 'on'
        
        if new_password:
            if session.get('role') not in {'IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER'}:
                flash('You are not allowed to reset passwords.', 'danger')
                return redirect(url_for('admin.users_edit', user_id=user_id))
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
        if new_password:
            wa_message = _password_reset_whatsapp_message(
                user['full_name'] or user['username'],
                user['username'],
                new_password,
                reset_reason,
            )
            wa_url = build_whatsapp_link(user['phone'], wa_message)
            if not wa_url:
                wa_url = f"https://wa.me/?text={quote(wa_message)}"
            flash('User updated and password reset. A WhatsApp credentials message is opening.', 'success')
            return _render_whatsapp_reset_redirect(wa_url, url_for('admin.users_list'))

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


@bp.route('/users/<int:user_id>')
@role_required('IT_ADMIN')
def users_detail(user_id):
    db = get_db()
    user = db.execute(
        """SELECT u.*, m.member_no, m.full_name, m.phone, m.email,
                  m.role AS member_role, m.status AS member_status
             FROM users u
             LEFT JOIN members m ON m.id = u.member_id
            WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))

    activity = db.execute(
        "SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (user_id,),
    ).fetchall()

    today_str = date.today().isoformat()
    stats = db.execute(
        """SELECT COUNT(*) total_actions,
                  SUM(CASE WHEN date(created_at)=? THEN 1 ELSE 0 END) today_actions,
                  MIN(created_at) first_action,
                  MAX(created_at) last_action
             FROM audit_log WHERE user_id=?""",
        (today_str, user_id),
    ).fetchone()

    return render_template('admin/users_detail.html', user=user, activity=activity, stats=stats)


@bp.route('/users/<int:user_id>/unlock', methods=['POST'])
@role_required('IT_ADMIN')
def users_unlock(user_id):
    """Clear the force-change-password flag without resetting the password."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    db.execute("UPDATE users SET must_change_pw=0 WHERE id=?", (user_id,))
    db.commit()
    log_action('UNLOCK_USER', 'user', user_id,
               f"Cleared force-password-change for {user['username']}")
    flash(f'Force-password-change cleared for {user["username"]}.', 'success')
    return redirect(url_for('admin.users_detail', user_id=user_id))


@bp.route('/users/<int:user_id>/clear-lockout', methods=['POST'])
@role_required('IT_ADMIN')
def users_clear_lockout(user_id):
    """Unlock an account that was locked due to repeated failed login attempts."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users_list'))
    db.execute(
        "UPDATE users SET failed_login_count=0, locked_until=NULL WHERE id=?",
        (user_id,),
    )
    db.commit()
    log_action('CLEAR_ACCOUNT_LOCKOUT', 'user', user_id,
               f"Account lockout cleared by IT Admin for user: {user['username']}")
    flash(f'Account unlocked for {user["username"]}. They can now log in normally.', 'success')
    # Redirect back to the referring page (member detail or users list)
    referrer = request.referrer
    if referrer:
        return redirect(referrer)
    if user['member_id']:
        return redirect(url_for('admin.member_detail', member_id=user['member_id']))
    return redirect(url_for('admin.users_list'))


# ===========================================================================
# AUDIT LOG
# ===========================================================================
@bp.route('/audit')
@admin_required
def audit_list():
    db = get_db()
    f_action  = (request.args.get('action')  or '').strip()
    f_user    = (request.args.get('user')    or '').strip()
    f_entity  = (request.args.get('entity')  or '').strip()
    f_date    = (request.args.get('date')    or '').strip()
    page      = max(1, int(request.args.get('page', 1) or 1))
    per_page  = 100
    offset    = (page - 1) * per_page

    where, params = [], []
    if f_action:
        where.append("al.action LIKE ?")
        params.append(f'%{f_action}%')
    if f_user:
        where.append("(u.username LIKE ? OR m.full_name LIKE ?)")
        params.extend([f'%{f_user}%', f'%{f_user}%'])
    if f_entity:
        where.append("al.entity_type = ?")
        params.append(f_entity)
    if f_date:
        where.append("date(al.created_at) = ?")
        params.append(f_date)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    total = db.execute(
        f"""SELECT COUNT(*) c FROM audit_log al
             LEFT JOIN users u ON u.id = al.user_id
             LEFT JOIN members m ON m.id = u.member_id
             {where_sql}""",
        params,
    ).fetchone()['c']

    rows = db.execute(
        f"""SELECT al.*, u.username, m.full_name
             FROM audit_log al
             LEFT JOIN users u ON u.id = al.user_id
             LEFT JOIN members m ON m.id = u.member_id
             {where_sql}
            ORDER BY al.id DESC LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    today_str = date.today().isoformat()
    stats = db.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN date(created_at)=? THEN 1 ELSE 0 END) today_count,
                  COUNT(DISTINCT user_id) unique_users
             FROM audit_log""",
        (today_str,),
    ).fetchone()

    action_types = [r['action'] for r in db.execute(
        "SELECT DISTINCT action FROM audit_log ORDER BY action"
    ).fetchall()]
    entity_types = [r['entity_type'] for r in db.execute(
        "SELECT DISTINCT entity_type FROM audit_log WHERE entity_type IS NOT NULL ORDER BY entity_type"
    ).fetchall()]

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        'admin/audit_list.html',
        rows=rows, stats=stats,
        action_types=action_types, entity_types=entity_types,
        f_action=f_action, f_user=f_user, f_entity=f_entity, f_date=f_date,
        page=page, total_pages=total_pages, total=total, per_page=per_page,
    )


@bp.route('/audit/<int:log_id>')
@admin_required
def audit_detail(log_id):
    db = get_db()
    row = db.execute(
        """SELECT al.*, u.username, m.full_name, m.member_no, m.role AS member_role
             FROM audit_log al
             LEFT JOIN users u ON u.id = al.user_id
             LEFT JOIN members m ON m.id = u.member_id
            WHERE al.id = ?""",
        (log_id,),
    ).fetchone()
    if not row:
        flash('Log entry not found.', 'danger')
        return redirect(url_for('admin.audit_list'))

    et  = (row['entity_type'] or '').lower()
    eid = row['entity_id']
    entity_data, entity_label = None, None
    if eid:
        if et == 'member':
            entity_data  = db.execute("SELECT * FROM members WHERE id=?", (eid,)).fetchone()
            entity_label = 'Member'
        elif et == 'user':
            entity_data  = db.execute(
                "SELECT u.*, m.full_name, m.member_no FROM users u LEFT JOIN members m ON m.id=u.member_id WHERE u.id=?",
                (eid,)).fetchone()
            entity_label = 'System User'
        elif et == 'loan':
            entity_data  = db.execute(
                "SELECT l.*, m.full_name, m.member_no FROM loans l LEFT JOIN members m ON m.id=l.member_id WHERE l.id=?",
                (eid,)).fetchone()
            entity_label = 'Loan'
        elif et == 'savings':
            entity_data  = db.execute(
                "SELECT s.*, m.full_name, m.member_no FROM savings s LEFT JOIN members m ON m.id=s.member_id WHERE s.id=?",
                (eid,)).fetchone()
            entity_label = 'Savings Record'
        elif et == 'dividend_run':
            entity_data  = db.execute("SELECT * FROM dividend_runs WHERE id=?", (eid,)).fetchone()
            entity_label = 'Dividend Run'
        elif et == 'expense':
            entity_data  = db.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
            entity_label = 'Expense'
        elif et == 'fine':
            entity_data  = db.execute(
                "SELECT f.*, m.full_name, m.member_no FROM fines f LEFT JOIN members m ON m.id=f.member_id WHERE f.id=?",
                (eid,)).fetchone()
            entity_label = 'Fine'
        elif et == 'annual_fee':
            entity_data  = db.execute(
                "SELECT af.*, m.full_name, m.member_no FROM annual_fees af LEFT JOIN members m ON m.id=af.member_id WHERE af.id=?",
                (eid,)).fetchone()
            entity_label = 'Annual Fee'
        elif et == 'minutes':
            entity_data  = db.execute("SELECT * FROM minutes WHERE id=?", (eid,)).fetchone()
            entity_label = 'Meeting Minutes'

    prev_row = db.execute("SELECT id FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT 1", (log_id,)).fetchone()
    next_row = db.execute("SELECT id FROM audit_log WHERE id > ? ORDER BY id ASC  LIMIT 1", (log_id,)).fetchone()

    related = []
    if et and eid:
        related = db.execute(
            """SELECT al.*, u.username FROM audit_log al
               LEFT JOIN users u ON u.id = al.user_id
               WHERE al.entity_type=? AND al.entity_id=? AND al.id!=?
               ORDER BY al.id DESC LIMIT 15""",
            (row['entity_type'], eid, log_id),
        ).fetchall()

    return render_template(
        'admin/audit_detail.html',
        row=row, entity_data=entity_data, entity_label=entity_label,
        related=related,
        prev_id=prev_row['id'] if prev_row else None,
        next_id=next_row['id'] if next_row else None,
    )


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
    cur_period = period_str(today)
    periods_to_date = all_savings_periods(today)
    periods_12 = periods_to_date[-12:] if len(periods_to_date) >= 12 else periods_to_date

    # Core members snapshot
    active_members_rows = db.execute(
        """SELECT id, member_no, full_name, phone, join_date
             FROM members
            WHERE status='Active'
            ORDER BY member_no"""
    ).fetchall()
    total_members = len(active_members_rows)

    all_paid_rows = db.execute("SELECT member_id, period FROM savings").fetchall()
    paid_map = {}
    for r in all_paid_rows:
        paid_map.setdefault(r['member_id'], set()).add(r['period'])

    # Savings arrears / defaulters (current snapshot)
    savings_defaulters = []
    arrears_members_count = 0
    arrears_amount_total = 0
    current_month_unpaid_count = 0
    for m in active_members_rows:
        join_date = m['join_date']
        if isinstance(join_date, str):
            join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
        else:
            join_d = join_date
        join_period = period_str(join_d)
        expected_periods = [p for p in periods_to_date if p >= join_period]
        member_paid = paid_map.get(m['id'], set())

        overdue_missing = [p for p in expected_periods if p < cur_period and p not in member_paid]
        current_unpaid = cur_period in expected_periods and cur_period not in member_paid
        arrears_amount = len(overdue_missing) * Config.MONTHLY_SAVINGS_AMOUNT

        if overdue_missing:
            arrears_members_count += 1
            arrears_amount_total += arrears_amount
        if current_unpaid:
            current_month_unpaid_count += 1

        if overdue_missing or current_unpaid:
            savings_defaulters.append({
                'member_no': m['member_no'],
                'full_name': m['full_name'],
                'phone': m['phone'],
                'arrears_count': len(overdue_missing),
                'arrears_amount': arrears_amount,
                'current_unpaid': current_unpaid,
            })

    savings_defaulters.sort(
        key=lambda x: (
            0 if x['current_unpaid'] else 1,
            -x['arrears_amount'],
            -x['arrears_count'],
            x['member_no'],
        )
    )
    top_savings_defaulters = savings_defaulters[:10]

    # Financial balance snapshot
    total_savings = db.execute(
        """SELECT COALESCE(SUM(s.amount),0) s
             FROM savings s
             JOIN members m ON m.id = s.member_id
            WHERE m.status='Active'"""
    ).fetchone()['s']

    active_loans = db.execute(
        """SELECT l.*, m.member_no, m.full_name AS borrower_name, m.phone
             FROM loans l
             LEFT JOIN members m ON m.id = l.member_id
            WHERE l.status='Active'"""
    ).fetchall()

    out_principal = 0
    out_interest = 0
    out_penalty = 0
    overdue_loans_total_owed = 0
    overdue_loans_count = 0
    loan_defaulters = []
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
        if pos['is_overdue'] and int(pos['total_outstanding'] or 0) > 0:
            overdue_loans_count += 1
            overdue_loans_total_owed += int(pos['total_outstanding'] or 0)
            loan_defaulters.append({
                'loan_no': loan['loan_no'],
                'member_no': loan['member_no'],
                'borrower_name': loan['borrower_name'],
                'days_overdue': int(pos['days_overdue'] or 0),
                'months_overdue': int(pos['overdue_months'] or 0),
                'outstanding': int(pos['total_outstanding'] or 0),
                'penalty': int(pos['outstanding_penalty'] or 0),
            })

    loan_defaulters.sort(key=lambda x: (-x['outstanding'], -x['days_overdue'], x['loan_no']))
    top_loan_defaulters = loan_defaulters[:10]

    loan_totals = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(principal),0) s FROM loans"
    ).fetchone()
    loan_total_count = int(loan_totals['c'] or 0)
    loan_book_total = int(loan_totals['s'] or 0)

    pending_loans_count = db.execute(
        "SELECT COUNT(*) c FROM loans WHERE status='Pending'"
    ).fetchone()['c']

    fines_receivable = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM fines WHERE status='Unpaid'"
    ).fetchone()['s']
    fees_receivable = db.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM annual_fees
           WHERE status='Unpaid'"""
    ).fetchone()['s']
    # Income and operating performance
    income_fees = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM annual_fees WHERE status='Paid'"
    ).fetchone()['s']
    income_fines = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM fines WHERE status='Paid'"
    ).fetchone()['s']
    _int_row = db.execute(
        "SELECT COALESCE(SUM(interest_part),0) i, COALESCE(SUM(penalty_part),0) p FROM loan_repayments"
    ).fetchone()
    loan_interest_collected = int(_int_row['i'] or 0)
    loan_penalty_collected = int(_int_row['p'] or 0)
    loan_income_cash = loan_interest_collected + loan_penalty_collected

    ops = _operational_fund_snapshot(db)
    expenses_spent = ops['approved_expenses']
    expenses_pending = ops['pending_expenses']
    expenses_count = db.execute(
        "SELECT COUNT(*) c FROM expenses WHERE status='Approved'"
    ).fetchone()['c']

    # Governance / control signals
    minutes_stats = db.execute(
        """SELECT
                 COUNT(*) total,
                 COALESCE(SUM(CASE WHEN is_published=1 THEN 1 ELSE 0 END),0) published
              FROM minutes"""
    ).fetchone()
    audit_30d = db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE created_at >= datetime('now','-30 day')"
    ).fetchone()['c']
    pending_expenses_count = db.execute(
        "SELECT COUNT(*) c FROM expenses WHERE status='Pending'"
    ).fetchone()['c']

    # Cash estimate mirrors dashboard logic
    loans_all_disbursed_amount = db.execute(
        "SELECT COALESCE(SUM(principal),0) s FROM loans WHERE disbursed_date IS NOT NULL"
    ).fetchone()['s']
    total_repayments_in = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM loan_repayments"
    ).fetchone()['s']

    cash_estimate = (
        int(total_savings)
        + int(income_fees or 0)
        + int(income_fines or 0)
        + int(total_repayments_in or 0)
        - int(loans_all_disbursed_amount or 0)
        - int(expenses_spent or 0)
    )

    cash_available = total_savings - out_principal
    total_assets = (
        cash_available + out_principal + out_interest + out_penalty +
        fines_receivable + fees_receivable
    )
    operating_income = int(income_fees or 0) + int(income_fines or 0) - int(expenses_spent or 0)

    # Membership stats
    active_members = total_members
    exited_members = db.execute(
        "SELECT COUNT(*) c FROM members WHERE status='Exited'"
    ).fetchone()['c']

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

    # Monthly trend pack for AGM reporting (12 months)
    monthly_rows = []
    for p in periods_12:
        savings_row = db.execute(
            """SELECT COALESCE(SUM(s.amount),0) s
                 FROM savings s
                 JOIN members m ON m.id = s.member_id
                WHERE s.period=? AND m.status='Active'""",
            (p,),
        ).fetchone()

        repay_row = db.execute(
            """SELECT COALESCE(SUM(amount),0) total,
                      COALESCE(SUM(interest_part),0) i,
                      COALESCE(SUM(penalty_part),0) p
                 FROM loan_repayments
                WHERE strftime('%Y-%m', payment_date)=?""",
            (p,),
        ).fetchone()

        fees_row = db.execute(
            """SELECT COALESCE(SUM(amount),0) s
                 FROM annual_fees
                WHERE status='Paid' AND paid_date IS NOT NULL
                  AND strftime('%Y-%m', paid_date)=?""",
            (p,),
        ).fetchone()

        fines_row = db.execute(
            """SELECT COALESCE(SUM(amount),0) s
                 FROM fines
                WHERE status='Paid' AND strftime('%Y-%m', fine_date)=?""",
            (p,),
        ).fetchone()

        expenses_row = db.execute(
            """SELECT COALESCE(SUM(amount),0) s
                 FROM expenses
                WHERE status='Approved' AND strftime('%Y-%m', expense_date)=?""",
            (p,),
        ).fetchone()

        disbursed_row = db.execute(
            """SELECT COALESCE(SUM(principal),0) s
                 FROM loans
                WHERE disbursed_date IS NOT NULL
                  AND strftime('%Y-%m', disbursed_date)=?""",
            (p,),
        ).fetchone()

        savings_m = int(savings_row['s'] or 0)
        repay_total_m = int(repay_row['total'] or 0)
        interest_m = int(repay_row['i'] or 0)
        penalty_m = int(repay_row['p'] or 0)
        fees_m = int(fees_row['s'] or 0)
        fines_m = int(fines_row['s'] or 0)
        expenses_m = int(expenses_row['s'] or 0)
        disbursed_m = int(disbursed_row['s'] or 0)

        inflow_m = savings_m + repay_total_m + fees_m + fines_m
        outflow_m = expenses_m + disbursed_m
        net_m = inflow_m - outflow_m

        savings_default_count = 0
        for m in active_members_rows:
            join_date = m['join_date']
            if isinstance(join_date, str):
                join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
            else:
                join_d = join_date
            if period_str(join_d) > p:
                continue
            if p not in paid_map.get(m['id'], set()):
                savings_default_count += 1

        monthly_rows.append({
            'period': p,
            'label': period_label(p),
            'savings': savings_m,
            'loan_repayments': repay_total_m,
            'interest': interest_m,
            'penalty': penalty_m,
            'fees': fees_m,
            'fines': fines_m,
            'expenses': expenses_m,
            'disbursed': disbursed_m,
            'inflow': inflow_m,
            'outflow': outflow_m,
            'net': net_m,
            'savings_defaulters': savings_default_count,
        })

    max_inflow = max([r['inflow'] for r in monthly_rows] + [1])
    max_outflow = max([r['outflow'] for r in monthly_rows] + [1])
    max_net_abs = max([abs(r['net']) for r in monthly_rows] + [1])
    max_sav_defaulters = max([r['savings_defaulters'] for r in monthly_rows] + [1])

    for r in monthly_rows:
        r['inflow_pct'] = round((r['inflow'] / max_inflow) * 100, 1)
        r['outflow_pct'] = round((r['outflow'] / max_outflow) * 100, 1)
        r['net_pct'] = round((abs(r['net']) / max_net_abs) * 100, 1)
        r['net_positive'] = r['net'] >= 0
        r['savings_defaulters_pct'] = round((r['savings_defaulters'] / max_sav_defaulters) * 100, 1)

    ytd_rows = [r for r in monthly_rows if r['period'].startswith(str(current_year))]
    ytd_inflow = sum(r['inflow'] for r in ytd_rows)
    ytd_outflow = sum(r['outflow'] for r in ytd_rows)
    ytd_net = ytd_inflow - ytd_outflow
    ytd_interest = sum(r['interest'] for r in ytd_rows)
    ytd_penalties = sum(r['penalty'] for r in ytd_rows)

    return render_template(
        'admin/reports.html',
        current_year=current_year,
        current_period=period_label(cur_period),
        periods_12=periods_12,
        monthly_rows=monthly_rows,
        ytd_inflow=ytd_inflow,
        ytd_outflow=ytd_outflow,
        ytd_net=ytd_net,
        ytd_interest=ytd_interest,
        ytd_penalties=ytd_penalties,
        total_savings=total_savings,
        total_assets=total_assets,
        out_principal=out_principal,
        out_interest=out_interest,
        out_penalty=out_penalty,
        fines_receivable=fines_receivable,
        fees_receivable=fees_receivable,
        income_fees=income_fees,
        income_fines=income_fines,
        loan_interest_collected=loan_interest_collected,
        loan_penalty_collected=loan_penalty_collected,
        loan_income_cash=loan_income_cash,
        expenses_spent=expenses_spent,
        expenses_pending=expenses_pending,
        expenses_count=expenses_count,
        operating_income=operating_income,
        cash_estimate=cash_estimate,
        cash_available=cash_available,
        loan_total_count=loan_total_count,
        loan_book_total=loan_book_total,
        pending_loans_count=pending_loans_count,
        overdue_loans_count=overdue_loans_count,
        overdue_loans_total_owed=overdue_loans_total_owed,
        top_loan_defaulters=top_loan_defaulters,
        top_savings_defaulters=top_savings_defaulters,
        arrears_members_count=arrears_members_count,
        arrears_amount_total=arrears_amount_total,
        current_month_unpaid_count=current_month_unpaid_count,
        active_members=active_members,
        exited_members=exited_members,
        minutes_total=minutes_stats['total'],
        minutes_published=minutes_stats['published'],
        audit_30d=audit_30d,
        pending_expenses_count=pending_expenses_count,
        fees_paid_ytd=fees_year['paid_amount'] or 0,
        fees_unpaid_ytd=fees_year['unpaid_amount'] or 0,
        fines_paid_ytd=fines_year['paid_amount'] or 0,
        fines_unpaid_ytd=fines_year['unpaid_amount'] or 0,
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


# ===========================================================================
# MEMBER OFFBOARDING MODULE
# ===========================================================================

_OFFBOARD_ROLES = ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'IT_ADMIN')
_OFFBOARD_WRITE_ROLES = ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'IT_ADMIN')


# _deduct_member_savings is imported from utils as deduct_member_savings
# and aliased below for local use inside this module.
_deduct_member_savings = deduct_member_savings


def _offboarding_loan_check(db, member_id):
    """Return active/pending loans for member, or empty list."""
    return db.execute(
        """SELECT l.*, m.full_name
             FROM loans l
             JOIN members m ON m.id = l.member_id
            WHERE l.member_id=? AND l.status IN ('Active','Pending')""",
        (member_id,),
    ).fetchall()


@bp.route('/offboarding')
@role_required(*_OFFBOARD_ROLES)
def offboarding_list():
    db = get_db()
    role = session.get('role')

    exited_members = db.execute(
        """SELECT m.*, ob.exit_date, ob.savings_at_exit, ob.penalty_amount,
                  ob.net_refund, ob.reason, ob.payment_medium,
                  u_proc.username AS processed_by_name
             FROM members m
             LEFT JOIN member_offboardings ob ON ob.member_id = m.id
             LEFT JOIN users u_proc ON u_proc.id = ob.processed_by
            WHERE m.status='Exited'
            ORDER BY ob.exit_date DESC, m.member_no"""
    ).fetchall()

    active_members = db.execute(
        "SELECT id, member_no, full_name, phone, join_date, status FROM members WHERE status='Active' ORDER BY member_no"
    ).fetchall()

    exited_count = len(exited_members)
    total_penalties = db.execute(
        "SELECT COALESCE(SUM(penalty_amount),0) s FROM member_offboardings"
    ).fetchone()['s']
    total_refunded = db.execute(
        "SELECT COALESCE(SUM(net_refund),0) s FROM member_offboardings"
    ).fetchone()['s']
    total_savings_exited = db.execute(
        "SELECT COALESCE(SUM(savings_at_exit),0) s FROM member_offboardings"
    ).fetchone()['s']

    return render_template(
        'admin/offboarding.html',
        exited_members=exited_members,
        active_members=active_members,
        exited_count=exited_count,
        total_penalties=total_penalties,
        total_refunded=total_refunded,
        total_savings_exited=total_savings_exited,
        can_write=(role in _OFFBOARD_WRITE_ROLES),
        role=role,
    )


@bp.route('/offboarding/preview/<int:member_id>')
@role_required(*_OFFBOARD_WRITE_ROLES)
def offboarding_preview(member_id):
    db = get_db()
    member = db.execute(
        "SELECT * FROM members WHERE id=? AND status='Active'",
        (member_id,),
    ).fetchone()
    if not member:
        flash('Member not found or is not currently active.', 'danger')
        return redirect(url_for('admin.offboarding_list'))

    total_savings = get_member_total_savings(db, member_id)
    penalty = int(total_savings * 0.02)
    net_refund = total_savings - penalty

    active_loans = _offboarding_loan_check(db, member_id)
    loan_outstanding = 0
    loan_positions = []
    for loan in active_loans:
        repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)).fetchall()
        pens = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        loan_outstanding += pos['total_outstanding']
        loan_positions.append({'loan': loan, 'pos': pos})

    savings_after_loan = max(0, total_savings - loan_outstanding)
    penalty_after_loan = int(savings_after_loan * 0.02)
    net_after_loan = savings_after_loan - penalty_after_loan

    return render_template(
        'admin/offboarding.html',
        preview_member=member,
        total_savings=total_savings,
        penalty=penalty,
        net_refund=net_refund,
        active_loans=active_loans,
        loan_outstanding=loan_outstanding,
        loan_positions=loan_positions,
        savings_after_loan=savings_after_loan,
        penalty_after_loan=penalty_after_loan,
        net_after_loan=net_after_loan,
        exited_members=[],
        active_members=[],
        exited_count=0,
        total_penalties=0,
        total_refunded=0,
        total_savings_exited=0,
        can_write=True,
        role=session.get('role'),
    )


@bp.route('/offboarding/execute/<int:member_id>', methods=['POST'])
@role_required(*_OFFBOARD_WRITE_ROLES)
def offboarding_execute(member_id):
    db = get_db()
    member = db.execute(
        "SELECT * FROM members WHERE id=? AND status='Active'",
        (member_id,),
    ).fetchone()
    if not member:
        flash('Member not found or is not currently active.', 'danger')
        return redirect(url_for('admin.offboarding_list'))

    reason = (request.form.get('reason') or '').strip()
    payment_medium = (request.form.get('payment_medium') or '').strip()
    payment_ref = (request.form.get('payment_ref') or '').strip()
    notes = (request.form.get('notes') or '').strip()

    if not reason:
        flash('A reason for offboarding is required.', 'danger')
        return redirect(url_for('admin.offboarding_preview', member_id=member_id))

    # Block if there are active loans — must force-recover first
    active_loans = _offboarding_loan_check(db, member_id)
    if active_loans:
        loan_nos = ', '.join(l['loan_no'] for l in active_loans)
        flash(
            f'Cannot offboard {member["full_name"]}: they have outstanding loan(s) [{loan_nos}]. '
            'Use Forced Loan Recovery to clear the loan(s) first, then retry offboarding.',
            'danger',
        )
        return redirect(url_for('admin.offboarding_preview', member_id=member_id))

    total_savings = get_member_total_savings(db, member_id)
    penalty_amount = int(total_savings * 0.02)
    net_refund = total_savings - penalty_amount

    today_str = date.today().isoformat()

    # Record the offboarding
    db.execute(
        """INSERT INTO member_offboardings
           (member_id, reason, exit_date, savings_at_exit, outstanding_loan_cleared,
            penalty_amount, net_refund, payment_medium, payment_ref, processed_by, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (member_id, reason, today_str, total_savings, 0,
         penalty_amount, net_refund, payment_medium, payment_ref,
         session['user_id'], notes),
    )

    # Record exit penalty as club income (2%)
    if penalty_amount > 0:
        inc_no = _next_doc_no(db, 'operational_incomes', 'income_no', 'GIC-INC')
        db.execute(
            """INSERT INTO operational_incomes
               (income_no, income_date, amount, source, notes, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (inc_no, today_str, penalty_amount, 'Exit Penalty',
             f"2% exit penalty from offboarding of {member['full_name']} ({member['member_no']})",
             session['user_id']),
        )

    # Change member status to Exited
    old_no = member['member_no']
    archived_no = _next_archived_member_no(db, old_no)
    db.execute(
        """UPDATE members
              SET status='Exited', member_no=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (archived_no, member_id),
    )

    # Disable all user logins for this member
    db.execute(
        "UPDATE users SET is_active=0 WHERE member_id=?",
        (member_id,),
    )

    db.commit()

    # Notify all committee members
    _notify_roles(
        db,
        ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
        'Member Offboarded',
        (f"{member['full_name']} ({old_no}) has been offboarded. "
         f"Savings refunded: {fmt_money(net_refund)} (after {fmt_money(penalty_amount)} exit penalty). "
         f"Reason: {reason}"),
        url_for('admin.offboarding_list'),
        exclude_user_id=session['user_id'],
    )
    db.commit()

    log_action(
        'MEMBER_OFFBOARDED', 'member', member_id,
        f"Offboarded {old_no} → {archived_no}. Savings: {fmt_money(total_savings)}, "
        f"Penalty: {fmt_money(penalty_amount)}, Refund: {fmt_money(net_refund)}. Reason: {reason}",
    )

    flash(
        f'{member["full_name"]} ({old_no}) has been successfully offboarded. '
        f'Net refund: {fmt_money(net_refund)} (savings {fmt_money(total_savings)} '
        f'minus 2% exit penalty {fmt_money(penalty_amount)}). '
        f'Login access has been disabled.',
        'success',
    )
    return redirect(url_for('admin.offboarding_list'))


# ===========================================================================
# FORCED LOAN RECOVERY
# ===========================================================================

@bp.route('/loans/<int:loan_id>/force-recover', methods=['GET', 'POST'])
@role_required('TREASURER', 'IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'COMMITTEE')
def force_recover(loan_id):
    db = get_db()
    role = session.get('role')
    loan = db.execute(
        """SELECT l.*, m.full_name AS borrower_name, m.member_no AS borrower_no,
                  m.phone AS borrower_phone, m.whatsapp_no AS borrower_wa,
                  g1.full_name AS g1_name, g1.member_no AS g1_no,
                  g1.phone AS g1_phone, g1.whatsapp_no AS g1_wa, g1.id AS g1_id,
                  g2.full_name AS g2_name, g2.member_no AS g2_no,
                  g2.phone AS g2_phone, g2.whatsapp_no AS g2_wa, g2.id AS g2_id
             FROM loans l
             JOIN members m ON m.id = l.member_id
             LEFT JOIN members g1 ON g1.id = l.guarantor1_id
             LEFT JOIN members g2 ON g2.id = l.guarantor2_id
            WHERE l.id=? AND l.status IN ('Active','Defaulted')""",
        (loan_id,),
    ).fetchone()
    if not loan:
        flash('Loan not found or not eligible for forced recovery.', 'danger')
        return redirect(url_for('admin.loans_list'))

    repays = db.execute("SELECT * FROM loan_repayments WHERE loan_id=?", (loan_id,)).fetchall()
    penalties = db.execute("SELECT * FROM loan_penalties WHERE loan_id=?", (loan_id,)).fetchall()
    pos = calculate_loan_position(loan, repays, penalties)
    total_owed = int(pos['total_outstanding'])

    # Savings balances
    borrower_savings = get_member_total_savings(db, loan['member_id'])
    g1_savings = get_member_total_savings(db, loan['g1_id']) if loan['g1_id'] else 0
    g2_savings = get_member_total_savings(db, loan['g2_id']) if loan['g2_id'] else 0

    # Calculate recovery plan
    from_borrower = min(borrower_savings, total_owed)
    shortfall_after_borrower = total_owed - from_borrower

    # Split remainder between guarantors proportionally (by their available savings)
    g_total_savings = g1_savings + g2_savings
    if shortfall_after_borrower > 0 and g_total_savings > 0:
        if loan['g1_id'] and loan['g2_id']:
            if g_total_savings > 0:
                from_g1_raw = int(shortfall_after_borrower * g1_savings / g_total_savings)
                from_g2_raw = shortfall_after_borrower - from_g1_raw
            else:
                from_g1_raw = from_g2_raw = shortfall_after_borrower // 2
            from_g1 = min(from_g1_raw, g1_savings)
            from_g2 = min(from_g2_raw, g2_savings)
        elif loan['g1_id']:
            from_g1 = min(shortfall_after_borrower, g1_savings)
            from_g2 = 0
        else:
            from_g1 = 0
            from_g2 = min(shortfall_after_borrower, g2_savings)
    else:
        from_g1 = from_g2 = 0

    total_recoverable = from_borrower + from_g1 + from_g2
    final_shortfall = total_owed - total_recoverable
    needs_guarantors = (from_g1 > 0 or from_g2 > 0)

    # Existing recovery record (if any)
    recovery = db.execute(
        "SELECT * FROM forced_loan_recoveries WHERE loan_id=?", (loan_id,)
    ).fetchone()

    # Existing consents
    g1_consent = None
    g2_consent = None
    if loan['g1_id']:
        g1_consent = db.execute(
            "SELECT * FROM guarantor_consents WHERE loan_id=? AND guarantor_id=?",
            (loan_id, loan['g1_id']),
        ).fetchone()
    if loan['g2_id']:
        g2_consent = db.execute(
            "SELECT * FROM guarantor_consents WHERE loan_id=? AND guarantor_id=?",
            (loan_id, loan['g2_id']),
        ).fetchone()

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()

        # --- ACTION: Request guarantor consent ---
        if action == 'request_consent':
            if role not in ('TREASURER', 'IT_ADMIN', 'CHAIRMAN'):
                flash('Only the Treasurer or Chairman can initiate forced recovery.', 'danger')
                return redirect(url_for('admin.force_recover', loan_id=loan_id))

            # Create recovery record (PENDING_CONSENT)
            if not recovery:
                db.execute(
                    """INSERT INTO forced_loan_recoveries
                       (loan_id, triggered_by, recovery_from_borrower,
                        recovery_from_g1, recovery_from_g2, total_recovered,
                        shortfall, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING_CONSENT')""",
                    (loan_id, session['user_id'], from_borrower, from_g1, from_g2,
                     total_recoverable, final_shortfall),
                )
            else:
                db.execute(
                    """UPDATE forced_loan_recoveries
                          SET triggered_by=?, recovery_from_borrower=?, recovery_from_g1=?,
                              recovery_from_g2=?, total_recovered=?, shortfall=?,
                              status='PENDING_CONSENT'
                        WHERE loan_id=?""",
                    (session['user_id'], from_borrower, from_g1, from_g2,
                     total_recoverable, final_shortfall, loan_id),
                )

            # Create/reset consent records for guarantors
            consent_link = url_for('member.guarantor_consent', loan_id=loan_id, _external=True)
            if loan['g1_id'] and from_g1 > 0:
                db.execute(
                    """INSERT OR REPLACE INTO guarantor_consents
                       (loan_id, guarantor_id, amount_requested, consent_status, dispute_raised)
                       VALUES (?, ?, ?, 'PENDING', 0)""",
                    (loan_id, loan['g1_id'], from_g1),
                )
                _notify_member(
                    db, loan['g1_id'],
                    '⚠️ Forced Loan Recovery — Your Consent Required',
                    (f"**ACTION REQUIRED — LOAN RECOVERY NOTICE**\n\n"
                     f"You guaranteed loan {loan['loan_no']} for {loan['borrower_name']} ({loan['borrower_no']}).\n"
                     f"This loan has defaulted. The committee has resolved to initiate forced recovery.\n\n"
                     f"**Your role:** Guarantor\n"
                     f"**Amount to be deducted from your savings:** {fmt_money(from_g1)}\n\n"
                     f"Please log in and go to My Loans → Guarantor Consent to accept or dispute this deduction.\n"
                     f"Accepting will automatically deduct the amount and clear this obligation.\n"
                     f"Declining will raise a formal dispute for committee resolution."),
                    url_for('member.guarantor_consent', loan_id=loan_id),
                )
                # WhatsApp message for guarantor 1
                wa_msg_g1 = (
                    f"*GAZEBO GIC — FORCED LOAN RECOVERY NOTICE*\n\n"
                    f"Dear {loan['g1_name']},\n\n"
                    f"You are a guarantor on loan *{loan['loan_no']}* for {loan['borrower_name']}. "
                    f"This loan has defaulted and the committee has resolved to recover it.\n\n"
                    f"*Amount to be deducted from your savings:* {fmt_money(from_g1)}\n\n"
                    f"*Action Required:* Please log in to the system and accept or dispute this deduction.\n"
                    f"System: {Config.APP_URL}\n\n"
                    "Your prompt attention is required. Thank you."
                )
                if loan['g1_phone']:
                    db.execute(
                        """INSERT INTO notifications (user_id, title, message, link)
                           SELECT u.id, ?, ?, ?
                             FROM users u WHERE u.member_id=? AND u.is_active=1 LIMIT 1""",
                        ('WA Recovery Notice G1 Sent', f"WhatsApp sent to {loan['g1_name']}: {loan['g1_phone']}",
                         url_for('admin.force_recover', loan_id=loan_id),
                         session['user_id']),
                    )

            if loan['g2_id'] and from_g2 > 0:
                db.execute(
                    """INSERT OR REPLACE INTO guarantor_consents
                       (loan_id, guarantor_id, amount_requested, consent_status, dispute_raised)
                       VALUES (?, ?, ?, 'PENDING', 0)""",
                    (loan_id, loan['g2_id'], from_g2),
                )
                _notify_member(
                    db, loan['g2_id'],
                    '⚠️ Forced Loan Recovery — Your Consent Required',
                    (f"**ACTION REQUIRED — LOAN RECOVERY NOTICE**\n\n"
                     f"You guaranteed loan {loan['loan_no']} for {loan['borrower_name']} ({loan['borrower_no']}).\n"
                     f"This loan has defaulted. The committee has resolved to initiate forced recovery.\n\n"
                     f"**Your role:** Guarantor\n"
                     f"**Amount to be deducted from your savings:** {fmt_money(from_g2)}\n\n"
                     f"Please log in and go to My Loans → Guarantor Consent to accept or dispute this deduction.\n"
                     f"Accepting will automatically deduct the amount and clear this obligation.\n"
                     f"Declining will raise a formal dispute for committee resolution."),
                    url_for('member.guarantor_consent', loan_id=loan_id),
                )

            db.commit()
            log_action(
                'FORCED_RECOVERY_CONSENT_SENT', 'loan', loan_id,
                f"Consent requests sent for forced recovery of {loan['loan_no']}. "
                f"Borrower: {fmt_money(from_borrower)}, G1: {fmt_money(from_g1)}, G2: {fmt_money(from_g2)}",
            )
            # Notify all committee about initiation
            _notify_roles(
                db,
                ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
                'Forced Recovery Initiated',
                f"Forced recovery initiated for loan {loan['loan_no']} ({loan['borrower_name']}). "
                f"Total owed: {fmt_money(total_owed)}. Consent requests sent to guarantors.",
                url_for('admin.force_recover', loan_id=loan_id),
                exclude_user_id=session['user_id'],
            )
            db.commit()
            flash(
                f'Consent requests sent to guarantors. '
                f'The Treasurer can execute full recovery once consents are received.',
                'success',
            )

            # Build WhatsApp links for the treasurer to send manually
            wa_g1_url = None
            wa_g2_url = None
            if loan['g1_id'] and from_g1 > 0 and (loan['g1_phone'] or loan['g1_wa']):
                wa_g1_url = build_whatsapp_link(
                    loan['g1_wa'] or loan['g1_phone'],
                    f"*GAZEBO GIC — FORCED LOAN RECOVERY NOTICE*\n\n"
                    f"Dear {loan['g1_name']},\n\n"
                    f"You guaranteed loan *{loan['loan_no']}* for {loan['borrower_name']}. "
                    f"This loan has defaulted. The committee has resolved to force-recover it.\n\n"
                    f"*Amount to be deducted from your savings:* {fmt_money(from_g1)}\n\n"
                    f"Please log in to {Config.APP_URL} and go to My Loans → Guarantor Consent "
                    f"to accept or dispute this action. Your prompt response is required.\n\nThank you.",
                )
            if loan['g2_id'] and from_g2 > 0 and (loan['g2_phone'] or loan['g2_wa']):
                wa_g2_url = build_whatsapp_link(
                    loan['g2_wa'] or loan['g2_phone'],
                    f"*GAZEBO GIC — FORCED LOAN RECOVERY NOTICE*\n\n"
                    f"Dear {loan['g2_name']},\n\n"
                    f"You guaranteed loan *{loan['loan_no']}* for {loan['borrower_name']}. "
                    f"This loan has defaulted. The committee has resolved to force-recover it.\n\n"
                    f"*Amount to be deducted from your savings:* {fmt_money(from_g2)}\n\n"
                    f"Please log in to {Config.APP_URL} and go to My Loans → Guarantor Consent "
                    f"to accept or dispute this action. Your prompt response is required.\n\nThank you.",
                )
            if wa_g1_url or wa_g2_url:
                return render_template(
                    'admin/whatsapp_open.html',
                    wa_url=wa_g1_url or wa_g2_url,
                    wa_url_g1=wa_g1_url,
                    wa_url_g2=wa_g2_url,
                    return_url=url_for('admin.force_recover', loan_id=loan_id),
                )
            return redirect(url_for('admin.force_recover', loan_id=loan_id))

        # --- ACTION: Execute forced recovery ---
        if action == 'execute':
            if role not in ('TREASURER', 'IT_ADMIN', 'CHAIRMAN'):
                flash('Only the Treasurer or Chairman can execute forced recovery.', 'danger')
                return redirect(url_for('admin.force_recover', loan_id=loan_id))

            # Deduct from borrower
            actual_from_borrower, b_periods = _deduct_member_savings(db, loan['member_id'], from_borrower)

            # Deduct from guarantors (only if they consented or treasurer overrides)
            actual_from_g1 = 0
            actual_from_g2 = 0
            g1_periods = []
            g2_periods = []

            override = request.form.get('override_consent') == '1'

            if from_g1 > 0 and loan['g1_id']:
                g1_cons = db.execute(
                    "SELECT consent_status FROM guarantor_consents WHERE loan_id=? AND guarantor_id=?",
                    (loan_id, loan['g1_id']),
                ).fetchone()
                g1_accepted = g1_cons and g1_cons['consent_status'] == 'ACCEPTED'
                if g1_accepted or override:
                    actual_from_g1, g1_periods = _deduct_member_savings(db, loan['g1_id'], from_g1)
                    if g1_cons:
                        db.execute(
                            "UPDATE guarantor_consents SET consent_status='ACCEPTED', consent_at=CURRENT_TIMESTAMP WHERE loan_id=? AND guarantor_id=?",
                            (loan_id, loan['g1_id']),
                        )

            if from_g2 > 0 and loan['g2_id']:
                g2_cons = db.execute(
                    "SELECT consent_status FROM guarantor_consents WHERE loan_id=? AND guarantor_id=?",
                    (loan_id, loan['g2_id']),
                ).fetchone()
                g2_accepted = g2_cons and g2_cons['consent_status'] == 'ACCEPTED'
                if g2_accepted or override:
                    actual_from_g2, g2_periods = _deduct_member_savings(db, loan['g2_id'], from_g2)
                    if g2_cons:
                        db.execute(
                            "UPDATE guarantor_consents SET consent_status='ACCEPTED', consent_at=CURRENT_TIMESTAMP WHERE loan_id=? AND guarantor_id=?",
                            (loan_id, loan['g2_id']),
                        )

            total_actual = actual_from_borrower + actual_from_g1 + actual_from_g2
            actual_shortfall = total_owed - total_actual
            narrative = (
                f"Forced Recovery — Loan {loan['loan_no']}. "
                f"Borrower deduction: {fmt_money(actual_from_borrower)}, "
                f"G1 deduction: {fmt_money(actual_from_g1)}, "
                f"G2 deduction: {fmt_money(actual_from_g2)}. "
                f"Total recovered: {fmt_money(total_actual)}"
            )
            if actual_shortfall > 0:
                narrative += f". Unrecovered shortfall: {fmt_money(actual_shortfall)}"

            # Record the repayment to clear the loan
            db.execute(
                """INSERT INTO loan_repayments
                   (loan_id, amount, principal_part, interest_part, penalty_part,
                    payment_date, payment_method, notes, recorded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (loan_id, total_actual,
                 min(total_actual, int(pos['outstanding_principal'])),
                 min(max(0, total_actual - int(pos['outstanding_principal'])), int(pos['outstanding_interest'])),
                 0,
                 date.today().isoformat(), 'Forced Recovery', narrative,
                 session['user_id']),
            )

            # Mark loan as Cleared
            db.execute(
                """UPDATE loans SET status='Cleared', cleared_date=?, notes=?
                    WHERE id=?""",
                (date.today().isoformat(), narrative, loan_id),
            )

            # Update recovery record
            db.execute(
                """INSERT OR REPLACE INTO forced_loan_recoveries
                   (loan_id, triggered_by, recovery_from_borrower, recovery_from_g1,
                    recovery_from_g2, total_recovered, shortfall, status, narrative, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'COMPLETED', ?, CURRENT_TIMESTAMP)""",
                (loan_id, session['user_id'], actual_from_borrower, actual_from_g1,
                 actual_from_g2, total_actual, actual_shortfall, narrative),
            )
            db.commit()

            # Notify borrower
            borrower_arrears_months = len(b_periods)
            _notify_member(
                db, loan['member_id'],
                '⚠️ Forced Loan Recovery — Your Savings Deducted',
                (f"**FORCED RECOVERY NOTICE**\n\n"
                 f"Your defaulted loan *{loan['loan_no']}* has been forcibly recovered by the Treasurer "
                 f"as resolved by the committee.\n\n"
                 f"**Your role:** Borrower\n"
                 f"**Amount deducted from your savings:** {fmt_money(actual_from_borrower)}\n"
                 f"**Periods affected (now in arrears):** {borrower_arrears_months} month(s) — "
                 f"{', '.join(b_periods[:5])}{'...' if len(b_periods) > 5 else ''}\n\n"
                 f"You are required to repay {fmt_money(actual_from_borrower)} in savings contributions "
                 f"over the coming months to restore your savings hygiene. "
                 f"Please contact the Treasurer to arrange a repayment plan."),
                url_for('member.loans'),
            )

            # Notify guarantor 1
            if actual_from_g1 > 0 and loan['g1_id']:
                g1_arrears_months = len(g1_periods)
                _notify_member(
                    db, loan['g1_id'],
                    '⚠️ Guarantor Recovery Executed — Savings Deducted',
                    (f"**GUARANTOR RECOVERY NOTICE**\n\n"
                     f"As guarantor on loan *{loan['loan_no']}* for {loan['borrower_name']} ({loan['borrower_no']}), "
                     f"your savings have been partially used to recover this defaulted loan.\n\n"
                     f"**Your role:** Guarantor\n"
                     f"**Amount deducted from your savings:** {fmt_money(actual_from_g1)}\n"
                     f"**Periods now in arrears:** {g1_arrears_months} month(s) — "
                     f"{', '.join(g1_periods[:5])}{'...' if len(g1_periods) > 5 else ''}\n\n"
                     f"You are obligated to rebuild your savings to cover these {g1_arrears_months} month(s). "
                     f"Each month you must contribute the standard {fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)} "
                     f"PLUS catch-up on {fmt_money(actual_from_g1)} in arrears. "
                     f"Please contact the Treasurer immediately to confirm your repayment plan."),
                    url_for('member.savings_statement'),
                )

            # Notify guarantor 2
            if actual_from_g2 > 0 and loan['g2_id']:
                g2_arrears_months = len(g2_periods)
                _notify_member(
                    db, loan['g2_id'],
                    '⚠️ Guarantor Recovery Executed — Savings Deducted',
                    (f"**GUARANTOR RECOVERY NOTICE**\n\n"
                     f"As guarantor on loan *{loan['loan_no']}* for {loan['borrower_name']} ({loan['borrower_no']}), "
                     f"your savings have been partially used to recover this defaulted loan.\n\n"
                     f"**Your role:** Guarantor\n"
                     f"**Amount deducted from your savings:** {fmt_money(actual_from_g2)}\n"
                     f"**Periods now in arrears:** {g2_arrears_months} month(s) — "
                     f"{', '.join(g2_periods[:5])}{'...' if len(g2_periods) > 5 else ''}\n\n"
                     f"You are obligated to rebuild your savings to cover these {g2_arrears_months} month(s). "
                     f"Each month you must contribute the standard {fmt_money(Config.MONTHLY_SAVINGS_AMOUNT)} "
                     f"PLUS catch-up on {fmt_money(actual_from_g2)} in arrears. "
                     f"Please contact the Treasurer immediately to confirm your repayment plan."),
                    url_for('member.savings_statement'),
                )

            # Notify all committee
            _notify_roles(
                db,
                ['IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'],
                'Forced Recovery Executed',
                (f"Loan {loan['loan_no']} ({loan['borrower_name']}) forcibly recovered. "
                 f"Total owed: {fmt_money(total_owed)}. Recovered: {fmt_money(total_actual)}. "
                 f"Shortfall: {fmt_money(actual_shortfall)}."),
                url_for('admin.loan_detail', loan_id=loan_id),
                exclude_user_id=session['user_id'],
            )
            db.commit()

            log_action(
                'FORCED_RECOVERY_EXECUTED', 'loan', loan_id,
                f"Forced recovery of {loan['loan_no']}. Recovered {fmt_money(total_actual)} "
                f"/ {fmt_money(total_owed)}. Shortfall: {fmt_money(actual_shortfall)}.",
            )
            flash(
                f'Forced recovery executed for loan {loan["loan_no"]}. '
                f'Total recovered: {fmt_money(total_actual)} of {fmt_money(total_owed)} owed. '
                + (f'Unrecovered shortfall: {fmt_money(actual_shortfall)}. ' if actual_shortfall > 0 else '')
                + 'Loan marked Cleared. Affected members have been notified.',
                'success' if actual_shortfall == 0 else 'warning',
            )
            return redirect(url_for('admin.loan_detail', loan_id=loan_id))

        flash('Invalid action.', 'warning')
        return redirect(url_for('admin.force_recover', loan_id=loan_id))

    # GET — show recovery preview page
    return render_template(
        'admin/force_recovery.html',
        loan=loan,
        pos=pos,
        total_owed=total_owed,
        borrower_savings=borrower_savings,
        g1_savings=g1_savings,
        g2_savings=g2_savings,
        from_borrower=from_borrower,
        from_g1=from_g1,
        from_g2=from_g2,
        total_recoverable=total_recoverable,
        final_shortfall=final_shortfall,
        needs_guarantors=needs_guarantors,
        recovery=recovery,
        g1_consent=g1_consent,
        g2_consent=g2_consent,
        role=role,
    )
