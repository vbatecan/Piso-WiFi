"""Dashboard routes for Piso-WiFi web application."""

import logging
from typing import Any, Dict, List, Optional
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from piso_wifi.web.auth import admin_required
from piso_wifi.web.routes.debug import debug_connections

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)


def _get_services():
    """Retrieve injected services from application context."""
    user_service = current_app.config.get("USER_SERVICE")
    network_controller = current_app.config.get("NETWORK_CONTROLLER")
    return user_service, network_controller


def _redirect_index():
    """Safely redirect to index view."""
    try:
        return redirect(url_for("dashboard.index"))
    except Exception:
        return redirect(url_for("index"))


def get_client_ip() -> str:
    """Retrieve client remote IP address handling reverse proxies."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def get_client_mac(client_ip: str) -> Optional[str]:
    """Resolve client IP to hardware MAC address via /proc/net/arp or ip neigh."""
    if not client_ip or client_ip in ("127.0.0.1", "localhost", "::1"):
        return None
    try:
        with open("/proc/net/arp", "r") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == client_ip:
                    mac = parts[3]
                    if mac and mac != "00:00:00:00:00:00":
                        return mac.upper()
    except Exception:
        pass

    try:
        import subprocess
        out = subprocess.check_output(["ip", "neigh", "show", client_ip], text=True, timeout=1)
        for part in out.split():
            if ":" in part and len(part) == 17:
                return part.upper()
    except Exception:
        pass

    return None


# ============================================================================
# OS Captive Portal Detection Endpoints (RFC 8908 & Vendor Probes)
# ============================================================================


@dashboard_bp.route("/generate_204")
@dashboard_bp.route("/gen_204")
@dashboard_bp.route("/canonical.html")
@dashboard_bp.route("/mobile/status")
@dashboard_bp.route("/portal")
def captive_portal_android():
    """Android, Chromium, and vendor captive portal detection endpoints."""
    return _redirect_index()


@dashboard_bp.route("/hotspot-detect.html")
@dashboard_bp.route("/library/test/success.html")
def captive_portal_apple():
    """Apple iOS and macOS Captive Network Assistant (CNA) detection endpoint."""
    return _redirect_index()


@dashboard_bp.route("/connecttest.txt")
@dashboard_bp.route("/ncsi.txt")
@dashboard_bp.route("/msftconnecttest.txt")
def captive_portal_windows():
    """Windows Network Connectivity Status Indicator (NCSI) detection endpoint."""
    return _redirect_index()


@dashboard_bp.route("/.well-known/captive-portal")
def rfc8908_captive_portal():
    """RFC 8908 Captive Portal API JSON status endpoint."""
    ap_ip = getattr(current_app.config.get("CONFIG"), "network", None)
    ip_str = getattr(ap_ip, "ap_ip", "192.168.4.1") if ap_ip else "192.168.4.1"
    return jsonify({
        "captive": True,
        "user-portal-url": f"http://{ip_str}/",
        "venue-info-url": f"http://{ip_str}/"
    })


@dashboard_bp.route("/")
def index():
    """Render dashboard displaying connected devices, uplink status, and balances."""
    user_service, network_controller = _get_services()
    try:
        raw_devices = []
        if network_controller is not None and hasattr(network_controller, "get_connected_devices"):
            raw_devices = network_controller.get_connected_devices() or []

        default_down = getattr(network_controller, "DEFAULT_DOWNLOAD_SPEED", 2048)
        default_up = getattr(network_controller, "DEFAULT_UPLOAD_SPEED", 1024)

        devices: List[Dict[str, Any]] = []
        for dev in raw_devices:
            if isinstance(dev, dict):
                item = dict(dev)
            elif hasattr(dev, "to_dict"):
                item = dev.to_dict()
            else:
                item = {
                    "mac_address": getattr(dev, "mac_address", ""),
                    "ip": getattr(dev, "ip", "Unknown"),
                    "hostname": getattr(dev, "hostname", "Unknown"),
                    "signal": getattr(dev, "signal", None),
                    "connected": getattr(dev, "connected", True),
                }

            mac = item.get("mac_address", "")
            user_info = None
            if user_service is not None and mac:
                if hasattr(user_service, "get_user_info"):
                    user_info = user_service.get_user_info(mac)

            if user_info is not None:
                item["time_balance"] = getattr(user_info, "time_balance", 0.0)
                item["download_limit"] = getattr(user_info, "download_limit", default_down)
                item["upload_limit"] = getattr(user_info, "upload_limit", default_up)
                plan = getattr(user_info, "plan", "default")
                item["plan"] = plan.value if hasattr(plan, "value") else str(plan)
                item["upgrade_requested"] = getattr(user_info, "upgrade_requested", False)
            else:
                time_bal = 0.0
                if user_service is not None and hasattr(user_service, "check_balance") and mac:
                    time_bal = user_service.check_balance(mac)
                item["time_balance"] = time_bal
                item["download_limit"] = default_down
                item["upload_limit"] = default_up
                item["plan"] = "default"
                item["upgrade_requested"] = False

            devices.append(item)

        # Query uplink status safely with fallback
        uplink_info: Dict[str, Any] = {}
        if network_controller is not None and hasattr(network_controller, "get_uplink_status"):
            try:
                raw_uplink = network_controller.get_uplink_status()
                if isinstance(raw_uplink, dict):
                    uplink_info = dict(raw_uplink)
                elif hasattr(raw_uplink, "to_dict"):
                    uplink_info = raw_uplink.to_dict()
                elif raw_uplink is not None:
                    uplink_info = {"status": str(raw_uplink)}
            except Exception as e:
                logger.warning(f"Failed to query uplink status from network controller: {e}")

        # Normalize / populate standard fields for the view template
        if uplink_info:
            if "connection_type" not in uplink_info:
                if uplink_info.get("is_wireless"):
                    uplink_info["connection_type"] = "Wireless Uplink"
                elif uplink_info.get("interface"):
                    if "wlan" in str(uplink_info["interface"]).lower() or "wifi" in str(uplink_info["interface"]).lower():
                        uplink_info["connection_type"] = "Wireless Uplink"
                    else:
                        uplink_info["connection_type"] = "Wired LAN"
                else:
                    uplink_info["connection_type"] = "Wired LAN"

            if "status" not in uplink_info:
                carrier_val = uplink_info.get("carrier")
                if carrier_val is True or str(carrier_val).lower() in ["true", "active", "up", "online", "1"]:
                    uplink_info["status"] = "Online"
                else:
                    uplink_info["status"] = "Online" if uplink_info.get("ip") else "Offline"

            if "carrier_status" not in uplink_info:
                carrier_val = uplink_info.get("carrier")
                if carrier_val is True or str(carrier_val).lower() in ["true", "active", "up", "online", "1"]:
                    uplink_info["carrier_status"] = "Active"
                else:
                    uplink_info["carrier_status"] = "Active" if uplink_info.get("status", "").lower() == "online" else "Disconnected"

            if "ip_address" not in uplink_info and "ip" in uplink_info:
                uplink_info["ip_address"] = uplink_info["ip"]
        else:
            # Fallback default uplink info if not available
            inet_iface = getattr(network_controller, "internet_interface", "eth0") if network_controller else "eth0"
            is_wifi = "wlan" in str(inet_iface).lower() or "wifi" in str(inet_iface).lower()
            uplink_info = {
                "interface": inet_iface,
                "connection_type": "Wireless Uplink" if is_wifi else "Wired LAN",
                "status": "Online",
                "carrier_status": "Active",
                "ip_address": "Unknown",
                "gateway": "Unknown",
            }

        client_ip = get_client_ip()
        client_mac = get_client_mac(client_ip) or request.args.get("mac", "")
        if not client_mac and request.headers.get("X-Client-MAC"):
            client_mac = request.headers.get("X-Client-MAC").strip().upper()

        client_user = None
        if user_service and client_mac:
            client_user = user_service.get_user_info(client_mac)

        client_status = "inactive"
        if client_user:
            raw_st = getattr(client_user, "status", None)
            client_status = raw_st.value if hasattr(raw_st, "value") else str(raw_st or "inactive")
            if client_status == "active" and getattr(client_user, "time_balance", 0.0) <= 0:
                client_status = "expired"

        client_info = {
            "ip": client_ip,
            "mac": client_mac or "UNKNOWN",
            "detected": bool(client_mac),
            "time_balance": getattr(client_user, "time_balance", 0.0) if client_user else 0.0,
            "status": client_status,
            "plan": (getattr(client_user, "plan", None).value if hasattr(getattr(client_user, "plan", None), "value") else str(getattr(client_user, "plan", "default"))) if client_user else "default",
        }

        is_admin_user = session.get("is_admin", False)
        return render_template(
            "index.html",
            devices=devices,
            uplink_info=uplink_info,
            is_admin=is_admin_user,
            client_info=client_info,
        )

    except Exception as e:
        logger.error(f"Error in index route: {e}", exc_info=True)
        return "Internal Server Error", 500


@dashboard_bp.route("/add_time", methods=["POST"])
def add_time():
    """Add time balance to a device and unblock network access."""
    user_service, network_controller = _get_services()
    try:
        mac_address = request.form.get("mac_address", "").strip()
        amount_raw = request.form.get("amount")

        if not mac_address or amount_raw is None:
            flash("MAC address and amount are required", "error")
            return _redirect_index()

        try:
            amount = float(amount_raw)
        except (ValueError, TypeError):
            flash("Please enter a valid amount", "error")
            return _redirect_index()

        if amount <= 0:
            flash("Amount must be greater than zero", "error")
            return _redirect_index()

        if user_service is not None and hasattr(user_service, "add_time"):
            success = user_service.add_time(mac_address, amount)
            if success:
                if network_controller is not None and hasattr(network_controller, "unblock_mac"):
                    network_controller.unblock_mac(mac_address)
                flash(f"Successfully added time for {mac_address}", "success")
                return _redirect_index()

        flash("Error adding time", "error")
        return _redirect_index()

    except Exception as e:
        logger.error(f"Error in add_time route: {e}", exc_info=True)
        flash("Internal Server Error", "error")
        return _redirect_index()


@dashboard_bp.route("/deduct_time", methods=["POST"])
def deduct_time():
    """Deduct time balance from a device; block if depleted."""
    user_service, network_controller = _get_services()
    try:
        mac_address = request.form.get("mac_address", "").strip()
        minutes_raw = request.form.get("minutes", "0")

        try:
            minutes = float(minutes_raw)
        except (ValueError, TypeError):
            minutes = 0.0

        if not mac_address or minutes <= 0:
            flash("Please enter a valid number of minutes", "error")
            return _redirect_index()

        if user_service is not None and hasattr(user_service, "deduct_time"):
            success = user_service.deduct_time(mac_address, minutes, manual=True)
            if success:
                new_balance = 0.0
                if hasattr(user_service, "check_balance"):
                    new_balance = user_service.check_balance(mac_address)

                if new_balance <= 0 and network_controller is not None:
                    if hasattr(network_controller, "block_mac"):
                        network_controller.block_mac(mac_address)
                        logger.info(f"Blocked {mac_address} due to zero balance after manual deduction")

                flash(f"Successfully deducted {minutes} minutes", "success")
            else:
                flash("Error deducting time", "error")
        else:
            flash("Error deducting time", "error")

        return _redirect_index()

    except Exception as e:
        logger.error(f"Error in deduct_time route: {e}", exc_info=True)
        flash("Internal Server Error", "error")
        return _redirect_index()


@dashboard_bp.route("/set_bandwidth", methods=["POST"])
def set_bandwidth():
    """Validate and apply bandwidth rate limits."""
    user_service, network_controller = _get_services()
    try:
        mac_address = request.form.get("mac_address", "").strip()
        download_raw = request.form.get("download", 1024)
        upload_raw = request.form.get("upload", 512)

        try:
            download = int(download_raw)
            upload = int(upload_raw)
        except (ValueError, TypeError):
            flash("Bandwidth values must be valid integers", "error")
            return _redirect_index()

        if download < 32 or upload < 32:
            flash("Minimum bandwidth is 32 kbps", "error")
            return _redirect_index()

        if download > 100000 or upload > 100000:
            flash("Maximum bandwidth is 100 Mbps", "error")
            return _redirect_index()

        success = False
        if user_service is not None and hasattr(user_service, "set_bandwidth"):
            success = user_service.set_bandwidth(mac_address, download, upload)

        if success:
            if network_controller is not None and hasattr(network_controller, "set_bandwidth_limit"):
                network_controller.set_bandwidth_limit(mac_address, download, upload)
            flash("Bandwidth limits updated successfully", "success")
        else:
            flash("Error updating bandwidth settings", "error")

        return _redirect_index()

    except Exception as e:
        logger.error(f"Error in set_bandwidth route: {e}", exc_info=True)
        flash("Internal Server Error", "error")
        return _redirect_index()


@dashboard_bp.route("/request_upgrade", methods=["POST"])
def request_upgrade():
    """Flag a device's request for premium plan upgrade."""
    user_service, _ = _get_services()
    try:
        mac_address = request.form.get("mac_address", "").strip()
        if not mac_address:
            flash("MAC address is required", "error")
            return _redirect_index()

        if user_service is not None and hasattr(user_service, "request_upgrade"):
            user_service.request_upgrade(mac_address)
            flash("Premium upgrade requested. Please wait for admin approval.", "success")
        else:
            flash("Error requesting upgrade", "error")

        return _redirect_index()

    except Exception as e:
        logger.error(f"Error requesting upgrade: {e}", exc_info=True)
        flash("Error requesting upgrade", "error")
        return _redirect_index()


@dashboard_bp.route("/manage_plan", methods=["POST"])
@admin_required
def manage_plan():
    """Update device plan tier and bandwidth limits (admin only)."""
    user_service, network_controller = _get_services()
    try:
        mac_address = request.form.get("mac_address", "").strip()
        new_plan = request.form.get("plan", "default").strip().lower()

        if not mac_address:
            flash("MAC address is required", "error")
            return _redirect_index()

        if user_service is not None and hasattr(user_service, "get_user_info"):
            current_user = user_service.get_user_info(mac_address)
            if current_user is not None:
                cur_plan = getattr(current_user, "plan", "")
                cur_plan_str = cur_plan.value if hasattr(cur_plan, "value") else str(cur_plan)
                if cur_plan_str == new_plan:
                    flash("Device is already on this plan", "info")
                    return _redirect_index()

        # Remove existing bandwidth limit if supported
        if network_controller is not None and hasattr(network_controller, "remove_bandwidth_limit"):
            network_controller.remove_bandwidth_limit(mac_address)

        # Determine bandwidth speeds based on plan
        if new_plan == "premium":
            download_speed = getattr(network_controller, "PREMIUM_DOWNLOAD_SPEED", 8096)
            upload_speed = getattr(network_controller, "PREMIUM_UPLOAD_SPEED", 8096)
        else:
            download_speed = getattr(network_controller, "DEFAULT_DOWNLOAD_SPEED", 2048)
            upload_speed = getattr(network_controller, "DEFAULT_UPLOAD_SPEED", 1024)

        # Update in user service
        updated = False
        if user_service is not None and hasattr(user_service, "manage_plan"):
            updated = user_service.manage_plan(mac_address, new_plan, download_speed, upload_speed)
        elif user_service is not None and hasattr(user_service, "set_bandwidth"):
            updated = user_service.set_bandwidth(mac_address, download_speed, upload_speed)

        # Apply new bandwidth limits via network controller
        limits_applied = False
        if network_controller is not None and hasattr(network_controller, "set_bandwidth_limit"):
            limits_applied = network_controller.set_bandwidth_limit(mac_address, download_speed, upload_speed)
        else:
            limits_applied = True

        if updated:
            if limits_applied:
                flash(
                    f"Plan updated to {new_plan}. New speeds: {download_speed}kbps down / {upload_speed}kbps up",
                    "success",
                )
            else:
                flash("Plan updated but there was an issue applying bandwidth limits", "warning")
        else:
            flash("Error updating plan", "error")

        return _redirect_index()

    except Exception as e:
        logger.error(f"Error managing plan: {e}", exc_info=True)
        flash("Error updating plan", "error")
        return _redirect_index()


@dashboard_bp.route("/api/coin/insert_coin", methods=["POST"])
def start_insert_coin():
    """Start an active insert-coin payment window for a customer device."""
    coin_svc = current_app.config.get("COIN_SERVICE")
    if coin_svc is None:
        return jsonify({"success": False, "error": "Coin service unavailable"}), 500

    data = request.get_json(silent=True) or request.form
    mac_address = data.get("mac_address", "").strip()
    if not mac_address:
        return jsonify({"success": False, "error": "MAC address is required"}), 400

    result = coin_svc.start_payment_session(mac_address=mac_address)
    return jsonify(result)


@dashboard_bp.route("/api/coin/session", methods=["GET"])
def get_coin_session():
    """Query current payment session state, countdown timer, and accumulated coins."""
    coin_svc = current_app.config.get("COIN_SERVICE")
    if coin_svc is None:
        return jsonify({"success": False, "error": "Coin service unavailable"}), 500

    session_info = coin_svc.get_active_session()
    return jsonify({
        "success": True,
        "has_active_session": session_info is not None,
        "session": session_info,
    })


@dashboard_bp.route("/api/coin/cancel_session", methods=["POST"])
def cancel_coin_session():
    """Cancel payment session and turn off coin slot relay."""
    coin_svc = current_app.config.get("COIN_SERVICE")
    if coin_svc is None:
        return jsonify({"success": False, "error": "Coin service unavailable"}), 500

    coin_svc.cancel_payment_session()
    return jsonify({"success": True})


# ============================================================================
# Customer Portal APIs (Pause/Resume, Status, Vouchers)
# ============================================================================


@dashboard_bp.route("/api/portal/status", methods=["GET"])
def portal_status():
    """Return live status, time balance, and connection info for the caller device."""
    user_service, _ = _get_services()
    client_ip = get_client_ip()
    client_mac = request.args.get("mac_address", "").strip() or get_client_mac(client_ip) or ""
    if not client_mac and request.headers.get("X-Client-MAC"):
        client_mac = request.headers.get("X-Client-MAC").strip().upper()

    user_info = None
    if user_service and client_mac:
        user_info = user_service.get_user_info(client_mac)

    status_str = "inactive"
    if user_info:
        raw_st = getattr(user_info, "status", None)
        status_str = raw_st.value if hasattr(raw_st, "value") else str(raw_st or "inactive")
        if status_str == "active" and getattr(user_info, "time_balance", 0.0) <= 0:
            status_str = "expired"

    return jsonify({
        "success": True,
        "ip": client_ip,
        "mac_address": client_mac,
        "has_mac": bool(client_mac),
        "time_balance": getattr(user_info, "time_balance", 0.0) if user_info else 0.0,
        "status": status_str,
        "plan": getattr(user_info, "plan", "default") if user_info else "default",
    })


@dashboard_bp.route("/api/portal/pause", methods=["POST"])
def portal_pause():
    """Pause internet session and freeze remaining time balance."""
    user_service, network_controller = _get_services()
    data = request.get_json(silent=True) or request.form
    client_ip = get_client_ip()
    mac = data.get("mac_address", "").strip() or get_client_mac(client_ip) or ""

    if not mac:
        return jsonify({"success": False, "error": "MAC address could not be identified"}), 400

    if user_service and hasattr(user_service, "pause_time"):
        if user_service.pause_time(mac):
            if network_controller and hasattr(network_controller, "block_mac"):
                network_controller.block_mac(mac)
            return jsonify({"success": True, "status": "paused", "mac_address": mac})

    return jsonify({"success": False, "error": "Unable to pause session (insufficient balance or invalid device)"}), 400


@dashboard_bp.route("/api/portal/resume", methods=["POST"])
def portal_resume():
    """Resume a paused internet session and restore connection."""
    user_service, network_controller = _get_services()
    data = request.get_json(silent=True) or request.form
    client_ip = get_client_ip()
    mac = data.get("mac_address", "").strip() or get_client_mac(client_ip) or ""

    if not mac:
        return jsonify({"success": False, "error": "MAC address could not be identified"}), 400

    if user_service and hasattr(user_service, "resume_time"):
        if user_service.resume_time(mac):
            if network_controller and hasattr(network_controller, "unblock_mac"):
                network_controller.unblock_mac(mac)
            return jsonify({"success": True, "status": "active", "mac_address": mac})

    return jsonify({"success": False, "error": "Unable to resume session"}), 400


@dashboard_bp.route("/api/portal/redeem-voucher", methods=["POST"])
def portal_redeem_voucher():
    """Redeem a promo or time voucher code for internet access."""
    user_service, network_controller = _get_services()
    data = request.get_json(silent=True) or request.form
    code = data.get("code", "").strip()
    client_ip = get_client_ip()
    mac = data.get("mac_address", "").strip() or get_client_mac(client_ip) or ""

    if not code:
        return jsonify({"success": False, "error": "Voucher code is required"}), 400

    if not mac:
        return jsonify({"success": False, "error": "Device MAC address is required"}), 400

    if user_service and hasattr(user_service, "redeem_voucher"):
        res = user_service.redeem_voucher(code=code, mac_address=mac)
        if res.get("success"):
            if network_controller and hasattr(network_controller, "unblock_mac"):
                network_controller.unblock_mac(mac)
        return jsonify(res)

    return jsonify({"success": False, "error": "Voucher system not available"}), 500


@dashboard_bp.route("/api/vouchers/generate", methods=["POST"])
@admin_required
def generate_vouchers():
    """Generate new voucher codes (Admin only)."""
    user_service, _ = _get_services()
    data = request.get_json(silent=True) or request.form
    try:
        count = int(data.get("count", 1))
        minutes = float(data.get("time_minutes", 60))
        days = int(data.get("expires_days", 30))
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid voucher parameters"}), 400

    if user_service and hasattr(user_service, "create_vouchers"):
        codes = user_service.create_vouchers(count=count, time_minutes=minutes, expires_days=days)
        return jsonify({"success": True, "vouchers": codes, "count": len(codes)})

    return jsonify({"success": False, "error": "Voucher service unavailable"}), 500


@dashboard_bp.route("/api/vouchers/list", methods=["GET"])
@admin_required
def list_vouchers():
    """List vouchers (Admin only)."""
    user_service, _ = _get_services()
    active_only = request.args.get("active_only", "false").lower() in ("true", "1")
    if user_service and hasattr(user_service, "list_vouchers"):
        vouchers = user_service.list_vouchers(active_only=active_only)
        return jsonify({
            "success": True,
            "vouchers": [v.to_dict() if hasattr(v, "to_dict") else v for v in vouchers],
        })
    return jsonify({"success": False, "error": "Voucher service unavailable"}), 500

