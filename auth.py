"""
GAZEBO Investment Club - Authentication routes
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, current_app
)
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime

from database import get_db
from utils import login_required, log_action
from config import Config

bp = Blueprint('auth', __name__, url_prefix='/auth')


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

        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('auth/login.html', next=next_url)

        db = get_db()
        
        # First, try direct username match
        user = db.execute(
            """SELECT u.*, m.full_name, m.role, m.member_no, m.photo_url, m.id AS m_id
                 FROM users u
                 JOIN members m ON m.id = u.member_id
                WHERE LOWER(u.username) = ? AND u.is_active = 1""",
            (username,),
        ).fetchone()
        
        # If not found and username looks like GIC-xxx, try to find by member number
        # This allows admins to login with their member number (e.g., GIC-006)
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

        if not user or not check_password_hash(user['password_hash'], password):
            flash('Invalid username or password.', 'danger')
            return render_template('auth/login.html', next=next_url)

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
        session.permanent = True

        # Last login timestamp
        db.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.utcnow(), user['id']),
        )
        db.commit()
        log_action('LOGIN', 'user', user['id'], f"{username} logged in ({'member mode' if is_member_login else 'admin mode'})")

        flash(f"Welcome back, {user['full_name']}.", 'success')

        if user['must_change_pw']:
            return redirect(url_for('auth.change_password'))
        if next_url:
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

        if len(new_pw) < 6:
            flash('New password must be at least 6 characters.', 'danger')
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
