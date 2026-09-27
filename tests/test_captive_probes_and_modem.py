"""Unit tests for OS captive portal probes, RFC 8908, ModemService, and SessionRecoveryService."""

import os
import tempfile
from unittest.mock import MagicMock
import pytest

from piso_wifi.config import AppConfig
from piso_wifi.services.modem_service import ModemService
from piso_wifi.services.session_recovery import SessionRecoveryService
from piso_wifi.web import create_app


@pytest.fixture
def app_probes():
    """Create Flask app for testing captive portal probes."""
    cfg = AppConfig(setup_completed=True)
    app = create_app(config=cfg)
    app.config["TESTING"] = True
    return app


def test_android_and_chromeos_probes(app_probes):
    """Test Android / Chromium /generate_204 and /gen_204 redirect to /."""
    client = app_probes.test_client()
    for path in ["/generate_204", "/gen_204", "/canonical.html", "/mobile/status", "/portal"]:
        res = client.get(path)
        assert res.status_code == 302
        assert res.location.endswith("/") or res.location.endswith("/#")


def test_apple_cna_probes(app_probes):
    """Test Apple iOS and macOS Captive Network Assistant probes."""
    client = app_probes.test_client()
    for path in ["/hotspot-detect.html", "/library/test/success.html"]:
        res = client.get(path)
        assert res.status_code == 302


def test_windows_ncsi_probes(app_probes):
    """Test Windows NCSI probes redirect to captive portal."""
    client = app_probes.test_client()
    for path in ["/connecttest.txt", "/ncsi.txt", "/msftconnecttest.txt"]:
        res = client.get(path)
        assert res.status_code == 302


def test_rfc8908_captive_portal_endpoint(app_probes):
    """Test RFC 8908 JSON API endpoint."""
    client = app_probes.test_client()
    res = client.get("/.well-known/captive-portal")
    assert res.status_code == 200
    data = res.get_json()
    assert data["captive"] is True
    assert "user-portal-url" in data
    assert "192.168.4.1" in data["user-portal-url"]


def test_modem_service():
    """Test ModemService USB cellular modem identification."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create mock eth0
        eth_dir = os.path.join(tmp_dir, "eth0")
        os.makedirs(eth_dir)
        with open(os.path.join(eth_dir, "carrier"), "w") as f:
            f.write("1\n")
        with open(os.path.join(eth_dir, "operstate"), "w") as f:
            f.write("up\n")

        # Create mock usb0 (cellular modem)
        usb_dir = os.path.join(tmp_dir, "usb0")
        os.makedirs(usb_dir)
        with open(os.path.join(usb_dir, "carrier"), "w") as f:
            f.write("1\n")
        with open(os.path.join(usb_dir, "operstate"), "w") as f:
            f.write("up\n")

        service = ModemService(sys_net_path=tmp_dir)
        modems = service.list_cellular_modems()
        assert len(modems) == 1
        assert modems[0]["interface"] == "usb0"
        assert modems[0]["is_cellular"] is True

        # Test failover
        failover = service.check_wan_failover(primary_iface="eth0")
        assert failover["status"] == "normal"
        assert failover["primary_online"] is True
        assert failover["has_cellular_backup"] is True


def test_session_recovery_randomized_mac():
    """Test SessionRecoveryService automatically migrates balance on randomized MAC rotation."""
    mock_user_svc = MagicMock()
    # Initial balance lookup
    balances = {
        "02:00:00:AA:BB:CC": 45.0, # original phone MAC
        "DA:A1:19:22:33:44": 0.0,  # newly randomized MAC
    }
    mock_user_svc.check_balance.side_effect = lambda mac: balances.get(mac, 0.0)

    service = SessionRecoveryService(user_service=mock_user_svc)

    # 1. Customer connects with original MAC
    token = service.get_or_create_token("02:00:00:AA:BB:CC")
    assert token is not None

    # 2. Customer reconnects next day; phone randomized its MAC to DA:A1:19:22:33:44,
    # but browser preserved the token cookie:
    resolved_mac = service.resolve_mac_with_token(token, "DA:A1:19:22:33:44")
    assert resolved_mac == "DA:A1:19:22:33:44"

    # Verify balance was migrated
    mock_user_svc.deduct_time.assert_called_with("02:00:00:AA:BB:CC", 45)
    mock_user_svc.add_time.assert_called_with("DA:A1:19:22:33:44", 45.0)
