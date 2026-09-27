"""Settings and centralized web configuration portal for Piso-WiFi."""

import logging
import os
from typing import Any, Dict, List, Optional
from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from piso_wifi.config import AppConfig, save_env_file
from piso_wifi.services.backup_service import BackupService
from piso_wifi.services.system_service import SystemService
from piso_wifi.web.auth import admin_required

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__)


def _get_system_service() -> SystemService:
    """Retrieve injected or default SystemService."""
    sys_svc = current_app.config.get("SYSTEM_SERVICE")
    if sys_svc is None:
        sys_svc = SystemService()
        current_app.config["SYSTEM_SERVICE"] = sys_svc
    return sys_svc


def _get_backup_service() -> BackupService:
    """Retrieve injected or default BackupService."""
    backup_svc = current_app.config.get("BACKUP_SERVICE")
    if backup_svc is None:
        app_config = current_app.config.get("CONFIG") or AppConfig()
        backup_svc = BackupService(db_path=app_config.db_path)
        current_app.config["BACKUP_SERVICE"] = backup_svc
    return backup_svc


@settings_bp.route("/settings", methods=["GET"])
@admin_required
def index():
    """Render the centralized configuration portal."""
    app_config = current_app.config.get("CONFIG") or AppConfig()
    sys_svc = _get_system_service()
    interfaces = [i.to_dict() for i in sys_svc.list_interfaces()]

    backup_svc = _get_backup_service()
    backups = backup_svc.list_backups()

    user_svc = current_app.config.get("USER_SERVICE")
    vouchers = []
    if user_svc and hasattr(user_svc, "list_vouchers"):
        try:
            vouchers = user_svc.list_vouchers()
        except Exception as e:
            logger.debug("Error listing vouchers: %s", e)

    disp_svc = current_app.config.get("DISPLAY_SERVICE")
    buzz_svc = current_app.config.get("BUZZER_SERVICE")

    return render_template(
        "settings.html",
        config=app_config,
        interfaces=interfaces,
        backups=backups,
        vouchers=vouchers,
        display_service=disp_svc,
        buzzer_service=buzz_svc,
    )


@settings_bp.route("/settings/network", methods=["POST"])
@admin_required
def update_network():
    """Update wireless hotspot and network interface configurations."""
    app_config = current_app.config.get("CONFIG") or AppConfig()

    wifi_iface = request.form.get("wifi_interface", app_config.network.ap_interface).strip()
    internet_iface = request.form.get("internet_interface", app_config.network.internet_interface).strip() or "auto"
    ssid = request.form.get("ssid", app_config.network.ssid).strip()
    password = request.form.get("password", app_config.network.password).strip()
    ap_ip = request.form.get("ap_ip", app_config.network.ip).strip()
    network_mask = request.form.get("network_mask", app_config.network.network_mask).strip()
    dhcp_start = request.form.get("dhcp_start", app_config.network.dhcp_start).strip()
    dhcp_end = request.form.get("dhcp_end", app_config.network.dhcp_end).strip()

    if not wifi_iface or not ssid or not ap_ip:
        flash("WiFi interface, SSID, and AP IP are required.", "error")
        return redirect(url_for("settings.index"))

    # Persist to .env
    env_updates = {
        "WIFI_INTERFACE": wifi_iface,
        "INTERNET_INTERFACE": internet_iface,
        "AP_SSID": ssid,
        "AP_PASSWORD": password,
        "AP_IP": ap_ip,
        "NETWORK_MASK": network_mask,
        "DHCP_RANGE_START": dhcp_start,
        "DHCP_RANGE_END": dhcp_end,
    }
    save_env_file(env_updates)

    # Update in memory
    app_config.network.ap_interface = wifi_iface
    app_config.network.internet_interface = internet_iface
    app_config.network.ssid = ssid
    app_config.network.password = password
    app_config.network.ip = ap_ip
    app_config.network.network_mask = network_mask
    app_config.network.dhcp_start = dhcp_start
    app_config.network.dhcp_end = dhcp_end

    # Auto-generate updated hostapd/dnsmasq config files
    network_controller = current_app.config.get("NETWORK_CONTROLLER")
    if network_controller is not None:
        try:
            if hasattr(network_controller, "access_point"):
                network_controller.access_point.configure_ap(app_config.network)
            elif hasattr(network_controller, "_configure_ap"):
                network_controller._configure_ap()
        except Exception as e:
            logger.warning("Could not auto-write AP configs: %s", e)

    flash("Network and WiFi settings updated successfully.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/settings/rates", methods=["POST"])
@admin_required
def update_rates():
    """Update coin rates, pricing, and bandwidth tier presets."""
    app_config = current_app.config.get("CONFIG") or AppConfig()

    try:
        minutes_per_peso = float(request.form.get("minutes_per_peso", app_config.minutes_per_peso) or 5.0)
        rate_pesos_per_min = round(1.0 / minutes_per_peso, 4) if minutes_per_peso > 0 else 0.2

        default_down = int(request.form.get("default_download_kbps", 2048) or 2048)
        default_up = int(request.form.get("default_upload_kbps", 1024) or 1024)
        premium_down = int(request.form.get("premium_download_kbps", 8096) or 8096)
        premium_up = int(request.form.get("premium_upload_kbps", 8096) or 8096)

        env_updates = {
            "RATE_PESOS_PER_MINUTE": str(rate_pesos_per_min),
            "DEFAULT_DOWNLOAD_SPEED": str(default_down),
            "DEFAULT_UPLOAD_SPEED": str(default_up),
            "PREMIUM_DOWNLOAD_SPEED": str(premium_down),
            "PREMIUM_UPLOAD_SPEED": str(premium_up),
        }
        save_env_file(env_updates)

        app_config.minutes_per_peso = minutes_per_peso
        app_config.network.bandwidth.default_download_kbps = default_down
        app_config.network.bandwidth.default_upload_kbps = default_up
        app_config.network.bandwidth.premium_download_kbps = premium_down
        app_config.network.bandwidth.premium_upload_kbps = premium_up

        flash("Rates and bandwidth limits updated successfully.", "success")
    except ValueError as e:
        flash(f"Invalid numeric input for rates or bandwidth: {e}", "error")

    return redirect(url_for("settings.index"))


@settings_bp.route("/settings/coin_slot", methods=["POST"])
@admin_required
def update_coin_slot():
    """Update coin slot hardware settings, GPIO pins, and pulse timeouts."""
    app_config = current_app.config.get("CONFIG") or AppConfig()

    try:
        enabled = request.form.get("coin_slot_enabled", "off").lower() in ("1", "true", "yes", "on")
        mode = request.form.get("coin_slot_mode", "simulated").strip().lower()
        board_preset = request.form.get("coin_board_preset", "raspberry_pi").strip().lower()
        signal_pin = int(request.form.get("coin_signal_pin", 18) or 18)

        relay_pin_raw = request.form.get("coin_relay_pin", "").strip()
        relay_pin = int(relay_pin_raw) if relay_pin_raw and relay_pin_raw != "0" else None

        pulses_per_peso = int(request.form.get("pulses_per_peso", 1) or 1)
        pulse_timeout = int(request.form.get("pulse_timeout_ms", 400) or 400)
        session_timeout = int(request.form.get("session_timeout_seconds", 60) or 60)

        env_updates = {
            "COIN_SLOT_ENABLED": "true" if enabled else "false",
            "COIN_SLOT_MODE": mode,
            "COIN_BOARD_PRESET": board_preset,
            "COIN_SIGNAL_PIN": str(signal_pin),
            "COIN_RELAY_PIN": str(relay_pin or 0),
            "PULSES_PER_PESO": str(pulses_per_peso),
            "COIN_PULSE_TIMEOUT_MS": str(pulse_timeout),
            "COIN_SESSION_TIMEOUT_SECONDS": str(session_timeout),
        }
        save_env_file(env_updates)

        if hasattr(app_config, "coin_slot"):
            app_config.coin_slot.enabled = enabled
            app_config.coin_slot.mode = mode
            app_config.coin_slot.board_preset = board_preset
            app_config.coin_slot.signal_pin = signal_pin
            app_config.coin_slot.relay_pin = relay_pin
            app_config.coin_slot.pulses_per_peso = pulses_per_peso
            app_config.coin_slot.pulse_timeout_ms = pulse_timeout
            app_config.coin_slot.session_timeout_seconds = session_timeout

        coin_svc = current_app.config.get("COIN_SERVICE")
        if coin_svc is not None and hasattr(app_config, "coin_slot"):
            coin_svc.config = app_config.coin_slot

        flash("Coin slot hardware settings updated successfully.", "success")
    except ValueError as e:
        flash(f"Invalid input for coin slot settings: {e}", "error")

    return redirect(url_for("settings.index"))



@settings_bp.route("/settings/admin", methods=["POST"])
@admin_required
def update_admin():
    """Update administrator account credentials."""
    app_config = current_app.config.get("CONFIG") or AppConfig()

    username = request.form.get("admin_username", "").strip()
    password = request.form.get("admin_password", "").strip()
    password_confirm = request.form.get("admin_password_confirm", "").strip()

    if not username:
        flash("Admin username cannot be blank.", "error")
        return redirect(url_for("settings.index"))

    updates: Dict[str, Any] = {"ADMIN_USERNAME": username}
    app_config.admin_username = username
    current_app.config["ADMIN_USERNAME"] = username

    if password:
        if len(password) < 4:
            flash("New password must be at least 4 characters long.", "error")
            return redirect(url_for("settings.index"))
        if password != password_confirm:
            flash("New password confirmation does not match.", "error")
            return redirect(url_for("settings.index"))

        updates["ADMIN_PASSWORD"] = password
        app_config.admin_password = password
        current_app.config["ADMIN_PASSWORD"] = password

    save_env_file(updates)
    flash("Admin security credentials updated successfully.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/settings/apply-network", methods=["POST"])
@admin_required
def apply_network_services():
    """Reconfigure and restart WiFi Access Point and firewall services."""
    network_controller = current_app.config.get("NETWORK_CONTROLLER")
    app_config = current_app.config.get("CONFIG") or AppConfig()

    if network_controller is None:
        flash("Network controller subsystem is not available.", "error")
        return redirect(url_for("settings.index"))

    try:
        if hasattr(network_controller, "start_ap"):
            network_controller.start_ap()
        flash("Access Point and Firewall rules applied and restarted successfully.", "success")
    except Exception as e:
        logger.error("Error restarting AP services: %s", e)
        flash(f"Error applying network services: {e}", "error")

    return redirect(url_for("settings.index"))


@settings_bp.route("/settings/reset-onboarding", methods=["POST"])
@admin_required
def reset_onboarding():
    """Reset onboarding flag to re-run the initial setup wizard."""
    app_config = current_app.config.get("CONFIG") or AppConfig()
    save_env_file({"SETUP_COMPLETED": "false"})
    app_config.setup_completed = False
    flash("Setup status reset. You may now step through the onboarding wizard again.", "info")
    return redirect(url_for("onboarding.index"))


# =============================================================================
# Database & Configuration Backup Routes
# =============================================================================

@settings_bp.route("/settings/backups/create", methods=["POST"])
@admin_required
def create_backup():
    """Trigger creation of a timestamped SQLite & .env backup archive."""
    svc = _get_backup_service()
    try:
        res = svc.create_backup()
        flash(f"Backup archive '{res['filename']}' ({res['size_bytes']} bytes) created successfully.", "success")
    except Exception as e:
        logger.error("Failed creating backup: %s", e)
        flash(f"Error creating backup snapshot: {e}", "error")
    return redirect(url_for("settings.index", _anchor="tab-backups"))


@settings_bp.route("/settings/backups/download/<filename>", methods=["GET"])
@admin_required
def download_backup(filename: str):
    """Securely download a backup zip archive."""
    safe_name = secure_filename(filename)
    svc = _get_backup_service()
    target_path = os.path.join(svc.backup_dir, safe_name)
    if not os.path.exists(target_path):
        flash(f"Backup archive '{safe_name}' not found.", "error")
        return redirect(url_for("settings.index", _anchor="tab-backups"))
    return send_from_directory(svc.backup_dir, safe_name, as_attachment=True)


@settings_bp.route("/settings/backups/restore", methods=["POST"])
@admin_required
def restore_backup():
    """Restore database and configuration from a selected backup archive."""
    filename = request.form.get("filename", "").strip()
    safe_name = secure_filename(filename)
    svc = _get_backup_service()
    target_path = os.path.join(svc.backup_dir, safe_name)
    if not os.path.exists(target_path):
        flash(f"Backup archive '{safe_name}' does not exist.", "error")
        return redirect(url_for("settings.index", _anchor="tab-backups"))

    try:
        ok = svc.restore_backup(target_path)
        if ok:
            flash(f"Database and configurations successfully restored from '{safe_name}'.", "success")
        else:
            flash(f"Failed restoring from backup '{safe_name}'. Archive may be corrupted.", "error")
    except Exception as e:
        logger.error("Restore error: %s", e)
        flash(f"Error restoring backup: {e}", "error")

    return redirect(url_for("settings.index", _anchor="tab-backups"))


@settings_bp.route("/settings/backups/prune", methods=["POST"])
@admin_required
def prune_backups():
    """Prune older backups beyond retention count."""
    try:
        max_keep = int(request.form.get("max_keep", 7))
        svc = _get_backup_service()
        deleted = svc.prune_old_backups(max_keep=max_keep)
        flash(f"Pruned {deleted} older backup archive(s). Kept newest {max_keep}.", "info")
    except Exception as e:
        flash(f"Error pruning backups: {e}", "error")
    return redirect(url_for("settings.index", _anchor="tab-backups"))


@settings_bp.route("/settings/backups/delete", methods=["POST"])
@admin_required
def delete_backup():
    """Delete a specific backup archive."""
    filename = request.form.get("filename", "").strip()
    safe_name = secure_filename(filename)
    svc = _get_backup_service()
    target_path = os.path.join(svc.backup_dir, safe_name)
    if os.path.exists(target_path):
        try:
            os.remove(target_path)
            flash(f"Backup archive '{safe_name}' deleted.", "success")
        except Exception as e:
            flash(f"Failed deleting backup '{safe_name}': {e}", "error")
    else:
        flash(f"Backup archive '{safe_name}' not found.", "error")
    return redirect(url_for("settings.index", _anchor="tab-backups"))


@settings_bp.route("/api/settings/backups", methods=["GET"])
@admin_required
def api_list_backups():
    """API endpoint to list available backup archives."""
    svc = _get_backup_service()
    return jsonify({"success": True, "backups": svc.list_backups()})


# =============================================================================
# Promotional & Time Credit Voucher Routes
# =============================================================================

@settings_bp.route("/settings/vouchers/generate", methods=["POST"])
@admin_required
def generate_vouchers():
    """Generate batch or single promotional vouchers."""
    user_svc = current_app.config.get("USER_SERVICE")
    if not user_svc or not hasattr(user_svc, "create_vouchers"):
        flash("User service unavailable.", "error")
        return redirect(url_for("settings.index", _anchor="tab-vouchers"))

    try:
        count = max(1, min(100, int(request.form.get("count", 1))))
        time_minutes = float(request.form.get("time_minutes", 60.0))
        expires_days = int(request.form.get("expires_days", 30))

        codes = user_svc.create_vouchers(
            count=count, time_minutes=time_minutes, expires_days=expires_days
        )
        sample = ", ".join(codes[:5])
        suffix = "..." if len(codes) > 5 else ""
        flash(f"Generated {len(codes)} voucher(s) ({time_minutes:.0f} mins): {sample}{suffix}", "success")
    except Exception as e:
        logger.error("Failed generating vouchers: %s", e)
        flash(f"Error generating vouchers: {e}", "error")

    return redirect(url_for("settings.index", _anchor="tab-vouchers"))


@settings_bp.route("/settings/vouchers/delete", methods=["POST"])
@admin_required
def delete_voucher():
    """Delete or revoke an existing voucher."""
    code = request.form.get("code", "").strip().upper()
    user_svc = current_app.config.get("USER_SERVICE")
    if user_svc and hasattr(user_svc, "delete_voucher"):
        if user_svc.delete_voucher(code):
            flash(f"Voucher code '{code}' revoked and removed.", "success")
        else:
            flash(f"Could not delete voucher code '{code}'.", "error")
    else:
        flash("Voucher service unavailable.", "error")
    return redirect(url_for("settings.index", _anchor="tab-vouchers"))


@settings_bp.route("/api/settings/vouchers", methods=["GET"])
@admin_required
def api_list_vouchers():
    """API endpoint to list vouchers."""
    user_svc = current_app.config.get("USER_SERVICE")
    vouchers = user_svc.list_vouchers() if user_svc and hasattr(user_svc, "list_vouchers") else []
    return jsonify({
        "success": True,
        "vouchers": [
            v.to_dict() if hasattr(v, "to_dict") else {
                "code": getattr(v, "code", ""),
                "time_minutes": getattr(v, "time_minutes", 0),
                "is_used": getattr(v, "is_used", False),
                "used_by_mac": getattr(v, "used_by_mac", None),
                "created_at": str(getattr(v, "created_at", "")),
                "expires_at": str(getattr(v, "expires_at", "")),
            }
            for v in vouchers
        ]
    })


# =============================================================================
# Kiosk Hardware (Display & Buzzer) Routes
# =============================================================================

@settings_bp.route("/settings/hardware/display", methods=["POST"])
@admin_required
def update_display():
    """Update kiosk display hardware configurations."""
    enabled = request.form.get("display_enabled", "off").lower() in ("1", "true", "yes", "on")
    display_type = request.form.get("display_type", "lcd_1602").strip().lower()
    i2c_bus = int(request.form.get("i2c_bus", 1) or 1)
    addr_str = request.form.get("i2c_address", "0x27").strip()
    try:
        i2c_address = int(addr_str, 16) if addr_str.startswith("0x") else int(addr_str)
    except ValueError:
        i2c_address = 0x27

    save_env_file({
        "DISPLAY_ENABLED": "true" if enabled else "false",
        "DISPLAY_TYPE": display_type,
        "DISPLAY_I2C_BUS": str(i2c_bus),
        "DISPLAY_I2C_ADDRESS": hex(i2c_address),
    })

    disp_svc = current_app.config.get("DISPLAY_SERVICE")
    if disp_svc:
        disp_svc.enabled = enabled
        disp_svc.display_type = display_type
        disp_svc.i2c_bus = i2c_bus
        disp_svc.i2c_address = i2c_address
        try:
            disp_svc._init_driver()
        except Exception as e:
            logger.debug("Re-init display driver: %s", e)

    flash("Kiosk display settings saved successfully.", "success")
    return redirect(url_for("settings.index", _anchor="tab-peripherals"))


@settings_bp.route("/settings/hardware/buzzer", methods=["POST"])
@admin_required
def update_buzzer():
    """Update audio buzzer hardware configurations."""
    enabled = request.form.get("buzzer_enabled", "off").lower() in ("1", "true", "yes", "on")
    pin = int(request.form.get("buzzer_pin", 24) or 24)
    passive = request.form.get("buzzer_passive", "off").lower() in ("1", "true", "yes", "on")

    save_env_file({
        "BUZZER_ENABLED": "true" if enabled else "false",
        "BUZZER_PIN": str(pin),
        "BUZZER_PASSIVE": "true" if passive else "false",
    })

    buzz_svc = current_app.config.get("BUZZER_SERVICE")
    if buzz_svc:
        buzz_svc.enabled = enabled
        buzz_svc.buzzer_pin = pin
        buzz_svc.passive = passive

    flash("Buzzer audio settings saved successfully.", "success")
    return redirect(url_for("settings.index", _anchor="tab-peripherals"))


@settings_bp.route("/api/settings/hardware/test-buzzer", methods=["POST"])
@admin_required
def api_test_buzzer():
    """Trigger a live test chime on the buzzer."""
    buzz_svc = current_app.config.get("BUZZER_SERVICE")
    if buzz_svc:
        try:
            buzz_svc.beep_session_start()
            return jsonify({"success": True, "message": "Test chime sounded."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": False, "error": "Buzzer service not active."}), 400


@settings_bp.route("/api/settings/hardware/test-display", methods=["POST"])
@admin_required
def api_test_display():
    """Render a test message on the kiosk screen."""
    disp_svc = current_app.config.get("DISPLAY_SERVICE")
    if disp_svc:
        try:
            disp_svc._render("TEST DISPLAY OK", "PISO-WIFI READY", state="test")
            return jsonify({"success": True, "message": "Test screen rendered."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": False, "error": "Display service not active."}), 400
