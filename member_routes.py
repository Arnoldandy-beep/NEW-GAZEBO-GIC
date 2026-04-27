"""
GAZEBO Investment Club - Member-side routes (read-only)
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, current_app
)
from datetime import datetime, date

from database import get_db
from utils import (
    login_required, fmt_money, period_str, period_label,
    all_savings_periods, get_member_total_savings,
    get_member_locked_amount, get_member_available_savings,
    calculate_loan_position, calculate_max_loan_amount,
    determine_loan_security,
)
from config import Config

bp = Blueprint('member', __name__, url_prefix='/member')


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
            ORDER BY dr.year DESC""",
        (member['id'],),
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
        latest_minutes=latest_minutes,
        payouts=payouts,
    )


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
    enriched_candidates = []
    for c in candidates:
        avail = get_member_available_savings(db, c['id'])
        d = dict(c)
        d['available_savings'] = avail
        enriched_candidates.append(d)

    # Top 2 suggested guarantors (highest available)
    top2 = sorted(
        enriched_candidates, key=lambda x: x['available_savings'], reverse=True
    )[:2]
    suggested_max = own + sum(g['available_savings'] for g in top2)

    # If a check is being run
    check_result = None
    if request.method == 'POST':
        principal = int(request.form.get('principal') or 0)
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
            check_result = determine_loan_security(
                db, member['id'], principal, g1_id, g2_id
            )
            check_result['principal'] = principal
            check_result['interest'] = int(
                principal * Config.LOAN_INTEREST_RATE_MONTHLY *
                int(request.form.get('term_months') or 1)
            )
            check_result['term_months'] = int(request.form.get('term_months') or 1)
            check_result['total_due'] = principal + check_result['interest']
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
        candidates=enriched_candidates,
        top_guarantors=top2,
        suggested_max=suggested_max,
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
    rows = db.execute(
        """SELECT m.*, mem.full_name AS chair_name
             FROM minutes m
             LEFT JOIN members mem ON mem.id = m.chaired_by
            WHERE m.is_published = 1
            ORDER BY m.meeting_date DESC"""
    ).fetchall()
    return render_template('member/minutes.html', minutes=rows)


@bp.route('/minutes/<int:minutes_id>')
def minutes_detail(minutes_id):
    db = get_db()
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
    return render_template('member/minutes_detail.html', m=m,
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
            ORDER BY dr.year DESC""",
        (member['id'],),
    ).fetchall()

    return render_template(
        'member/profile.html',
        member=member, user=user,
        fines=fines, fees=fees, payouts=payouts,
    )
