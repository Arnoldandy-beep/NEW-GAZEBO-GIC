"""
GAZEBO Investment Club - Authentication routes
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, current_app
)
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta
from urllib.parse import urlparse
import re
import threading

from database import get_db
from utils import login_required, log_action
from config import Config

bp = Blueprint('auth', __name__, url_prefix='/auth')

# --- Simple in-process brute-force guard ---
_FAIL_LIMIT = 10          # max failures before lockout
_LOCKOUT_SECONDS = 15 * 60  # 15-minute lockout window
_fail_lock = threading.Lock()
_fail_counts: dict = {}   # ip -> [attempt_count, first_fail_datetime]


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login, False if locked out."""
    with _fail_lock:
        entry = _fail_counts.get(ip)
        if not entry:
            return True
        count, first_fail = entry
        if datetime.utcnow() - first_fail > timedelta(seconds=_LOCKOUT_SECONDS):
            del _fail_counts[ip]
            return True
        return count < _FAIL_LIMIT


def _record_fail(ip: str):
    with _fail_lock:
        entry = _fail_counts.get(ip)
        if not entry or (datetime.utcnow() - entry[1] > timedelta(seconds=_LOCKOUT_SECONDS)):
            _fail_counts[ip] = [1, datetime.utcnow()]
        else:
            _fail_counts[ip][0] += 1


def _clear_fail(ip: str):
    with _fail_lock:
        _fail_counts.pop(ip, None)


def _log_failed_login_direct(db, user_id, username, ip, user_agent):
    """Write a LOGIN_FAILED audit entry directly (no session context yet)."""
    try:
        db.execute(
            """INSERT INTO audit_log
               (user_id, action, entity_type, entity_id, description,
                ip_address, actor_username, user_agent)
               VALUES (?, 'LOGIN_FAILED', 'user', ?, ?, ?, ?, ?)""",
            (
                user_id, user_id,
                f"Failed login attempt for username: {username}",
                ip, username, user_agent,
            ),
        )
        db.commit()
    except Exception as e:
        print(f"[audit_log] LOGIN_FAILED write error: {e}")


def _is_safe_redirect_url(url):
    """Allow only relative paths — no scheme, no host (prevents open redirect)."""
    if not url:
        return False
    parsed = urlparse(url)
    return not parsed.scheme and not parsed.netloc and url.startswith('/')


def _password_complexity_error(password):
    if len(password or '') < 8:
        return 'New password must be at least 8 characters.'
    if not re.search(r'[A-Z]', password or ''):
        return 'New password must include at least one uppercase letter.'
    if not re.search(r'[a-z]', password or ''):
        return 'New password must include at least one lowercase letter.'
    if not re.search(r'\d', password or ''):
        return 'New password must include at least one number.'
    return None


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        if Config.ROLES.get(session.get('role'), {}).get('admin'):
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('member.dashboard'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip().lower()
        password = request.form.get('password') or ''
        next_url = request.form.get('next') or request.args.get('next')
        client_ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        user_agent = (request.headers.get('User-Agent') or '')[:512]

        if not _check_rate_limit(client_ip):
            flash('Too many failed login attempts from your network. Please wait 15 minutes before trying again.', 'danger')
            return render_template('auth/login.html', next=next_url)

        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('auth/login.html', next=next_url)

        db = get_db()

        # Lookup user — active accounts only
        user = db.execute(
            """SELECT u.*, m.full_name, m.role, m.member_no, m.photo_url, m.id AS m_id
                 FROM users u
                 JOIN members m ON m.id = u.member_id
                WHERE LOWER(u.username) = ? AND u.is_active = 1""",
            (username,),
        ).fetchone()

        # Allow login with member number (e.g. GIC-006)
        if not user and username.startswith('gic-'):
            user = db.execute(
                """SELECT u.*, m.full_name, m.role, m.member_no, m.photo_url, m.id AS m_id
                     FROM users u
                     JOIN members m ON m.id = u.member_id
                    WHERE LOWER(m.member_no) = ? AND u.is_active = 1
                    LIMIT 1""",
                (username,),
            ).fetchone()
            is_member_login = bool(user)
        else:
            is_member_login = False

        # ── Username not found ─────────────────────────────────────────────
        if not user:
            _record_fail(client_ip)
            _log_failed_login_direct(db, None, username, client_ip, user_agent)
            flash('Invalid username or password.', 'danger')
            return render_template('auth/login.html', next=next_url)

        # ── Account lockout check (IT_ADMIN is never locked) ──────────────
        if user['role'] != 'IT_ADMIN':
            locked_until_raw = user['locked_until'] if 'locked_until' in user.keys() else None
            if locked_until_raw:
                try:
                    if isinstance(locked_until_raw, str):
                        try:
                            lu = datetime.strptime(locked_until_raw, '%Y-%m-%d %H:%M:%S.%f')
                        except ValueError:
                            lu = datetime.strptime(locked_until_raw, '%Y-%m-%d %H:%M:%S')
                    else:
                        lu = locked_until_raw
                    if lu > datetime.utcnow():
                        remaining = max(1, int((lu - datetime.utcnow()).total_seconds() / 60) + 1)
                        flash(
                            f'Account locked after too many failed attempts. '
                            f'Try again in {remaining} minute(s) or contact the IT Admin to unlock.',
                            'danger',
                        )
                        return render_template('auth/login.html', next=next_url)
                except Exception:
                    pass  # If date parse fails, don't block login

        # ── Password check ─────────────────────────────────────────────────
        if not check_password_hash(user['password_hash'], password):
            _record_fail(client_ip)
            _log_failed_login_direct(db, user['id'], username, client_ip, user_agent)

            # Increment DB counter and lock account if threshold reached (not IT_ADMIN)
            if user['role'] != 'IT_ADMIN':
                fail_count = (user['failed_login_count'] if 'failed_login_count' in user.keys() else 0) or 0
                fail_count += 1
                if fail_count >= 5:
                    lock_until = datetime.utcnow() + timedelta(minutes=30)
                    db.execute(
                        "UPDATE users SET failed_login_count=?, locked_until=? WHERE id=?",
                        (fail_count, lock_until, user['id']),
                    )
                    db.commit()
                    flash(
                        f'Account locked after {fail_count} failed attempts. '
                        'Contact the IT Admin to unlock your account.',
                        'danger',
                    )
                else:
                    remaining_attempts = 5 - fail_count
                    db.execute(
                        "UPDATE users SET failed_login_count=? WHERE id=?",
                        (fail_count, user['id']),
                    )
                    db.commit()
                    flash(
                        f'Invalid username or password. '
                        f'{remaining_attempts} attempt(s) remaining before account lockout.',
                        'danger',
                    )
            else:
                flash('Invalid username or password.', 'danger')
            return render_template('auth/login.html', next=next_url)

        # ── Correct password — clear any lockout state ─────────────────────
        db.execute(
            "UPDATE users SET failed_login_count=0, locked_until=NULL WHERE id=?",
            (user['id'],),
        )
        _clear_fail(client_ip)

        # Set up session
        session.clear()
        session['user_id']    = user['id']
        session['member_id']  = user['m_id']
        session['member_no']  = user['member_no']
        session['username']   = user['username']
        session['full_name']  = user['full_name']
        session['photo_url']  = user['photo_url']
        session['role']       = user['role']
        session['must_change_pw'] = bool(user['must_change_pw'])
        session['login_type'] = 'member' if is_member_login else 'admin'
        session['last_activity_at'] = int(datetime.utcnow().timestamp())
        session.permanent = False

        # Last login timestamp
        db.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.utcnow(), user['id']),
        )
        db.commit()
        log_action('LOGIN', 'user', user['id'], f"{username} logged in ({'member mode' if is_member_login else 'admin mode'})")

        flash(f"Welcome back, {user['full_name']}.", 'success')

        if user['must_change_pw']:
            flash('Password update required before system access. Use at least 8 characters with uppercase, lowercase, and a number.', 'warning')
            return redirect(url_for('auth.change_password'))
        if next_url and _is_safe_redirect_url(next_url):
            return redirect(next_url)
        
        # Route based on login type and role
        if is_member_login:
            # Member number login: always go to member portal (even for admins)
            return redirect(url_for('member.dashboard'))
        elif Config.ROLES.get(user['role'], {}).get('admin'):
            # Admin username login: go to admin dashboard
            return redirect(url_for('admin.dashboard'))
        else:
            # Regular member login: go to member dashboard
            return redirect(url_for('member.dashboard'))

    return render_template('auth/login.html')


@bp.route('/logout')
@login_required
def logout():
    log_action('LOGOUT', 'user', session.get('user_id'),
               f"{session.get('username')} logged out")
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw = request.form.get('current_password') or ''
        new_pw = request.form.get('new_password') or ''
        confirm = request.form.get('confirm_password') or ''

        complexity_error = _password_complexity_error(new_pw)
        if complexity_error:
            flash(complexity_error, 'danger')
            return render_template('auth/change_password.html')
        if new_pw != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/change_password.html')

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE id = ?",
            (session['user_id'],),
        ).fetchone()
        if not check_password_hash(user['password_hash'], current_pw):
            flash('Current password is incorrect.', 'danger')
            return render_template('auth/change_password.html')

        db.execute(
            """UPDATE users
                  SET password_hash = ?, must_change_pw = 0
                WHERE id = ?""",
            (generate_password_hash(new_pw), session['user_id']),
        )
        db.commit()
        session['must_change_pw'] = False
        log_action('CHANGE_PASSWORD', 'user', session['user_id'],
                   'Password changed by user')
        flash('Password changed successfully.', 'success')

        if Config.ROLES.get(session.get('role'), {}).get('admin'):
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('member.dashboard'))

    return render_template('auth/change_password.html')
