"""Unit tests for web onboarding routes, preflight APIs, and setup wizard."""

from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.config import AppConfig
from piso_wifi.services.system_service import NetworkInterfaceInfo, SystemService
from piso_wifi.web import create_app


@pytest.fixture
def mock_system_service():
    """Mock SystemService for onboarding tests."""
    mock = MagicMock(spec=SystemService)
    mock.list_interfaces.return_value = [
        NetworkInterfaceInfo(
            name="wlan0",
            is_wireless=True,
            supports_ap=True,
            operstate="up",
            carrier=True,
            mac_address="60:ff:9e:3b:c7:3a",
            ip_addresses=["192.168.4.1/24"],
            is_default_uplink=False,
        ),
        NetworkInterfaceInfo(
            name="eth0",
            is_wireless=False,
            supports_ap=False,
            operstate="up",
            carrier=True,
            mac_address="bc:fc:e7:01:e7:9e",
            ip_addresses=["192.168.1.100/24"],
            gateway="192.168.1.1",
            is_default_uplink=True,
        ),
    ]
    mock.check_prerequisites.return_value = {
        "ready": True,
        "is_root": True,
        "binaries": {
            "hostapd": {"installed": True, "path": "/usr/sbin/hostapd"},
            "dnsmasq": {"installed": True, "path": "/usr/sbin/dnsmasq"},
            "iptables": {"installed": True, "path": "/usr/sbin/iptables"},
            "iw": {"installed": True, "path": "/usr/sbin/iw"},
            "ip": {"installed": True, "path": "/usr/sbin/ip"},
        },
        "missing_binaries": [],
        "rfkill": {"blocked": False, "details": "WiFi unblocked"},
        "hardware": {"total_interfaces": 2, "has_wireless": True, "has_ap_capable": True},
        "issues": [],
    }
    mock.run_ping.return_value = {
        "target": "8.8.8.8",
        "success": True,
        "packet_loss_pct": 0.0,
        "avg_latency_ms": 14.5,
        "output": "3 packets, 0% loss",
    }
    mock.run_dns_lookup.return_value = {
        "domain": "google.com",
        "success": True,
        "resolved_ips": ["142.250.190.46"],
        "duration_ms": 12.3,
        "error": None,
    }
    return mock


@pytest.fixture
def onboarding_app(mock_system_service):
    """Flask application configured in unconfigured state for testing onboarding."""
    cfg = AppConfig(
        setup_completed=False,
        admin_username="admin",
        admin_password="initial_password",
    )
    application = create_app(
        config=cfg,
        system_service=mock_system_service,
    )
    application.config["TESTING"] = True
    application.config["TEST_ONBOARDING_GUARD"] = True
    return application


@pytest.fixture
def client(onboarding_app):
    return onboarding_app.test_client()


def test_unconfigured_redirects_to_onboarding(client):
    """Test that requests to / redirect to /onboarding when setup is not completed."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/onboarding" in response.headers["Location"]


def test_onboarding_page_renders_successfully(client):
    """Test GET /onboarding renders the setup wizard."""
    response = client.get("/onboarding")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Piso-WiFi Setup &amp; Onboarding" in html or "Piso-WiFi Setup & Onboarding" in html
    assert "Step 1: System &amp; Hardware Pre-Flight Check" in html or "Step 1: System & Hardware Pre-Flight Check" in html
    assert "wlan0" in html


def test_preflight_api(client):
    """Test GET /api/onboarding/preflight returns JSON diagnostics."""
    response = client.get("/api/onboarding/preflight")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ready"] is True
    assert "binaries" in data
    assert data["binaries"]["hostapd"]["installed"] is True


def test_interfaces_api(client):
    """Test GET /api/onboarding/interfaces returns discovered network adapters."""
    response = client.get("/api/onboarding/interfaces")
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert len(data) == 2
    names = [i["name"] for i in data]
    assert "wlan0" in names
    assert "eth0" in names


def test_diagnostics_ping_api(client):
    """Test POST /api/onboarding/diagnostics/ping."""
    response = client.post(
        "/api/onboarding/diagnostics/ping",
        json={"target": "8.8.8.8", "count": 2},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["packet_loss_pct"] == 0.0


def test_diagnostics_dns_api(client):
    """Test POST /api/onboarding/diagnostics/dns."""
    response = client.post(
        "/api/onboarding/diagnostics/dns",
        json={"domain": "google.com"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert "142.250.190.46" in data["resolved_ips"]


def test_complete_setup_validation_error(client):
    """Test POST /onboarding/complete fails when passwords do not match."""
    response = client.post(
        "/onboarding/complete",
        data={
            "wifi_interface": "wlan0",
            "ssid": "PisoWiFi",
            "ap_ip": "192.168.4.1",
            "admin_username": "newadmin",
            "admin_password": "secretpassword",
            "admin_password_confirm": "mismatch",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Admin password confirmation does not match" in html


def test_complete_setup_success(client, onboarding_app):
    """Test successful onboarding submission saves config and activates session."""
    with patch("piso_wifi.web.routes.onboarding.save_env_file", return_value=True) as mock_save:
        response = client.post(
            "/onboarding/complete",
            data={
                "wifi_interface": "wlan0",
                "internet_interface": "auto",
                "ssid": "BarangayHotspot",
                "password": "hotspotpassword",
                "ap_ip": "192.168.4.1",
                "network_mask": "255.255.255.0",
                "dhcp_start": "192.168.4.2",
                "dhcp_end": "192.168.4.50",
                "minutes_per_peso": "10.0",
                "default_download_kbps": "3000",
                "default_upload_kbps": "1500",
                "premium_download_kbps": "10000",
                "premium_upload_kbps": "10000",
                "admin_username": "siteadmin",
                "admin_password": "secureadminpass",
                "admin_password_confirm": "secureadminpass",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

        mock_save.assert_called_once()
        saved_dict = mock_save.call_args[0][0]
        assert saved_dict["WIFI_INTERFACE"] == "wlan0"
        assert saved_dict["AP_SSID"] == "BarangayHotspot"
        assert saved_dict["ADMIN_USERNAME"] == "siteadmin"
        assert saved_dict["SETUP_COMPLETED"] == "true"

        # Verify in-memory config update
        curr_config = onboarding_app.config["CONFIG"]
        assert curr_config.setup_completed is True
        assert curr_config.network.ssid == "BarangayHotspot"
        assert curr_config.admin_username == "siteadmin"
        assert curr_config.minutes_per_peso == 10.0

        # Verify session logged in as admin
        with client.session_transaction() as sess:
            assert sess.get("is_admin") is True
