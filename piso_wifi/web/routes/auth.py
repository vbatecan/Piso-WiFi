"""Authentication routes for Piso-WiFi web application."""

import hmac
import logging
import os
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from piso_wifi.security.rate_limiter import LoginRateLimiter

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def _get_admin_credentials():
    """Retrieve configured admin username and password."""
    app_config = current_app.config.get("CONFIG")
    if app_config is not None:
        username = getattr(app_config, "admin_username", None)
        password = getattr(app_config, "admin_password", None)
        if username and password:
            return username, password

    username = current_app.config.get("ADMIN_USERNAME", os.getenv("ADMIN_USERNAME", "admin"))
    password = current_app.config.get("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", "admin123"))
    return username, password


def _get_rate_limiter() -> LoginRateLimiter:
    """Retrieve or initialize application rate limiter."""
    limiter = current_app.config.get("LOGIN_RATE_LIMITER")
    if limiter is None:
        limiter = LoginRateLimiter(max_attempts=5, window_seconds=900)
        current_app.config["LOGIN_RATE_LIMITER"] = limiter
    return limiter


def _get_client_ip() -> str:
    """Extract client IP handling proxies."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate administrator credentials with rate limiting and constant-time comparison."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        client_ip = _get_client_ip()
        rate_limiter = _get_rate_limiter()

        # Check if client IP or username is locked out
        is_locked, remaining_seconds = rate_limiter.is_login_locked(client_ip, username)
        if is_locked:
            flash(
                f"Too many failed login attempts. Locked out for {int(remaining_seconds)} seconds.",
                "error",
            )
            return render_template("login.html"), 429

        admin_username, admin_password = _get_admin_credentials()

        # Secure constant-time comparison
        valid_username = hmac.compare_digest(
            username.encode("utf-8"), (admin_username or "").encode("utf-8")
        )
        valid_password = hmac.compare_digest(
            password.encode("utf-8"), (admin_password or "").encode("utf-8")
        )

        if valid_username and valid_password:
            rate_limiter.record_login_attempt(client_ip, username, success=True)
            session["is_admin"] = True
            flash("Logged in successfully", "success")
            try:
                return redirect(url_for("dashboard.index"))
            except Exception:
                return redirect(url_for("index"))
        else:
            rate_limiter.record_login_attempt(client_ip, username, success=False)
            flash("Invalid credentials", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    """Clear administrator session and redirect to dashboard."""
    session.pop("is_admin", None)
    flash("Logged out successfully", "success")
    try:
        return redirect(url_for("dashboard.index"))
    except Exception:
        return redirect(url_for("index"))
