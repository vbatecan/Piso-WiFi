"""Diagnostics dashboard and live troubleshooting routes for Piso-WiFi."""

import logging
from typing import Any, Dict
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from piso_wifi.config import AppConfig
from piso_wifi.services.system_service import SystemService
from piso_wifi.web.auth import admin_required

logger = logging.getLogger(__name__)

diagnostics_bp = Blueprint("diagnostics", __name__)


def _get_system_service() -> SystemService:
    """Retrieve injected or default SystemService."""
    sys_svc = current_app.config.get("SYSTEM_SERVICE")
    if sys_svc is None:
        sys_svc = SystemService()
        current_app.config["SYSTEM_SERVICE"] = sys_svc
    return sys_svc


@diagnostics_bp.route("/diagnostics", methods=["GET"])
@admin_required
def index():
    """Render the diagnostics and network troubleshooting dashboard."""
    app_config = current_app.config.get("CONFIG") or AppConfig()
    network_controller = current_app.config.get("NETWORK_CONTROLLER")
    sys_svc = _get_system_service()

    # Uplink status
    uplink_info: Dict[str, Any] = {}
    if network_controller is not None and hasattr(network_controller, "get_uplink_status"):
        try:
            raw_uplink = network_controller.get_uplink_status()
            if isinstance(raw_uplink, dict):
                uplink_info = raw_uplink
            elif hasattr(raw_uplink, "to_dict"):
                uplink_info = raw_uplink.to_dict()
        except Exception as e:
            logger.debug("Failed querying uplink in diagnostics: %s", e)

    interfaces = [i.to_dict() for i in sys_svc.list_interfaces()]
    services = sys_svc.get_service_statuses()
    metrics = sys_svc.get_system_metrics()
    prereqs = sys_svc.check_prerequisites()

    coin_svc = current_app.config.get("COIN_SERVICE")
    coin_status = coin_svc.get_status() if coin_svc is not None else {}

    return render_template(
        "diagnostics.html",
        config=app_config,
        uplink_info=uplink_info,
        interfaces=interfaces,
        services=services,
        metrics=metrics,
        prereqs=prereqs,
        coin_status=coin_status,
    )



@diagnostics_bp.route("/api/diagnostics/ping", methods=["POST"])
@admin_required
def ping_tool():
    """Execute on-demand ping diagnostics and return latency/loss."""
    sys_svc = _get_system_service()
    data = request.get_json(silent=True) or request.form
    target = data.get("target", "8.8.8.8")
    count = int(data.get("count", 3))
    res = sys_svc.run_ping(target=target, count=count)
    return jsonify(res)


@diagnostics_bp.route("/api/diagnostics/dns", methods=["POST"])
@admin_required
def dns_tool():
    """Execute on-demand DNS resolution diagnostics."""
    sys_svc = _get_system_service()
    data = request.get_json(silent=True) or request.form
    domain = data.get("domain", "google.com")
    res = sys_svc.run_dns_lookup(domain=domain)
    return jsonify(res)


@diagnostics_bp.route("/api/diagnostics/restart-daemon", methods=["POST"])
@admin_required
def restart_daemon():
    """Safely restart hostapd, dnsmasq, or reload firewall rules."""
    sys_svc = _get_system_service()
    data = request.get_json(silent=True) or request.form
    daemon = data.get("service", "").strip().lower()

    if daemon in ("hostapd", "dnsmasq"):
        result = sys_svc.restart_service(daemon)
        return jsonify(result)

    return jsonify({"success": False, "error": f"Unsupported service: {daemon}"}), 400


@diagnostics_bp.route("/api/diagnostics/coin/simulate", methods=["POST"])
@admin_required
def simulate_coin():
    """Simulate coin pulse insertion for diagnostics and hardware verification."""
    coin_svc = current_app.config.get("COIN_SERVICE")
    if coin_svc is None:
        return jsonify({"success": False, "error": "CoinSlotService is not initialized"}), 500

    data = request.get_json(silent=True) or request.form
    try:
        denomination = int(data.get("denomination", 1))
    except (ValueError, TypeError):
        denomination = 1

    mac_address = data.get("mac_address", "").strip() or None
    result = coin_svc.simulate_coin(denomination=denomination, mac_address=mac_address)
    return jsonify(result)


@diagnostics_bp.route("/api/diagnostics/coin/relay", methods=["POST"])
@admin_required
def toggle_coin_relay():
    """Toggle coin slot 12V power or LED relay for wiring tests."""
    coin_svc = current_app.config.get("COIN_SERVICE")
    if coin_svc is None:
        return jsonify({"success": False, "error": "CoinSlotService is not initialized"}), 500

    data = request.get_json(silent=True) or request.form
    state = bool(data.get("state", False))
    coin_svc.set_relay(state)
    return jsonify({"success": True, "relay_state": state})


@diagnostics_bp.route("/api/diagnostics/coin/status", methods=["GET"])
@admin_required
def coin_status():
    """Return live coin slot status and recent pulse events."""
    coin_svc = current_app.config.get("COIN_SERVICE")
    if coin_svc is None:
        return jsonify({"success": False, "error": "CoinSlotService is not initialized"}), 500

    return jsonify(coin_svc.get_status())

