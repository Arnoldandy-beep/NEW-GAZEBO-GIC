"""
GAZEBO Investment Club - Main Flask application
Run with: python app.py
"""
import os
from flask import Flask, redirect, url_for, session, render_template, request, flash
from datetime import datetime, date

from config import Config
from database import close_db, init_schema, get_db
from utils import fmt_money, period_label
from auth import bp as auth_bp
from admin_routes import bp as admin_bp
from member_routes import bp as member_bp


def create_app():
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(Config)

    # Ensure data directory exists
    os.makedirs(os.path.dirname(app.config['DATABASE_PATH']), exist_ok=True)

    # Initialize schema if missing
    if not os.path.exists(app.config['DATABASE_PATH']):
        init_schema(app.config['DATABASE_PATH'])
        # Seed initial data
        from init_db import seed
        seed(app.config['DATABASE_PATH'])
    else:
        # Apply schema (idempotent)
        init_schema(app.config['DATABASE_PATH'])

    app.teardown_appcontext(close_db)

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(member_bp)

    # ----- Jinja filters & globals -----
    app.jinja_env.filters['money']        = fmt_money
    app.jinja_env.filters['period_label'] = period_label

    @app.template_filter('datefmt')
    def _datefmt(value, fmt='%d %b %Y'):
        if not value:
            return ''
        if isinstance(value, str):
            try:
                value = datetime.strptime(value[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                return value
        return value.strftime(fmt)

    @app.template_filter('datetimefmt')
    def _datetimefmt(value, fmt='%d %b %Y %H:%M'):
        if not value:
            return ''
        if isinstance(value, str):
            try:
                value = datetime.strptime(value[:19],
                                          '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                return value
        return value.strftime(fmt)

    @app.context_processor
    def inject_globals():
        pending_expenses_count = 0
        pending_approvals_count = 0
        unread_notifications_count = 0
        if session.get('user_id'):
            try:
                db = get_db()
                role = session.get('role')
                uid = session.get('user_id')
                if role in ('CHAIRMAN', 'IT_ADMIN'):
                    pending_expenses_count = db.execute(
                        "SELECT COUNT(*) c FROM expenses WHERE status='Pending'"
                    ).fetchone()['c']
                if role in ('IT_ADMIN', 'CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE'):
                    cnt = 0
                    if role in ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE', 'IT_ADMIN'):
                        cnt += db.execute(
                            """SELECT COUNT(*) c FROM loan_amendments la
                               WHERE la.status='Pending' AND la.requested_by!=?
                               AND NOT EXISTS (
                                   SELECT 1 FROM loan_approvals lax
                                   WHERE lax.loan_id=la.loan_id AND lax.amendment_id=la.id
                                     AND lax.stage='AMENDMENT' AND lax.user_id=?)""",
                            (uid, uid)
                        ).fetchone()['c']
                    if role in ('CHAIRMAN', 'IT_ADMIN'):
                        cnt += db.execute(
                            "SELECT COUNT(*) c FROM expenses WHERE status='Pending'"
                        ).fetchone()['c']
                    if role in ('CHAIRMAN', 'SECRETARY', 'TREASURER', 'COMMITTEE', 'IT_ADMIN'):
                        cnt += db.execute(
                            """SELECT COUNT(*) c FROM loans l
                               WHERE l.status='Pending'
                               AND NOT EXISTS (
                                   SELECT 1 FROM loan_approvals la
                                   WHERE la.loan_id=l.id AND la.stage='NEW' AND la.user_id=?)""",
                            (uid,)
                        ).fetchone()['c']
                    if role in ('TREASURER', 'IT_ADMIN'):
                        cnt += db.execute(
                            """SELECT COUNT(*) c
                                 FROM member_profile_change_requests r
                                WHERE r.status='Pending' AND r.request_type='NEXT_OF_KIN'"""
                        ).fetchone()['c']
                    pending_approvals_count = cnt
                    unread_notifications_count = db.execute(
                        "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
                        (uid,)
                    ).fetchone()['c']
            except Exception:
                pass
        return dict(
            CONFIG=Config,
            today=date.today(),
            now=datetime.now(),
            ROLES=Config.ROLES,
            pending_expenses_count=pending_expenses_count,
            pending_approvals_count=pending_approvals_count,
            unread_notifications_count=unread_notifications_count,
        )

    @app.before_request
    def enforce_session_timeout():
        if request.endpoint == 'static':
            return None
        if 'user_id' not in session:
            return None

        if session.get('must_change_pw'):
            allowed = {'auth.change_password', 'auth.logout'}
            if request.endpoint not in allowed:
                flash('Change your temporary password to continue. Use at least 8 characters with uppercase, lowercase, and a number.', 'warning')
                return redirect(url_for('auth.change_password'))

        now_ts = int(datetime.utcnow().timestamp())
        last_seen = session.get('last_activity_at')
        timeout = int(app.config.get('IDLE_SESSION_TIMEOUT_SECONDS', 180))

        if last_seen is not None:
            try:
                if (now_ts - int(last_seen)) > timeout:
                    session.clear()
                    flash('Your session expired after 3 minutes of inactivity. Please sign in again.', 'warning')
                    return redirect(url_for('auth.login', next=request.path))
            except (TypeError, ValueError):
                session.clear()
                return redirect(url_for('auth.login'))

        session['last_activity_at'] = now_ts

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        if session.get('user_id'):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
        if request.is_secure:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response

    # ----- Root route -----
    @app.route('/')
    def index():
        if 'user_id' in session:
            if Config.ROLES.get(session.get('role'), {}).get('admin'):
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('member.dashboard'))
        return redirect(url_for('auth.login'))

    # ----- Error handlers -----
    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template('errors/403.html'), 403

    @app.errorhandler(500)
    def server_error(e):
        return render_template('errors/500.html'), 500

    return app


app = create_app()

_DEFAULT_SECRET = 'gazebo-gic-change-this-in-production-2026'

if Config.SECRET_KEY == _DEFAULT_SECRET:
    import warnings
    warnings.warn(
        "\n[SECURITY] SECRET_KEY is set to the insecure default value. "
        "Set the SECRET_KEY environment variable to a strong random secret before deploying.",
        stacklevel=1,
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = os.environ.get('HOST', '127.0.0.1')
    debug_mode = os.environ.get('FLASK_DEBUG', '0').strip() in ('1', 'true', 'yes')
    print("=" * 60)
    print("GAZEBO Investment Club - Management System")
    print("=" * 60)
    print(f"Database: {Config.DATABASE_PATH}")
    print(f"Visit:    http://{host}:{port}/")
    print(f"Login:    use seeded credentials (see README.md)")
    if debug_mode:
        print("WARNING:  Debug mode is ON — do not use in production!")
    print("=" * 60)
    app.run(debug=debug_mode, host=host, port=port)
