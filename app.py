"""
GAZEBO Investment Club - Main Flask application
Run with: python app.py
"""
import os
from flask import Flask, redirect, url_for, session, render_template
from datetime import datetime, date

from config import Config
from database import close_db, init_schema
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
        return dict(
            CONFIG=Config,
            today=date.today(),
            now=datetime.now(),
            ROLES=Config.ROLES,
        )

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = os.environ.get('HOST', '127.0.0.1')
    print("=" * 60)
    print("GAZEBO Investment Club - Management System")
    print("=" * 60)
    print(f"Database: {Config.DATABASE_PATH}")
    print(f"Visit:    http://{host}:{port}/")
    print(f"Login:    use seeded credentials (see README.md)")
    print("=" * 60)
    app.run(debug=True, host=host, port=port)
