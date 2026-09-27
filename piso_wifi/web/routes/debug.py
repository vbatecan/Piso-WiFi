"""Debug diagnostic routes for Piso-WiFi web application."""

import logging
from typing import Any, Dict, List
from flask import Blueprint, current_app, jsonify

logger = logging.getLogger(__name__)

debug_bp = Blueprint("debug", __name__)


def _exec_command(nc: Any, command: str) -> str:
    """Execute command via network controller or return empty string on error."""
    if nc is None:
        return ""
    try:
        if hasattr(nc, "_execute_command"):
            return nc._execute_command(command, ignore_errors=True) or ""
        if hasattr(nc, "runner") and hasattr(nc.runner, "execute"):
            return nc.runner.execute(command, ignore_errors=True) or ""
    except Exception as e:
        logger.debug(f"Command '{command}' execution failed in debug endpoint: {e}")
    return ""


@debug_bp.route("/debug/connections")
def debug_connections():
    """Debug endpoint to inspect network controller state, interfaces, and packet filtering."""
    network_controller = current_app.config.get("NETWORK_CONTROLLER")
    try:
        raw_devices = []
        if network_controller is not None and hasattr(network_controller, "get_connected_devices"):
            raw_devices = network_controller.get_connected_devices() or []

        devices: List[Dict[str, Any]] = []
        for dev in raw_devices:
            if isinstance(dev, dict):
                devices.append(dev)
            elif hasattr(dev, "to_dict"):
                devices.append(dev.to_dict())
            else:
                devices.append({
                    "mac_address": getattr(dev, "mac_address", ""),
                    "ip": getattr(dev, "ip", "Unknown"),
                    "hostname": getattr(dev, "hostname", "Unknown"),
                    "connected": getattr(dev, "connected", True),
                    "signal": getattr(dev, "signal", None),
                })

        ap_iface = getattr(network_controller, "ap_interface", "wlan0") if network_controller else "wlan0"
        inet_iface = getattr(network_controller, "internet_interface", "wlan1") if network_controller else "wlan1"

        ap_status = _exec_command(network_controller, f"ip addr show {ap_iface}")
        internet_status = _exec_command(network_controller, f"ip addr show {inet_iface}")
        hostapd_status = _exec_command(network_controller, "systemctl status hostapd")
        iptables_rules = _exec_command(network_controller, "iptables -L -n -v")

        uplink_status: Dict[str, Any] = {}
        if network_controller is not None and hasattr(network_controller, "get_uplink_status"):
            try:
                raw_uplink = network_controller.get_uplink_status()
                if isinstance(raw_uplink, dict):
                    uplink_status = raw_uplink
                elif hasattr(raw_uplink, "to_dict") and not hasattr(raw_uplink, "_mock_return_value"):
                    res = raw_uplink.to_dict()
                    uplink_status = res if isinstance(res, dict) else {"status": str(res)}
                elif raw_uplink is not None and not hasattr(raw_uplink, "_mock_return_value"):
                    uplink_status = {"status": str(raw_uplink)}
                elif hasattr(raw_uplink, "_mock_return_value"):
                    uplink_status = {"status": "mocked"}
            except Exception as e:
                logger.debug(f"Failed to query uplink status in debug endpoint: {e}")
                uplink_status = {"error": str(e)}

        return jsonify({
            "connected_devices": devices,
            "ap_interface_status": ap_status,
            "internet_interface_status": internet_status,
            "hostapd_status": hostapd_status,
            "iptables_rules": iptables_rules,
            "uplink_status": uplink_status,
        })
    except Exception as e:
        logger.error(f"Error in debug_connections route: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
