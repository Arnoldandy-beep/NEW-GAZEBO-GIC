"""
GAZEBO Investment Club - Database initialization & seed
Run directly with: python init_db.py  (will recreate the DB)
"""
import os
import sys
from datetime import date, datetime
from werkzeug.security import generate_password_hash

from database import standalone_db, init_schema
from config import Config
from utils import (
    period_str, end_of_month, calculate_due_date, all_savings_periods,
)


# Members extracted from the GAZEBO_GIC_Mature_Reports_2026-04-25.xlsx file.
# IT_ADMIN/CHAIRMAN/SECRETARY/TREASURER/COMMITTEE/MEMBER  -- roles you can edit later.
MEMBERS = [
    # member_no, full_name, phone, role, join_date
    ('GIC-001', 'SSEMPALA IVAN',         '0704-023988', 'CHAIRMAN',  '2025-08-01'),
    ('GIC-002', 'KALANGWA ROBERT',       '0787-326470', 'TREASURER', '2025-08-01'),
    ('GIC-003', 'KASULE JOASH',          '0782-947777', 'COMMITTEE', '2025-08-01'),
    ('GIC-004', 'MUGISHA PETER',         '',            'MEMBER',    '2025-08-01'),
    ('GIC-005', 'MUSHANA FRED',          '0773-155238', 'MEMBER',    '2025-08-01'),
    ('GIC-006', 'SSEKITTO CHRISTOPHER',  '0782-360518', 'IT_ADMIN',  '2025-08-01'),
    ('GIC-007', 'NSOBYA ARNOLD',         '',            'SECRETARY', '2025-08-01'),
    ('GIC-008', 'MURUNGI J. JONATHAN',   '0752-721833', 'MEMBER',    '2025-08-01'),
    ('GIC-009', 'KIRONDE TONNY',         '',            'COMMITTEE', '2025-08-01'),
    ('GIC-010', 'ANNITAR TUMWEBAZE',     '0776-336044', 'MEMBER',    '2025-08-01'),
    ('GIC-011', 'KYANKWANZI ROGERS',     '0741-905728', 'MEMBER',    '2025-08-01'),
    ('GIC-012', 'SSENTALE BRIAN',        '0756-072590', 'MEMBER',    '2025-08-01'),
    ('GIC-013', 'NSUBUGA DICKSON',       '0777-752002', 'MEMBER',    '2025-08-01'),
    ('GIC-014', 'TUMUSIIME NATHAN',      '0703-954599', 'MEMBER',    '2025-08-01'),
    ('GIC-015', 'NAMBOOZE GRACE',        '',            'MEMBER',    '2025-08-01'),
    ('GIC-016', 'OKELLO DAVID',          '',            'MEMBER',    '2025-08-01'),
    ('GIC-017', 'KAWOOJWA WATSON',       '0703200573',  'MEMBER',    '2025-09-01'),
]

# (member_no, [periods that are PAID]) -- everything else within their join->Mar26 window is unpaid
# Build paid set from the savings defaulters list (the OPPOSITE).
# Defaulters per the Excel "Savings Defaulters" sheet:
DEFAULTER_INFO = {
    'GIC-011': {'arrears': 7, 'oldest': '2025-09', 'latest': '2026-03'},
    'GIC-012': {'arrears': 7, 'oldest': '2025-09', 'latest': '2026-03'},
    'GIC-014': {'arrears': 6, 'oldest': '2025-10', 'latest': '2026-03'},
    'GIC-013': {'arrears': 4, 'oldest': '2025-12', 'latest': '2026-03'},
    'GIC-005': {'arrears': 3, 'oldest': '2026-01', 'latest': '2026-03'},
    'GIC-008': {'arrears': 3, 'oldest': '2026-01', 'latest': '2026-03'},
    'GIC-010': {'arrears': 2, 'oldest': '2026-02', 'latest': '2026-03'},
    'GIC-002': {'arrears': 1, 'oldest': '2026-03', 'latest': '2026-03'},
    'GIC-017': {'arrears': 1, 'oldest': '2026-03', 'latest': '2026-03'},
}

# Members marked GIC-004, GIC-015, GIC-016 are placeholders for "unidentified"
# inactive/dormant members from the report (16 active out of 17 numbers used).
# They can be edited or deactivated later. Mark GIC-016 inactive for now.
INACTIVE_MEMBERS = {'GIC-016'}

# Loans from the Loan Book sheet
LOANS = [
    # loan_no, borrower_name, principal, rate, term, issued_date, approved_date,
    # disbursed_date, due_date, status, total_paid, g1, g2
    ('GIC-L-002', 'SSEKITTO CHRISTOPHER', 1_000_000, 0.05, 1,
     '2026-01-02', '2026-01-02', '2026-01-05', '2026-02-02', 'Cleared',
     1_050_000, 'SSEMPALA IVAN', 'KALANGWA ROBERT'),
    ('GIC-L-003', 'KASULE JOASH', 1_000_000, 0.05, 1,
     '2026-02-11', '2026-02-11', '2026-02-11', '2026-03-14', 'Active',
     0, 'KIRONDE TONNY', 'SSEMPALA IVAN'),
    ('GIC-L-004', 'KIRONDE TONNY', 600_000, 0.05, 1,
     '2026-04-01', '2026-04-01', '2026-04-01', '2026-05-02', 'Cleared',
     630_000, 'SSEMPALA IVAN', 'NSOBYA ARNOLD'),
    ('GIC-L-005', 'SSEMPALA IVAN', 500_000, 0.05, 1,
     '2026-03-23', '2026-03-23', '2026-03-23', '2026-04-23', 'Active',
     0, 'KALANGWA ROBERT', 'SSEKITTO CHRISTOPHER'),
    ('GIC-L-006', 'SSEKITTO CHRISTOPHER', 1_600_000, 0.05, 1,
     '2026-03-03', '2026-03-03', '2026-03-03', '2026-04-03', 'Cleared',
     1_680_000, 'SSEMPALA IVAN', 'KIRONDE TONNY'),
    ('GIC-L-007', 'KIRONDE TONNY', 1_000_000, 0.05, 1,
     '2026-02-28', '2026-02-28', '2026-02-28', '2026-03-31', 'Cleared',
     1_050_000, 'KALANGWA ROBERT', 'SSEMPALA IVAN'),
    ('GIC-L-008', 'SSEKITTO CHRISTOPHER', 1_800_000, 0.05, 1,
     '2026-03-12', '2026-03-12', '2026-03-12', '2026-04-12', 'Active',
     180_000, 'KALANGWA ROBERT', 'SSEMPALA IVAN'),
]

# Annual fees paid in 2026 (from Annual Fees sheet)
ANNUAL_FEES_PAID = [
    ('SSEKITTO CHRISTOPHER', 2026, 50_000, '2026-04-16'),
    ('NSOBYA ARNOLD',        2026, 50_000, '2026-04-23'),
    ('KIRONDE TONNY',        2026, 50_000, '2026-04-24'),
]

# Fines from the Fines sheet
FINES = [
    ('KASULE JOASH',         '2026-04-04', 'Absent without prior apology', 20_000),
    ('MURUNGI J. JONATHAN',  '2026-04-04', 'Absent without prior apology', 20_000),
]


# Sample meeting minutes
SAMPLE_MINUTES = [
    {
        'meeting_date': '2025-08-15',
        'title': 'Inaugural Meeting - Club Founding',
        'venue': 'Kampala',
        'agenda': '1. Election of office bearers\n'
                  '2. Adoption of constitution\n'
                  '3. Setting of monthly contribution\n'
                  '4. Setting of loan terms and interest rate\n'
                  '5. AOB',
        'discussion': 'The members agreed to establish GAZEBO Investment Club. '
                      'Monthly mandatory savings of UGX 100,000 starting August 2025. '
                      'Loans at 5% per month, max 6-month term, all due by month-end. '
                      'Two guarantors required, savings serve as primary collateral. '
                      'Year-end interest distribution: top saver gets UGX 50,000 award, '
                      'rest shared equally among members.',
        'resolutions': '1. Monthly savings set at UGX 100,000.\n'
                       '2. Loan interest set at 5% per month.\n'
                       '3. Maximum loan term: 6 months, all due by 30th of due month.\n'
                       '4. Annual fee of UGX 50,000 due by 30 June each year.\n'
                       '5. Office bearers elected as recorded.',
    },
    {
        'meeting_date': '2026-04-04',
        'title': 'Q1 2026 Review & Disciplinary Session',
        'venue': 'Kampala',
        'agenda': '1. Review of Q1 savings collections\n'
                  '2. Loan portfolio update\n'
                  '3. Attendance fines\n'
                  '4. AOB',
        'discussion': 'The treasurer presented Q1 savings showing several members '
                      'in arrears. Loan book reviewed - three loans currently active. '
                      'Members absent without apology fined UGX 20,000 each.',
        'resolutions': '1. Defaulting members to clear arrears by next meeting.\n'
                       '2. Fines of UGX 20,000 issued for unexcused absence.\n'
                       '3. Penalty interest to be enforced strictly on overdue loans.',
    },
]


# Default password for all seeded users
DEFAULT_PASSWORD = 'gazebo123'


def seed(db_path):
    """Populate the database with members, savings, loans, and minutes."""
    if not os.path.exists(db_path):
        init_schema(db_path)

    with standalone_db(db_path) as conn:
        # --- 1. Members ---
        member_ids = {}  # name -> id; member_no -> id
        for member_no, full_name, phone, role, join_date in MEMBERS:
            existing = conn.execute(
                "SELECT id FROM members WHERE member_no=?", (member_no,)
            ).fetchone()
            if existing:
                member_ids[full_name] = existing['id']
                member_ids[member_no] = existing['id']
                continue
            status = 'Inactive' if member_no in INACTIVE_MEMBERS else 'Active'
            cur = conn.execute(
                """INSERT INTO members
                   (member_no, full_name, phone, role, join_date, status, district)
                   VALUES (?, ?, ?, ?, ?, ?, 'Kampala')""",
                (member_no, full_name, phone, role, join_date, status),
            )
            member_ids[full_name] = cur.lastrowid
            member_ids[member_no] = cur.lastrowid

        # --- 2. User logins for seeded members ---
        for member_no, full_name, phone, role, join_date in MEMBERS:
            mid = member_ids[member_no]
            existing = conn.execute(
                "SELECT id FROM users WHERE member_id=?", (mid,)
            ).fetchone()
            if existing:
                continue
            # username = first name (lowercase), unique
            base = full_name.split()[0].lower().replace('.', '')
            username = base
            i = 1
            while conn.execute(
                "SELECT 1 FROM users WHERE username=?", (username,)
            ).fetchone():
                i += 1
                username = f"{base}{i}"
            # IT admin doesn't need to change pw on first login? Let them, for safety.
            must_change = 1
            conn.execute(
                """INSERT INTO users
                   (username, password_hash, member_id, must_change_pw)
                   VALUES (?, ?, ?, ?)""",
                (username, generate_password_hash(DEFAULT_PASSWORD),
                 mid, must_change),
            )

        # --- 3. Savings: derive from defaulter info ---
        all_periods = all_savings_periods(date(2026, 4, 30))  # up to Apr-26
        # Trim to start from Aug-2025
        for member_no, full_name, phone, role, join_date in MEMBERS:
            if member_no in INACTIVE_MEMBERS:
                continue  # they don't save

            mid = member_ids[member_no]
            join_period = period_str(join_date)

            # Determine which periods the member did NOT pay
            unpaid_set = set()
            info = DEFAULTER_INFO.get(member_no)
            # The Excel sheet shows period range up to Mar-26.
            # Apr-26 not yet captured for anyone (5 contributors per the trends sheet)
            # -> Apr-26 is treated as not yet recorded for everyone except top savers.
            # We derive paid status as follows:
            #   - For listed defaulters: their oldest..latest range as unpaid.
            #   - Apr-26 for everyone except a small list of "top savers".
            if info:
                from dateutil.relativedelta import relativedelta as _rd
                start = datetime.strptime(info['oldest'], '%Y-%m')
                end = datetime.strptime(info['latest'], '%Y-%m')
                cur = start
                while cur <= end:
                    unpaid_set.add(cur.strftime('%Y-%m'))
                    cur += _rd(months=1)

            # Apr-26 only paid by the 5 most active savers (5 contributors)
            apr26_payers = {
                'GIC-006', 'GIC-007', 'GIC-009', 'GIC-001', 'GIC-003',
            }
            for p in all_periods:
                if p < join_period:
                    continue
                if p in unpaid_set:
                    continue
                if p == '2026-04' and member_no not in apr26_payers:
                    continue
                # Already inserted?
                if conn.execute(
                    "SELECT 1 FROM savings WHERE member_id=? AND period=?",
                    (mid, p),
                ).fetchone():
                    continue
                # payment_date = end of that month (last day)
                pay_d = end_of_month(p + '-01')
                conn.execute(
                    """INSERT INTO savings
                       (member_id, period, amount, payment_date, payment_method, notes)
                       VALUES (?, ?, ?, ?, 'Cash', 'Seeded from migration')""",
                    (mid, p, Config.MONTHLY_SAVINGS_AMOUNT,
                     pay_d.isoformat()),
                )

        # --- 4. Loans ---
        loan_id_map = {}  # loan_no -> id
        for (loan_no, borrower, principal, rate, term, issued, approved,
             disbursed, due, status, total_paid, g1, g2) in LOANS:
            existing = conn.execute(
                "SELECT id FROM loans WHERE loan_no=?", (loan_no,)
            ).fetchone()
            if existing:
                loan_id_map[loan_no] = existing['id']
                continue
            mid = member_ids.get(borrower)
            g1_id = member_ids.get(g1)
            g2_id = member_ids.get(g2)
            if mid is None:
                print(f"  ! skipped loan {loan_no}: borrower not found ({borrower})")
                continue

            # Compute collateral lock based on savings at issue time
            # For seeded loans, lock the principal against borrower savings if possible
            # Get total savings of borrower (gross)
            sav = conn.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM savings WHERE member_id=?",
                (mid,),
            ).fetchone()['s']
            self_lock = min(principal, sav)
            remaining = principal - self_lock
            g1_lock = 0
            g2_lock = 0
            if remaining > 0 and g1_id and g2_id:
                half = (remaining + 1) // 2
                g1_sav = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) s FROM savings WHERE member_id=?",
                    (g1_id,),
                ).fetchone()['s']
                g2_sav = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) s FROM savings WHERE member_id=?",
                    (g2_id,),
                ).fetchone()['s']
                g1_lock = min(half, g1_sav)
                g2_lock = min(remaining - g1_lock, g2_sav)

            # Cleared loans don't lock anything
            if status in ('Cleared', 'Declined'):
                self_lock = g1_lock = g2_lock = 0

            cur = conn.execute(
                """INSERT INTO loans
                   (loan_no, member_id, principal, interest_rate, term_months,
                    purpose, issued_date, approved_date, disbursed_date, due_date,
                    status, guarantor1_id, guarantor2_id,
                    self_locked_amount, g1_locked_amount, g2_locked_amount,
                    cleared_date)
                   VALUES (?, ?, ?, ?, ?, 'Migrated from spreadsheet',
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (loan_no, mid, principal, rate, term,
                 issued, approved, disbursed, due, status,
                 g1_id, g2_id,
                 self_lock, g1_lock, g2_lock,
                 issued if status == 'Cleared' else None),
            )
            loan_id_map[loan_no] = cur.lastrowid

            # Record total_paid as a single repayment if cleared or partially paid
            if total_paid > 0:
                interest = int(principal * rate * term)
                # Allocate: first interest, then principal
                int_part = min(total_paid, interest)
                prin_part = max(0, min(total_paid - int_part, principal))
                conn.execute(
                    """INSERT INTO loan_repayments
                       (loan_id, amount, principal_part, interest_part, penalty_part,
                        payment_date, payment_method, notes)
                       VALUES (?, ?, ?, ?, 0, ?, 'Cash', 'Migrated from spreadsheet')""",
                    (cur.lastrowid, total_paid, prin_part, int_part,
                     issued if status == 'Cleared' else due),
                )

        # --- 5. Loan penalties (overdue active loans) ---
        # GIC-L-003 overdue 42 days (2 months penalty), GIC-L-008 (1 month), GIC-L-005 (1 month)
        penalty_data = [
            ('GIC-L-003', '2026-04', 50_000, '2026-04-01'),  # 1st penalty
            ('GIC-L-003', '2026-05', 50_000, '2026-05-01'),  # 2nd penalty
            ('GIC-L-008', '2026-05', 90_000, '2026-05-01'),
            ('GIC-L-005', '2026-05', 25_000, '2026-05-01'),
        ]
        for loan_no, period, amt, applied in penalty_data:
            lid = loan_id_map.get(loan_no)
            if not lid:
                continue
            existing = conn.execute(
                "SELECT 1 FROM loan_penalties WHERE loan_id=? AND period=?",
                (lid, period),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO loan_penalties
                   (loan_id, period, penalty_amount, applied_date, notes)
                   VALUES (?, ?, ?, ?, 'Migrated from spreadsheet')""",
                (lid, period, amt, applied),
            )

        # --- 6. Annual Fees ---
        for member_name, year, amount, paid_date in ANNUAL_FEES_PAID:
            mid = member_ids.get(member_name)
            if not mid:
                continue
            existing = conn.execute(
                "SELECT 1 FROM annual_fees WHERE member_id=? AND year=?",
                (mid, year),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO annual_fees
                   (member_id, year, amount, status, paid_date, deadline, notes)
                   VALUES (?, ?, ?, 'Paid', ?, ?, 'Migrated')""",
                (mid, year, amount, paid_date, f'{year}-06-30'),
            )

        # --- 7. Fines ---
        for member_name, fine_date, violation, amount in FINES:
            mid = member_ids.get(member_name)
            if not mid:
                continue
            existing = conn.execute(
                """SELECT 1 FROM fines
                   WHERE member_id=? AND fine_date=? AND violation=?""",
                (mid, fine_date, violation),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO fines
                   (member_id, fine_date, violation, amount, status, notes)
                   VALUES (?, ?, ?, ?, 'Unpaid', 'Migrated')""",
                (mid, fine_date, violation, amount),
            )

        # --- 8. Sample minutes ---
        chairman_id = member_ids.get('SSEMPALA IVAN')
        all_active_ids = [str(member_ids[m[0]]) for m in MEMBERS
                          if m[0] not in INACTIVE_MEMBERS]
        for entry in SAMPLE_MINUTES:
            existing = conn.execute(
                "SELECT 1 FROM minutes WHERE meeting_date=? AND title=?",
                (entry['meeting_date'], entry['title']),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO minutes
                   (meeting_date, title, venue, chaired_by,
                    attendance, agenda, discussion, resolutions, is_published)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (entry['meeting_date'], entry['title'], entry['venue'],
                 chairman_id, ','.join(all_active_ids),
                 entry['agenda'], entry['discussion'], entry['resolutions']),
            )

        # --- 9. Settings (defaults) ---
        defaults = [
            ('club_name', Config.CLUB_NAME, 'Display name of the club'),
            ('monthly_savings', str(Config.MONTHLY_SAVINGS_AMOUNT),
             'Mandatory monthly savings amount in UGX'),
            ('loan_interest_rate', str(Config.LOAN_INTEREST_RATE_MONTHLY),
             'Monthly loan interest rate'),
            ('loan_max_term', str(Config.LOAN_MAX_TERM_MONTHS),
             'Maximum loan term in months'),
            ('top_saver_award', str(Config.TOP_SAVER_AWARD),
             'Yearly award given to the top saver'),
            ('annual_fee', str(Config.ANNUAL_FEE),
             'Annual membership fee in UGX'),
        ]
        for k, v, d in defaults:
            existing = conn.execute(
                "SELECT 1 FROM settings WHERE key=?", (k,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                (k, v, d),
            )

    print(f"  ✓ Database seeded successfully: {db_path}")


if __name__ == '__main__':
    db_path = Config.DATABASE_PATH
    if '--reset' in sys.argv and os.path.exists(db_path):
        os.remove(db_path)
        print(f"  ! Existing database removed: {db_path}")

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    init_schema(db_path)
    seed(db_path)
    print()
    print("=" * 60)
    print("DEFAULT LOGIN CREDENTIALS")
    print("=" * 60)
    print("All members have been given a login.")
    print("Username: their first name (lowercase)  e.g. ssekitto, ssempala")
    print(f"Password: {DEFAULT_PASSWORD}")
    print("All users will be prompted to change password on first login.")
    print("=" * 60)
    print()
    print("Key admin accounts:")
    print("  ssekitto    -> IT_ADMIN  (SSEKITTO CHRISTOPHER)")
    print("  ssempala    -> CHAIRMAN  (SSEMPALA IVAN)")
    print("  nsobya      -> SECRETARY (NSOBYA ARNOLD)")
    print("  kalangwa    -> TREASURER (KALANGWA ROBERT)")
    print("  kasule      -> COMMITTEE (KASULE JOASH)")
    print("  kironde     -> COMMITTEE (KIRONDE TONNY)")
    print()
