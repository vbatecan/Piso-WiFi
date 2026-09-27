"""Flask application factory for Piso-WiFi."""

import logging
import os
from typing import Any, Optional
from flask import Flask, redirect, request, url_for

from piso_wifi.config import AppConfig
from piso_wifi.web.routes.auth import auth_bp
from piso_wifi.web.routes.dashboard import dashboard_bp
from piso_wifi.web.routes.debug import debug_bp
from piso_wifi.web.routes.onboarding import onboarding_bp
from piso_wifi.web.routes.settings import settings_bp
from piso_wifi.web.routes.diagnostics import diagnostics_bp
from piso_wifi.web.routes.reports import reports_bp

logger = logging.getLogger(__name__)


def _register_endpoint_aliases(app: Flask) -> None:
    """Register unprefixed endpoint aliases in url_map for seamless template compatibility."""
    for rule in list(app.url_map.iter_rules()):
        if "." in rule.endpoint:
            short_endpoint = rule.endpoint.split(".", 1)[1]
            if short_endpoint not in app.view_functions:
                app.add_url_rule(
                    rule.rule,
                    endpoint=short_endpoint,
                    view_func=app.view_functions[rule.endpoint],
                    methods=rule.methods,
                )


def create_app(
    config: Optional[AppConfig] = None,
    user_service: Optional[Any] = None,
    network_controller: Optional[Any] = None,
    system_service: Optional[Any] = None,
    coin_service: Optional[Any] = None,
    buzzer_service: Optional[Any] = None,
    display_service: Optional[Any] = None,
    backup_service: Optional[Any] = None,
    analytics_service: Optional[Any] = None,
    notification_service: Optional[Any] = None,
    session_recovery_service: Optional[Any] = None,
) -> Flask:
    """Create and configure the Piso-WiFi Flask web application.

    Args:
        config: Application configuration. Defaults to AppConfig().
        user_service: UserService instance. If None, instantiates default UserService.
        network_controller: NetworkController instance. If None, instantiates NetworkController(auto_start=False).
        system_service: SystemService instance. If None, instantiates default SystemService.
        coin_service: CoinSlotService instance. If None, instantiates default CoinSlotService.
        buzzer_service: BuzzerService instance for audio feedback.
        display_service: DisplayService instance for LCD/OLED kiosk rendering.
        backup_service: BackupService instance for automated SQLite/env archives.
        analytics_service: AnalyticsService instance for sales and BI reporting.
        notification_service: NotificationService instance for Telegram/Discord webhooks.
        session_recovery_service: SessionRecoveryService instance for randomized MAC token persistence.

    Returns:
        Flask: Configured Flask application instance.
    """
    if config is None:
        config = AppConfig()

    # Determine templates and static directories from project root
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    template_folder = os.path.join(root_dir, "templates")
    static_folder = os.path.join(root_dir, "static")

    app = Flask(
        __name__,
        template_folder=template_folder,
        static_folder=static_folder,
    )

    # Configure application secrets and settings
    app.secret_key = config.secret_key
    app.config["CONFIG"] = config
    app.config["APP_CONFIG"] = config
    app.config["ADMIN_USERNAME"] = config.admin_username
    app.config["ADMIN_PASSWORD"] = config.admin_password

    # Lazy-initialize services if not provided
    if user_service is None:
        try:
            from piso_wifi.services.user_service import UserService
            user_service = UserService(config=config)
        except Exception as e:
            logger.warning(f"Default UserService instantiation deferred or failed: {e}")

    if network_controller is None:
        try:
            from piso_wifi.network.controller import NetworkController
            network_controller = NetworkController(
                config=config.network,
                auto_start=False,
                skip_system_checks=True,
            )
        except Exception as e:
            logger.warning(f"Default NetworkController instantiation deferred or failed: {e}")

    if system_service is None:
        try:
            from piso_wifi.services.system_service import SystemService
            system_service = SystemService()
        except Exception as e:
            logger.warning(f"Default SystemService instantiation deferred or failed: {e}")

    if buzzer_service is None:
        try:
            from piso_wifi.hardware.buzzer_service import BuzzerService
            buzzer_service = BuzzerService(enabled=getattr(config, "buzzer_enabled", False))
        except Exception as e:
            logger.debug(f"Default BuzzerService initialization deferred: {e}")

    if display_service is None:
        try:
            from piso_wifi.hardware.display_service import DisplayService
            display_service = DisplayService(enabled=getattr(config, "display_enabled", False))
        except Exception as e:
            logger.debug(f"Default DisplayService initialization deferred: {e}")

    if backup_service is None:
        try:
            from piso_wifi.services.backup_service import BackupService
            backup_service = BackupService(db_path=config.db_path)
        except Exception as e:
            logger.debug(f"Default BackupService initialization deferred: {e}")

    if analytics_service is None:
        try:
            from piso_wifi.services.analytics_service import AnalyticsService
            analytics_service = AnalyticsService(db_path=config.db_path)
        except Exception as e:
            logger.debug(f"Default AnalyticsService initialization deferred: {e}")

    if notification_service is None:
        try:
            from piso_wifi.services.notification_service import NotificationService
            notification_service = NotificationService()
        except Exception as e:
            logger.debug(f"Default NotificationService initialization deferred: {e}")

    if session_recovery_service is None:
        try:
            from piso_wifi.services.session_recovery import SessionRecoveryService
            session_recovery_service = SessionRecoveryService(db_path=config.db_path)
        except Exception as e:
            logger.debug(f"Default SessionRecoveryService initialization deferred: {e}")

    if coin_service is None:
        try:
            from piso_wifi.services.coin_service import CoinSlotService
            coin_service = CoinSlotService(
                config=config.coin_slot,
                user_service=user_service,
                minutes_per_peso=config.minutes_per_peso,
                buzzer_service=buzzer_service,
                display_service=display_service,
            )
        except Exception as e:
            logger.warning(f"Default CoinSlotService instantiation deferred or failed: {e}")

    # Inject services into Flask configuration
    app.config["USER_SERVICE"] = user_service
    app.config["NETWORK_CONTROLLER"] = network_controller
    app.config["SYSTEM_SERVICE"] = system_service
    app.config["COIN_SERVICE"] = coin_service
    app.config["BUZZER_SERVICE"] = buzzer_service
    app.config["DISPLAY_SERVICE"] = display_service
    app.config["BACKUP_SERVICE"] = backup_service
    app.config["ANALYTICS_SERVICE"] = analytics_service
    app.config["NOTIFICATION_SERVICE"] = notification_service
    app.config["SESSION_RECOVERY_SERVICE"] = session_recovery_service


    # Register blueprints
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(debug_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(reports_bp)

    # First-run onboarding interceptor
    @app.before_request
    def _check_onboarding_redirect():
        if app.config.get("TESTING") and not app.config.get("TEST_ONBOARDING_GUARD"):
            return None

        curr_config = app.config.get("CONFIG")
        if curr_config is not None and not curr_config.setup_completed:
            path = request.path
            if (
                path.startswith("/static/")
                or path.startswith("/onboarding")
                or path.startswith("/api/onboarding")
            ):
                return None
            return redirect(url_for("onboarding.index"))
        return None

    # Register endpoint aliases so both blueprint endpoints (e.g. auth.login)
    # and unprefixed endpoints (e.g. login) resolve cleanly with url_for
    _register_endpoint_aliases(app)

    return app

