"""Authentication helpers and security decorators for Piso-WiFi web application."""

from functools import wraps
from typing import Any, Callable
from flask import flash, redirect, session, url_for


def is_admin() -> bool:
    """Check if current session has admin privileges."""
    return bool(session.get("is_admin"))


def admin_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to require administrator access for view routes.

    If session['is_admin'] is not True, flashes an error message
    and redirects the user back to the dashboard index.
    """
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if not is_admin():
            flash("Admin access required", "error")
            try:
                return redirect(url_for("dashboard.index"))
            except Exception:
                return redirect(url_for("index"))
        return f(*args, **kwargs)

    return decorated_function
