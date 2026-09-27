"""Onboarding and initial setup wizard routes for Piso-WiFi."""

import logging
import os
import re
from typing import Any, Dict
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from piso_wifi.config import AppConfig, save_env_file
from piso_wifi.services.system_service import SystemService

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__)


def _get_system_service() -> SystemService:
    """Retrieve injected or default SystemService."""
    sys_svc = current_app.config.get("SYSTEM_SERVICE")
    if sys_svc is None:
        sys_svc = SystemService()
        current_app.config["SYSTEM_SERVICE"] = sys_svc
    return sys_svc


def _is_ip_valid(ip_str: str) -> bool:
    """Validate IPv4 address format."""
    parts = ip_str.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        try:
            val = int(p)
            if val < 0 or val > 255:
                return False
        except ValueError:
            return False
    return True


@onboarding_bp.route("/onboarding", methods=["GET"])
def index():
    """Render the multi-step Onboarding Setup Wizard."""
    app_config = current_app.config.get("CONFIG") or AppConfig()

    # If setup is already completed and user is not an admin, redirect to main portal
    if app_config.setup_completed and not session.get("is_admin"):
        flash("Setup has already been completed.", "info")
        return redirect(url_for("dashboard.index"))

    sys_svc = _get_system_service()
    interfaces = [i.to_dict() for i in sys_svc.list_interfaces()]
    prereqs = sys_svc.check_prerequisites()

    return render_template(
        "onboarding.html",
        config=app_config,
        interfaces=interfaces,
        prereqs=prereqs,
        is_reconfigure=app_config.setup_completed,
    )


@onboarding_bp.route("/api/onboarding/preflight", methods=["GET"])
def preflight_check():
    """API endpoint to run and return system prerequisite checks."""
    sys_svc = _get_system_service()
    return jsonify(sys_svc.check_prerequisites())


@onboarding_bp.route("/api/onboarding/interfaces", methods=["GET"])
def list_interfaces():
    """API endpoint returning detected network interfaces and AP capability."""
    sys_svc = _get_system_service()
    return jsonify([i.to_dict() for i in sys_svc.list_interfaces()])


@onboarding_bp.route("/api/onboarding/diagnostics/ping", methods=["POST"])
def run_ping_test():
    """API endpoint running live ping diagnostics during setup."""
    sys_svc = _get_system_service()
    data = request.get_json(silent=True) or request.form
    target = data.get("target", "8.8.8.8")
    count = int(data.get("count", 3))
    result = sys_svc.run_ping(target=target, count=count)
    return jsonify(result)


@onboarding_bp.route("/api/onboarding/diagnostics/dns", methods=["POST"])
def run_dns_test():
    """API endpoint running live DNS diagnostics during setup."""
    sys_svc = _get_system_service()
    data = request.get_json(silent=True) or request.form
    domain = data.get("domain", "google.com")
    result = sys_svc.run_dns_lookup(domain=domain)
    return jsonify(result)


@onboarding_bp.route("/onboarding/complete", methods=["POST"])
def complete_setup():
    """Validate submitted setup data, persist configurations, initialize services, and launch."""
    is_json = request.is_json
    data: Dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()

    # 1. Extract values with sensible defaults
    wifi_iface = data.get("wifi_interface", "wlan0").strip()
    internet_iface = data.get("internet_interface", "auto").strip() or "auto"
    ssid = data.get("ssid", "PisoWiFi").strip()
    password = data.get("password", "pisowifi123").strip()
    ap_ip = data.get("ap_ip", "192.168.4.1").strip()
    network_mask = data.get("network_mask", "255.255.255.0").strip()
    dhcp_start = data.get("dhcp_start", "192.168.4.2").strip()
    dhcp_end = data.get("dhcp_end", "192.168.4.50").strip()

    # Rate and Bandwidth
    rate_minutes = float(data.get("minutes_per_peso", 5.0) or 5.0)
    rate_pesos_per_min = round(1.0 / rate_minutes, 4) if rate_minutes > 0 else 0.2

    default_down = int(data.get("default_download_kbps", 2048) or 2048)
    default_up = int(data.get("default_upload_kbps", 1024) or 1024)
    premium_down = int(data.get("premium_download_kbps", 8096) or 8096)
    premium_up = int(data.get("premium_upload_kbps", 8096) or 8096)

    # Coin Slot Settings
    coin_slot_enabled = str(data.get("coin_slot_enabled", "true")).lower() in ("1", "true", "yes", "on")
    coin_slot_mode = data.get("coin_slot_mode", "simulated").strip().lower()
    coin_board_preset = data.get("coin_board_preset", "raspberry_pi").strip().lower()
    try:
        coin_signal_pin = int(data.get("coin_signal_pin", 18) or 18)
    except (ValueError, TypeError):
        coin_signal_pin = 18

    coin_relay_pin_raw = data.get("coin_relay_pin", "")
    try:
        coin_relay_pin = int(coin_relay_pin_raw) if coin_relay_pin_raw and str(coin_relay_pin_raw) != "0" else None
    except (ValueError, TypeError):
        coin_relay_pin = None

    try:
        pulses_per_peso = int(data.get("pulses_per_peso", 1) or 1)
    except (ValueError, TypeError):
        pulses_per_peso = 1

    # Admin Credentials
    admin_user = data.get("admin_username", "admin").strip()
    admin_pass = data.get("admin_password", "").strip()
    admin_pass_confirm = data.get("admin_password_confirm", "").strip()

    errors = []
    if not wifi_iface:
        errors.append("WiFi AP interface selection is required.")
    if not ssid:
        errors.append("Hotspot SSID (WiFi Name) is required.")
    if not _is_ip_valid(ap_ip):
        errors.append(f"Invalid AP IP address: {ap_ip}")
    if not _is_ip_valid(dhcp_start) or not _is_ip_valid(dhcp_end):
        errors.append("Invalid DHCP IP range.")
    if not admin_user:
        errors.append("Admin username is required.")
    if not admin_pass:
        errors.append("Admin password is required.")
    elif len(admin_pass) < 4:
        errors.append("Admin password must be at least 4 characters long.")
    elif admin_pass != admin_pass_confirm:
        errors.append("Admin password confirmation does not match.")

    if errors:
        if is_json:
            return jsonify({"success": False, "errors": errors}), 400
        for err in errors:
            flash(err, "error")
        return redirect(url_for("onboarding.index"))

    # 2. Persist configurations into .env
    env_updates = {
        "WIFI_INTERFACE": wifi_iface,
        "INTERNET_INTERFACE": internet_iface,
        "AP_SSID": ssid,
        "AP_PASSWORD": password,
        "AP_IP": ap_ip,
        "NETWORK_MASK": network_mask,
        "DHCP_RANGE_START": dhcp_start,
        "DHCP_RANGE_END": dhcp_end,
        "RATE_PESOS_PER_MINUTE": str(rate_pesos_per_min),
        "ADMIN_USERNAME": admin_user,
        "ADMIN_PASSWORD": admin_pass,
        "DEFAULT_DOWNLOAD_SPEED": str(default_down),
        "DEFAULT_UPLOAD_SPEED": str(default_up),
        "PREMIUM_DOWNLOAD_SPEED": str(premium_down),
        "PREMIUM_UPLOAD_SPEED": str(premium_up),
        "COIN_SLOT_ENABLED": "true" if coin_slot_enabled else "false",
        "COIN_SLOT_MODE": coin_slot_mode,
        "COIN_BOARD_PRESET": coin_board_preset,
        "COIN_SIGNAL_PIN": str(coin_signal_pin),
        "COIN_RELAY_PIN": str(coin_relay_pin or 0),
        "PULSES_PER_PESO": str(pulses_per_peso),
        "SETUP_COMPLETED": "true",
    }
    save_env_file(env_updates)

    # 3. Update in-memory configuration objects
    app_config = current_app.config.get("CONFIG")
    if app_config is not None:
        app_config.admin_username = admin_user
        app_config.admin_password = admin_pass
        app_config.minutes_per_peso = rate_minutes
        app_config.setup_completed = True

        app_config.network.ap_interface = wifi_iface
        app_config.network.internet_interface = internet_iface
        app_config.network.ssid = ssid
        app_config.network.password = password
        app_config.network.ip = ap_ip
        app_config.network.network_mask = network_mask
        app_config.network.dhcp_start = dhcp_start
        app_config.network.dhcp_end = dhcp_end

        app_config.network.bandwidth.default_download_kbps = default_down
        app_config.network.bandwidth.default_upload_kbps = default_up
        app_config.network.bandwidth.premium_download_kbps = premium_down
        app_config.network.bandwidth.premium_upload_kbps = premium_up

        if hasattr(app_config, "coin_slot"):
            app_config.coin_slot.enabled = coin_slot_enabled
            app_config.coin_slot.mode = coin_slot_mode
            app_config.coin_slot.board_preset = coin_board_preset
            app_config.coin_slot.signal_pin = coin_signal_pin
            app_config.coin_slot.relay_pin = coin_relay_pin
            app_config.coin_slot.pulses_per_peso = pulses_per_peso

    coin_service = current_app.config.get("COIN_SERVICE")
    if coin_service is not None and app_config is not None:
        coin_service.config = app_config.coin_slot
        coin_service.minutes_per_peso = rate_minutes


    current_app.config["ADMIN_USERNAME"] = admin_user
    current_app.config["ADMIN_PASSWORD"] = admin_pass

    # 4. Initialize Database
    try:
        user_service = current_app.config.get("USER_SERVICE")
        if user_service and hasattr(user_service, "db"):
            from piso_wifi.database.repository import UserRepository
            repo = UserRepository(db_manager=user_service.db)
            repo.init_db()
        elif user_service and hasattr(user_service, "init_db"):
            user_service.init_db()
        logger.info("Database schema verified/initialized during onboarding")
    except Exception as e:
        logger.warning("Database initialization during onboarding: %s", e)

    # 5. Apply Hostapd and Dnsmasq files if NetworkController is available
    network_controller = current_app.config.get("NETWORK_CONTROLLER")
    if network_controller is not None:
        try:
            if hasattr(network_controller, "access_point"):
                network_controller.access_point.configure_ap(app_config.network)
            elif hasattr(network_controller, "_configure_ap"):
                network_controller._configure_ap()
        except Exception as e:
            logger.warning("AP configuration writing deferred: %s", e)

    # 6. Authenticate session as Admin immediately
    session["is_admin"] = True
    flash("🎉 Piso-WiFi setup completed successfully! Welcome to your administration dashboard.", "success")

    if is_json:
        return jsonify({
            "success": True,
            "message": "Setup completed successfully",
            "redirect_url": url_for("dashboard.index"),
        })

    return redirect(url_for("dashboard.index"))
