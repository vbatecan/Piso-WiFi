"""Flask route blueprints for Piso-WiFi web application."""

from piso_wifi.web.routes.auth import auth_bp
from piso_wifi.web.routes.dashboard import dashboard_bp
from piso_wifi.web.routes.debug import debug_bp
from piso_wifi.web.routes.onboarding import onboarding_bp
from piso_wifi.web.routes.settings import settings_bp
from piso_wifi.web.routes.diagnostics import diagnostics_bp

__all__ = [
    "auth_bp",
    "dashboard_bp",
    "debug_bp",
    "onboarding_bp",
    "settings_bp",
    "diagnostics_bp",
]

