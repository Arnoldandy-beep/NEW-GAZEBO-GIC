# GAZEBO Investment Club — Management System

A complete club savings & lending management system for **GAZEBO GIC** (Kampala, Uganda).
Built with **Python 3 + Flask + SQLite**, with a clean Bootstrap-based UI and a separate file-based database.

---

## ✨ Features

### Members
- Roster of all club members (currently **17** seeded from the Excel report).
- Roles: **IT Admin, Chairman, Secretary, Treasurer, Committee Member, Member**.
- Per-member login accounts with first-time password change enforcement.
- Add new members at any time — system auto-numbers them (`GIC-018`, `GIC-019`…).

### Savings
- Mandatory **100,000 UGX** per member every month (started **August 2025**).
- Single-entry or **bulk-record** by period.
- Per-period defaulter list & compliance percentage.
- Per-member running statement with locked / available split.

### Loans
- Maximum **6-month term** at **5% simple interest per month**.
- Always due on the **30th of the maturity month** for clean interest sharing.
- **Two guarantors required**.
- **Collateral lock waterfall**: borrower's available savings locked first, any shortfall split between the two guarantors' available savings.
- Auto-validation: no self-guarantee, no member with an open loan can take another, no duplicate guarantors.
- Penalty: **5% per overdue month** applied on the 1st of each new month after due date.
- Repayments allocate **penalty → interest → principal**.
- Pending/Active/Cleared/Declined/Defaulted lifecycle.
- Full eligibility check available to members (read-only) before requesting a loan.

### Dividends (every 31st December)
- Pool = total interest + penalty collected for the year.
- **Top saver** gets a bonus **50,000 UGX**.
- Remainder split equally among all active members.
- Preview the distribution before running it; one run per year, irreversible.

### Minutes
- Recordable meeting minutes with attendance, agenda, discussion, resolutions, next meeting date.
- Members see only **published** minutes (read-only).

### Fines & Annual Fees
- Issue and track fines (paid / unpaid) with violation notes.
- 50,000 UGX annual fee per member, with deadline tracking.

### Reports
- Live balance sheet (assets, equity, receivables, operating income).
- 6-month savings trend bar chart.

### Audit Log
- Every administrative action (login, savings, loans, dividends, etc.) is recorded.
- Last 200 entries viewable.

---

## 📂 Project Structure

```
gazebo_gic/
├── app.py                # Flask application factory + entry point
├── config.py             # Settings (currency, rates, roles, etc.)
├── database.py           # SQLite schema + connection helpers
├── auth.py               # Login / logout / password change
├── admin_routes.py       # Admin (Chairman, Treasurer, etc.) blueprint
├── member_routes.py      # Member-only blueprint (read-only views)
├── utils.py              # Money, date, savings, loan calculations
├── init_db.py            # Seed initial members & data from Excel
├── requirements.txt
├── data/
│   └── gazebo_gic.db     # SQLite database (auto-created)
├── static/
│   ├── css/style.css
│   └── js/app.js
└── templates/
    ├── base.html
    ├── auth/  admin/  member/  errors/
```

---

## 🚀 Getting Started

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

(Tested on Python 3.10+. The only dependencies are Flask, Werkzeug, and python-dateutil.)

### 2. Initialise & seed the database

```bash
python init_db.py
```

To wipe and re-seed:

```bash
python init_db.py --reset
```

### 3. Run the app

```bash
python app.py
```

Open <http://127.0.0.1:8080> in your browser.

> **Need a different port?** Set the `PORT` environment variable:
> - Windows (cmd): `set PORT=9000 && python app.py`
> - Mac/Linux: `PORT=9000 python app.py`

---

## 🔐 Default Login Credentials

All seeded members have the password **`gazebo123`**. Each user is required to change it on first login.

| Username   | Role        | Member                       |
|------------|-------------|------------------------------|
| `ssekitto` | IT Admin    | SSEKITTO CHRISTOPHER (GIC-006) |
| `ssempala` | Chairman    | SSEMPALA IVAN (GIC-001)        |
| `kalangwa` | Treasurer   | KALANGWA ROBERT (GIC-002)      |
| `nsobya`   | Secretary   | NSOBYA ARNOLD (GIC-007)        |
| `kasule`   | Committee   | KASULE JOASH (GIC-003)         |
| `kironde`  | Committee   | KIRONDE TONNY (GIC-009)        |
| `mushana`  | Member      | MUSHANA FRED (GIC-005)         |
| …          | Member      | every other seeded member uses their first name in lowercase |

The IT Admin account (`ssekitto`) is the only one that can promote any member to IT Admin.

---

## 🎛️ Two Sides of the System

### Admin Side
Anyone whose role is **IT Admin / Chairman / Secretary / Treasurer / Committee** sees the **Administration** menu (members, savings, loans, dividends, minutes, fines, annual fees, reports, audit log).

Admins can also click **"My Member View"** in the sidebar to see the member-side view of their own account.

### Member Side
All users (admin or not) have a member portal with **read-only** views:

- Dashboard (savings, locked, available, current-month status, active loans, fines, arrears, latest minutes, dividend history)
- Savings statement (full month-by-month with running totals and missing periods marked)
- My Loans (own loans + loans I'm guaranteeing)
- Loan eligibility checker (suggests guarantors, runs a full security calculation)
- Minutes (published only)
- Profile + login info

---

## 💰 Key Business Rules

- **Currency**: Ugandan Shillings (UGX). Displayed as `USh 100,000`.
- **Saving start period**: August 2025.
- **Mandatory monthly savings**: 100,000 UGX.
- **Loan interest**: 5% per month, simple, computed on principal × term.
- **Loan max term**: 6 months.
- **Loan due date**: always last day of the maturity month.
- **Penalty**: 5% per overdue month, applied on the 1st.
- **Guarantors**: 2, distinct, neither the borrower.
- **Collateral lock waterfall**: borrower's available savings first, then guarantors split the shortfall.
- **Top saver award** (annual): 50,000 UGX.
- **Annual fee**: 50,000 UGX per member.

These are configurable in `config.py`.

---

## 🛠 Useful Routes

| URL                         | Purpose                          |
|-----------------------------|----------------------------------|
| `/auth/login`               | Sign in                          |
| `/admin/`                   | Admin dashboard                  |
| `/admin/savings/bulk`       | Tick all members who paid this month |
| `/admin/loans/new`          | Issue a new loan                 |
| `/admin/loans/penalties/run`| Apply penalty for unrecorded overdue months |
| `/admin/dividends/preview/2025` | Preview the 2025 distribution |
| `/member/`                  | Member dashboard                 |
| `/member/loan-eligibility`  | Member self-service eligibility check |

---

## 🧰 Tech Notes

- All money values stored as integer UGX (no decimals).
- Periods stored as `YYYY-MM` strings (e.g. `2025-08`).
- SQLite foreign keys are enabled (`PRAGMA foreign_keys = ON`).
- All admin actions are recorded in `audit_log`.
- Passwords are hashed with Werkzeug.
- The session cookie is HttpOnly + SameSite=Lax with an 8-hour lifetime.

---

## ❓ Troubleshooting

**"Database is locked"** — close any other process holding `data/gazebo_gic.db` open.

**Reset everything:**
```bash
rm -rf data/ && python init_db.py
```

**Promote a member to IT Admin** — only the IT Admin can do this from the member edit form.

---

© 2026 GAZEBO Investment Club, Kampala, Uganda.
