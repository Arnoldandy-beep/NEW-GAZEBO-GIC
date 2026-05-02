"""
GAZEBO Investment Club - Member-side routes (read-only)
"""
import os
import uuid
import json
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, current_app
)
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

from database import get_db
from utils import (
    login_required, fmt_money, period_str, period_label,
    all_savings_periods, get_member_total_savings,
    get_member_locked_amount, get_member_available_savings,
    calculate_loan_position, calculate_due_date,
    determine_loan_security,
    calculate_member_max_eligible_loan,
)
from config import Config

bp = Blueprint('member', __name__, url_prefix='/member')

_IMAGE_MAGIC = {
    b'\xff\xd8\xff':           'jpg',
    b'\x89PNG\r\n\x1a\n':     'png',
    b'GIF87a':                  'gif',
    b'GIF89a':                  'gif',
    b'RIFF':                    'webp',
}


def _file_magic_ok(file_obj, allowed_exts):
    header = file_obj.read(12)
    file_obj.seek(0)
    ext_set = set(allowed_exts)
    for sig, ftype in _IMAGE_MAGIC.items():
        if ftype in ext_set and header[:len(sig)] == sig:
            return True
        if ftype == 'webp' and 'webp' in ext_set:
            if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                return True
    return False


def _save_upload(file_field_name, subfolder=''):
    f = request.files.get(file_field_name)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
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


def _notify_roles(db, roles, title, message, link=None):
    placeholders = ','.join(['?'] * len(roles))
    users = db.execute(
        f"""SELECT u.id
              FROM users u
              JOIN members m ON m.id = u.member_id
             WHERE u.is_active = 1 AND m.role IN ({placeholders})""",
        list(roles),
    ).fetchall()
    for u in users:
        db.execute(
            """INSERT INTO notifications (user_id, title, message, link)
               VALUES (?, ?, ?, ?)""",
            (u['id'], title, message, link),
        )


def _notify_member_user(db, member_id, title, message, link=None):
    user = db.execute(
        "SELECT id FROM users WHERE member_id=? AND is_active=1 ORDER BY id LIMIT 1",
        (member_id,),
    ).fetchone()
    if not user:
        return
    db.execute(
        """INSERT INTO notifications (user_id, title, message, link)
           VALUES (?, ?, ?, ?)""",
        (user['id'], title, message, link),
    )


@bp.before_request
@login_required
def _require_login():
    pass


def _current_member():
    db = get_db()
    member = db.execute(
        "SELECT * FROM members WHERE id = ?", (session['member_id'],)
    ).fetchone()
    if member:
        session['photo_url'] = member['photo_url']
    return member


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@bp.route('/')
def dashboard():
    db = get_db()
    member = _current_member()
    total_savings = get_member_total_savings(db, member['id'])
    locked = get_member_locked_amount(db, member['id'])
    available = max(0, total_savings - locked)

    # Savings status this month
    cur_period = period_str(date.today())
    cur_savings = db.execute(
        "SELECT * FROM savings WHERE member_id=? AND period=?",
        (member['id'], cur_period),
    ).fetchone()
    my_month_savings_paid = bool(cur_savings)
    my_month_savings_outstanding = 0 if my_month_savings_paid else Config.MONTHLY_SAVINGS_AMOUNT

    # Active loans
    active_loans = db.execute(
        """SELECT * FROM loans
            WHERE member_id=? AND status IN ('Active', 'Pending')
            ORDER BY issued_date DESC""",
        (member['id'],),
    ).fetchall()
    enriched_loans = []
    for loan in active_loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=?", (loan['id'],)
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        d = dict(loan)
        d.update(pos)
        enriched_loans.append(d)

    # Outstanding fines / fees
    fines = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM fines WHERE member_id=? AND status='Unpaid'",
        (member['id'],),
    ).fetchone()['s']

    current_year = date.today().year
    fee_row = db.execute(
        """SELECT * FROM annual_fees
            WHERE member_id=? AND year=?
            ORDER BY id DESC LIMIT 1""",
        (member['id'], current_year),
    ).fetchone()
    my_fee_paid = bool(fee_row and fee_row['status'] == 'Paid')
    my_fee_amount = int(fee_row['amount']) if fee_row else Config.ANNUAL_FEE
    my_fee_deadline = fee_row['deadline'] if fee_row and fee_row['deadline'] else f"{current_year}-06-30"

    my_fines_year = db.execute(
        """SELECT
                 COALESCE(SUM(CASE WHEN status='Paid' THEN amount ELSE 0 END), 0) collected,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN amount ELSE 0 END), 0) outstanding,
                 COALESCE(SUM(amount), 0) issued,
                 COALESCE(SUM(CASE WHEN status='Unpaid' THEN 1 ELSE 0 END), 0) unpaid_count
             FROM fines
            WHERE member_id=? AND strftime('%Y', fine_date)=?""",
        (member['id'], str(current_year)),
    ).fetchone()

    published_minutes_count = db.execute(
        "SELECT COUNT(*) c FROM minutes WHERE is_published=1"
    ).fetchone()['c']

    operations_my_collected = (my_fee_amount if my_fee_paid else 0) + int(my_fines_year['collected'] or 0)
    operations_my_projection = Config.ANNUAL_FEE + int(my_fines_year['issued'] or 0)
    operations_my_outstanding = max(0, operations_my_projection - operations_my_collected)

    my_loans_active_count = len([l for l in enriched_loans if l['status'] == 'Active'])
    my_loans_pending_count = len([l for l in enriched_loans if l['status'] == 'Pending'])
    my_loans_overdue_count = len([l for l in enriched_loans if l.get('is_overdue')])

    # Months in arrears
    expected_periods = all_savings_periods()
    join_date = member['join_date']
    if isinstance(join_date, str):
        join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
    else:
        join_d = join_date
    join_period = period_str(join_d)
    expected = [p for p in expected_periods if p >= join_period]
    paid_periods = {r['period'] for r in db.execute(
        "SELECT period FROM savings WHERE member_id=?",
        (member['id'],),
    ).fetchall()}
    arrears = [p for p in expected if p not in paid_periods]
    prior_arrears = [p for p in arrears if p < cur_period]

    # Savings trend for last 6 months (oldest -> newest)
    anchor = date.today().replace(day=1)
    trend_periods = [
        period_str(anchor - relativedelta(months=offset))
        for offset in range(5, -1, -1)
    ]
    placeholders = ','.join('?' * len(trend_periods))
    trend_rows = db.execute(
        f"""SELECT period, COALESCE(SUM(amount),0) AS amount
              FROM savings
             WHERE member_id=? AND period IN ({placeholders})
             GROUP BY period""",
        [member['id'], *trend_periods],
    ).fetchall()
    trend_map = {r['period']: int(r['amount'] or 0) for r in trend_rows}
    savings_trend = [
        {
            'period': p,
            'label': period_label(p),
            'amount': int(trend_map.get(p, 0)),
            'status': (
                'paid' if p in paid_periods else
                'pending' if p == cur_period else
                'unpaid'
            ),
        }
        for p in trend_periods
    ]
    savings_trend_max = max([p['amount'] for p in savings_trend] or [0])

    # Courtesy title for greeting
    gender_value = (member['gender'] if 'gender' in member.keys() else '') or ''
    gender_value = gender_value.strip().lower()
    if gender_value.startswith('m'):
        greeting_prefix = 'Mr.'
    elif gender_value.startswith('f'):
        greeting_prefix = 'Miss'
    else:
        greeting_prefix = 'Mr./Ms.'

    # Compliance color states
    fee_status_state = 'ok' if my_fee_paid else 'bad'
    if my_month_savings_paid:
        month_savings_state = 'ok'
    elif prior_arrears:
        month_savings_state = 'bad'
    else:
        month_savings_state = 'warn'

    # Latest minutes (5)
    latest_minutes = db.execute(
        "SELECT * FROM minutes WHERE is_published=1 ORDER BY meeting_date DESC LIMIT 5"
    ).fetchall()

    # Latest dividend payouts for this member
    payouts = db.execute(
        """SELECT dp.*, dr.year, dr.distribution_date
             FROM dividend_payouts dp
             JOIN dividend_runs dr ON dr.id = dp.dividend_run_id
            WHERE dp.member_id=?
              AND dr.status IN ('Published', 'Distributed')
            ORDER BY dr.year DESC""",
        (member['id'],),
    ).fetchall()

    unread_notifications = db.execute(
        """SELECT * FROM notifications
            WHERE user_id = ? AND is_read = 0
            ORDER BY id DESC LIMIT 10""",
        (session['user_id'],),
    ).fetchall()

    return render_template(
        'member/dashboard.html',
        member=member,
        total_savings=total_savings,
        locked=locked,
        available=available,
        cur_period=cur_period,
        cur_period_pretty=period_label(cur_period),
        cur_savings=cur_savings,
        my_month_savings_paid=my_month_savings_paid,
        my_month_savings_outstanding=my_month_savings_outstanding,
        active_loans=enriched_loans,
        outstanding_fines=fines,
        my_fee_paid=my_fee_paid,
        my_fee_amount=my_fee_amount,
        my_fee_deadline=my_fee_deadline,
        my_fines_collected=my_fines_year['collected'],
        my_fines_outstanding=my_fines_year['outstanding'],
        my_fines_unpaid_count=my_fines_year['unpaid_count'],
        operations_my_collected=operations_my_collected,
        operations_my_projection=operations_my_projection,
        operations_my_outstanding=operations_my_outstanding,
        published_minutes_count=published_minutes_count,
        my_loans_active_count=my_loans_active_count,
        my_loans_pending_count=my_loans_pending_count,
        my_loans_overdue_count=my_loans_overdue_count,
        current_year=current_year,
        arrears_count=len(arrears),
        arrears_amount=len(arrears) * Config.MONTHLY_SAVINGS_AMOUNT,
        prior_arrears_count=len(prior_arrears),
        prior_arrears_amount=len(prior_arrears) * Config.MONTHLY_SAVINGS_AMOUNT,
        fee_status_state=fee_status_state,
        month_savings_state=month_savings_state,
        greeting_prefix=greeting_prefix,
        savings_trend=savings_trend,
        savings_trend_max=savings_trend_max,
        latest_minutes=latest_minutes,
        unread_notifications=unread_notifications,
        payouts=payouts,
    )


@bp.route('/notifications/<int:notification_id>/open')
def notification_open(notification_id):
    db = get_db()
    n = db.execute(
        "SELECT * FROM notifications WHERE id=? AND user_id=?",
        (notification_id, session['user_id']),
    ).fetchone()
    if not n:
        flash('Notification not found.', 'warning')
        return redirect(url_for('member.dashboard'))

    db.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notification_id,))
    db.commit()
    if n['link']:
        return redirect(n['link'])
    return redirect(url_for('member.dashboard'))


@bp.route('/notifications')
@login_required
def notifications_page():
    db = get_db()
    member = _current_member()
    all_notifs = db.execute(
        """SELECT * FROM notifications
            WHERE user_id=?
            ORDER BY id DESC LIMIT 150""",
        (session['user_id'],),
    ).fetchall()
    unread_count = sum(1 for n in all_notifs if not n['is_read'])
    return render_template(
        'member/notifications.html',
        member=member,
        notifications=all_notifs,
        unread_count=unread_count,
    )


@bp.route('/notifications/mark-all-read', methods=['POST'])
@login_required
def notifications_mark_all_read():
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (session['user_id'],))
    db.commit()
    return redirect(url_for('member.notifications_page'))


# ---------------------------------------------------------------------------
# Savings Statement
# ---------------------------------------------------------------------------
@bp.route('/savings')
def savings_statement():
    db = get_db()
    member = _current_member()

    savings = db.execute(
        "SELECT * FROM savings WHERE member_id=? ORDER BY period DESC",
        (member['id'],),
    ).fetchall()

    total = sum(s['amount'] for s in savings)
    locked = get_member_locked_amount(db, member['id'])
    available = max(0, total - locked)

    # Build comprehensive history including missing periods
    expected_periods = all_savings_periods()
    join_date = member['join_date']
    if isinstance(join_date, str):
        join_d = datetime.strptime(join_date, '%Y-%m-%d').date()
    else:
        join_d = join_date
    join_period = period_str(join_d)
    expected = [p for p in expected_periods if p >= join_period]

    paid_map = {s['period']: s for s in savings}
    history = []
    running_total = 0
    # iterate ascending so we can compute running total correctly
    for p in expected:
        s = paid_map.get(p)
        amount = s['amount'] if s else 0
        running_total += amount
        history.append({
            'period':       p,
            'label':        period_label(p),
            'amount':       amount,
            'paid':         bool(s),
            'payment_date': s['payment_date'] if s else None,
            'method':       s['payment_method'] if s else None,
            'reference':    s['reference_no'] if s else None,
            'running':      running_total,
        })
    # show newest first
    history.reverse()

    return render_template(
        'member/savings_statement.html',
        member=member,
        total=total, locked=locked, available=available,
        history=history,
        expected_count=len(expected),
        paid_count=len(savings),
    )


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------
@bp.route('/loans')
def loans():
    db = get_db()
    member = _current_member()

    loans = db.execute(
        """SELECT l.*,
                  m1.full_name AS g1_name, m1.member_no AS g1_no,
                  m2.full_name AS g2_name, m2.member_no AS g2_no
             FROM loans l
             LEFT JOIN members m1 ON m1.id = l.guarantor1_id
             LEFT JOIN members m2 ON m2.id = l.guarantor2_id
            WHERE l.member_id = ?
            ORDER BY l.issued_date DESC""",
        (member['id'],),
    ).fetchall()

    enriched = []
    for loan in loans:
        repays = db.execute(
            "SELECT * FROM loan_repayments WHERE loan_id=? ORDER BY payment_date",
            (loan['id'],),
        ).fetchall()
        pens = db.execute(
            "SELECT * FROM loan_penalties WHERE loan_id=? ORDER BY period",
            (loan['id'],),
        ).fetchall()
        pos = calculate_loan_position(loan, repays, pens)
        d = dict(loan)
        d.update(pos)
        d['repayments'] = repays
        d['penalties'] = pens
        enriched.append(d)

    # Loans the member has guaranteed (showing risk exposure)
    guaranteed = db.execute(
        """SELECT l.*, m.member_no, m.full_name AS borrower
             FROM loans l
             JOIN members m ON m.id = l.member_id
            WHERE (l.guarantor1_id=? OR l.guarantor2_id=?)
              AND l.status IN ('Active','Pending')
            ORDER BY l.issued_date DESC""",
        (member['id'], member['id']),
    ).fetchall()

    return render_template(
        'member/loans.html',
        member=member, loans=enriched, guaranteed=guaranteed,
    )


# ---------------------------------------------------------------------------
# Loan Eligibility
# ---------------------------------------------------------------------------
@bp.route('/loan-eligibility', methods=['GET', 'POST'])
def loan_eligibility():
    db = get_db()
    member = _current_member()

    own = get_member_available_savings(db, member['id'])

    # Check for an open loan
    open_loan = db.execute(
        """SELECT * FROM loans WHERE member_id=?
              AND status IN ('Pending','Active')""",
        (member['id'],),
    ).fetchone()

    # All other members for guarantor selection
    candidates = db.execute(
        """SELECT * FROM members
            WHERE id != ? AND status='Active'
            ORDER BY member_no""",
        (member['id'],),
    ).fetchall()

    # If a check is being run
    check_result = None
    if request.method == 'POST':
        principal = int(request.form.get('principal') or 0)
        term_months = int(request.form.get('term_months') or 1)
        g1_id = request.form.get('guarantor1_id')
        g2_id = request.form.get('guarantor2_id')
        g1_id = int(g1_id) if g1_id else None
        g2_id = int(g2_id) if g2_id else None

        if open_loan:
            flash('You already have an active or pending loan; clear it first.',
                  'warning')
        elif principal <= 0:
            flash('Enter a positive principal amount.', 'warning')
        elif g1_id and g2_id and g1_id == g2_id:
            flash('Choose two different guarantors.', 'warning')
        elif g1_id == member['id'] or g2_id == member['id']:
            flash('You cannot guarantee yourself.', 'warning')
        else:
            policy_caps = calculate_member_max_eligible_loan(
                db, member['id'], g1_id, g2_id, term_months
            )
            check_result = determine_loan_security(
                db, member['id'], principal, g1_id, g2_id
            )
            check_result['principal'] = principal
            check_result['interest'] = int(
                principal * Config.LOAN_INTEREST_RATE_MONTHLY *
                term_months
            )
            check_result['term_months'] = term_months
            check_result['total_due'] = principal + check_result['interest']
            check_result['monthly_installment'] = (
                check_result['total_due'] // term_months if term_months else check_result['total_due']
            )
            check_result['final_installment'] = (
                check_result['total_due'] - (check_result['monthly_installment'] * max(0, term_months - 1))
            )
            check_result['due_date'] = calculate_due_date(date.today(), term_months)
            check_result['possible_penalty_monthly'] = int(
                principal * Config.LOAN_PENALTY_RATE_MONTHLY
            )
            check_result['max_eligible'] = policy_caps['max_eligible']
            check_result['policy_caps'] = policy_caps

            if principal > policy_caps['max_eligible']:
                check_result['sufficient'] = False
                check_result['message'] = (
                    'Requested principal exceeds your current maximum eligible loan of '
                    f"{fmt_money(policy_caps['max_eligible'])}."
                )

            if g1_id:
                g1_row = db.execute("SELECT * FROM members WHERE id=?",
                                    (g1_id,)).fetchone()
                check_result['g1_name'] = g1_row['full_name']
                check_result['g1_no'] = g1_row['member_no']
            if g2_id:
                g2_row = db.execute("SELECT * FROM members WHERE id=?",
                                    (g2_id,)).fetchone()
                check_result['g2_name'] = g2_row['full_name']
                check_result['g2_no'] = g2_row['member_no']

    return render_template(
        'member/eligibility.html',
        member=member,
        own_available=own,
        candidates=candidates,
        open_loan=open_loan,
        check_result=check_result,
        max_term=Config.LOAN_MAX_TERM_MONTHS,
        rate_pct=int(Config.LOAN_INTEREST_RATE_MONTHLY * 100),
    )


# ---------------------------------------------------------------------------
# Minutes
# ---------------------------------------------------------------------------
@bp.route('/minutes')
def minutes():
    db = get_db()
    member = _current_member()
    rows = db.execute(
        """SELECT m.*, mem.full_name AS chair_name
             FROM minutes m
             LEFT JOIN members mem ON mem.id = m.chaired_by
            WHERE m.is_published = 1
            ORDER BY m.meeting_date DESC"""
    ).fetchall()
    return render_template('member/minutes.html', member=member, minutes=rows)


@bp.route('/minutes/<int:minutes_id>')
def minutes_detail(minutes_id):
    db = get_db()
    member = _current_member()
    m = db.execute(
        """SELECT m.*, mem.full_name AS chair_name, mem.member_no AS chair_no
             FROM minutes m
             LEFT JOIN members mem ON mem.id = m.chaired_by
            WHERE m.id = ? AND m.is_published = 1""",
        (minutes_id,),
    ).fetchone()
    if not m:
        flash('Minutes not found.', 'danger')
        return redirect(url_for('member.minutes'))

    attendees = []
    if m['attendance']:
        ids = [int(x) for x in m['attendance'].split(',') if x.strip().isdigit()]
        if ids:
            placeholders = ','.join('?' * len(ids))
            attendees = db.execute(
                f"SELECT id, full_name, member_no FROM members WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
    return render_template('member/minutes_detail.html', member=member, m=m,
                           attendees=attendees)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@bp.route('/profile')
def profile():
    db = get_db()
    member = _current_member()
    user = db.execute("SELECT * FROM users WHERE member_id=?",
                      (member['id'],)).fetchone()

    fines = db.execute(
        "SELECT * FROM fines WHERE member_id=? ORDER BY fine_date DESC",
        (member['id'],),
    ).fetchall()
    fees = db.execute(
        "SELECT * FROM annual_fees WHERE member_id=? ORDER BY year DESC",
        (member['id'],),
    ).fetchall()
    payouts = db.execute(
        """SELECT dp.*, dr.year, dr.distribution_date
             FROM dividend_payouts dp
             JOIN dividend_runs dr ON dr.id = dp.dividend_run_id
            WHERE dp.member_id=?
              AND dr.status IN ('Published', 'Distributed')
            ORDER BY dr.year DESC""",
        (member['id'],),
    ).fetchall()

    pending_nok_request = db.execute(
        """SELECT *
             FROM member_profile_change_requests
            WHERE member_id=? AND request_type='NEXT_OF_KIN' AND status='Pending'
            ORDER BY created_at DESC LIMIT 1""",
        (member['id'],),
    ).fetchone()

    pending_nok_payload = {}
    if pending_nok_request:
        try:
            pending_nok_payload = json.loads(pending_nok_request['payload_json'] or '{}')
        except (TypeError, ValueError):
            pending_nok_payload = {}

    return render_template(
        'member/profile.html',
        member=member, user=user,
        fines=fines, fees=fees, payouts=payouts,
        pending_nok_request=pending_nok_request,
        pending_nok_payload=pending_nok_payload,
    )


@bp.route('/profile/next-of-kin-request', methods=['POST'])
def profile_next_of_kin_request():
    db = get_db()
    member = _current_member()
    next_of_kin = (request.form.get('next_of_kin') or '').strip()
    nok_relationship = (request.form.get('nok_relationship') or '').strip()
    nok_phone = (request.form.get('nok_phone') or '').strip()
    nok_address = (request.form.get('nok_address') or '').strip()

    if not next_of_kin or not nok_relationship or not nok_phone:
        flash('Enter Next of Kin full name, relationship, and phone number.', 'warning')
        return redirect(url_for('member.profile'))

    existing_pending = db.execute(
        """SELECT id
             FROM member_profile_change_requests
            WHERE member_id=? AND request_type='NEXT_OF_KIN' AND status='Pending'
            ORDER BY created_at DESC LIMIT 1""",
        (member['id'],),
    ).fetchone()

    payload = json.dumps({
        'next_of_kin': next_of_kin,
        'nok_relationship': nok_relationship,
        'nok_phone': nok_phone,
        'nok_address': nok_address,
    })
    if existing_pending:
        db.execute(
            """UPDATE member_profile_change_requests
                  SET payload_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (payload, existing_pending['id']),
        )
        request_id = existing_pending['id']
    else:
        db.execute(
            """INSERT INTO member_profile_change_requests
               (member_id, request_type, payload_json, status, requested_by_user_id)
               VALUES (?, 'NEXT_OF_KIN', ?, 'Pending', ?)""",
            (member['id'], payload, session['user_id']),
        )
        request_id = db.execute("SELECT last_insert_rowid() AS i").fetchone()['i']

    _notify_roles(
        db,
        ['TREASURER', 'SECRETARY', 'IT_ADMIN'],
        'Member Next of Kin update requires approval',
        f"{member['full_name']} ({member['member_no']}) submitted Next of Kin update request #{request_id}.",
        url_for('admin.approvals'),
    )
    db.commit()
    flash('Submitted successfully. The details were sent for Treasurer approval and the Club Secretary will update your details within 2 hours.', 'success')
    return redirect(url_for('member.profile'))


@bp.route('/profile/national-id-copy', methods=['POST'])
def profile_upload_national_id_copy():
    flash('National ID copies are managed by the Secretary or IT Admin from your member record.', 'info')
    return redirect(url_for('member.profile'))
