"""Unit tests for settings configuration management and diagnostics portal."""

from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.config import AppConfig
from piso_wifi.services.system_service import NetworkInterfaceInfo, SystemService
from piso_wifi.web import create_app


@pytest.fixture
def mock_system_service():
    """Mock SystemService for settings & diagnostics tests."""
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
    mock.get_service_statuses.return_value = {
        "hostapd": {"active": True, "state": "active"},
        "dnsmasq": {"active": True, "state": "active"},
        "iptables_nat": True,
    }
    mock.get_system_metrics.return_value = {
        "load_average": [0.15, 0.22, 0.18],
        "memory": {"total_mb": 4096, "used_mb": 1024, "available_mb": 3072, "used_pct": 25.0},
        "disk": {"total_gb": 32.0, "free_gb": 20.0, "used_pct": 37.5},
        "uptime": "2h 45m",
        "hostname": "pisowifi-box",
    }
    mock.check_prerequisites.return_value = {
        "ready": True,
        "is_root": True,
        "binaries": {
            "hostapd": {"installed": True, "path": "/usr/sbin/hostapd"},
            "dnsmasq": {"installed": True, "path": "/usr/sbin/dnsmasq"},
        },
        "missing_binaries": [],
        "rfkill": {"blocked": False, "details": "WiFi unblocked"},
        "hardware": {"total_interfaces": 2, "has_wireless": True, "has_ap_capable": True},
        "issues": [],
    }
    mock.run_ping.return_value = {
        "target": "1.1.1.1",
        "success": True,
        "packet_loss_pct": 0.0,
        "avg_latency_ms": 11.2,
        "output": "PING 1.1.1.1: 3 packets, 0% loss",
    }
    mock.run_dns_lookup.return_value = {
        "domain": "cloudflare.com",
        "success": True,
        "resolved_ips": ["104.16.132.229"],
        "duration_ms": 8.5,
        "error": None,
    }
    mock.restart_service.return_value = {
        "service": "hostapd",
        "success": True,
        "returncode": 0,
        "output": "",
    }
    return mock


@pytest.fixture
def mock_network_controller():
    mock = MagicMock()
    mock.get_uplink_status.return_value = {
        "interface": "eth0",
        "connection_type": "Wired LAN",
        "status": "Online",
        "ip_address": "192.168.1.100",
        "gateway": "192.168.1.1",
    }
    mock.start_ap.return_value = None
    return mock


@pytest.fixture
def app(mock_system_service, mock_network_controller):
    cfg = AppConfig(
        setup_completed=True,
        admin_username="admin",
        admin_password="adminpassword123",
        minutes_per_peso=5.0,
    )
    application = create_app(
        config=cfg,
        network_controller=mock_network_controller,
        system_service=mock_system_service,
    )
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize("rate", ["0", "-1", "nan", "inf"])
def test_invalid_rates_do_not_change_configuration(client, app, rate):
    with client.session_transaction() as sess:
        sess["is_admin"] = True
    with patch("piso_wifi.web.routes.settings.save_env_file") as save:
        client.post("/settings/rates", data={"minutes_per_peso": rate})
    save.assert_not_called()
    assert app.config["CONFIG"].minutes_per_peso == 5.0


def test_invalid_password_does_not_change_username(client, app):
    with client.session_transaction() as sess:
        sess["is_admin"] = True
    with patch("piso_wifi.web.routes.settings.save_env_file") as save:
        client.post("/settings/admin", data={"admin_username": "changed", "admin_password": "longpassword", "admin_password_confirm": "mismatch"})
    save.assert_not_called()
    assert app.config["CONFIG"].admin_username == "admin"


def test_settings_requires_admin(client):
    """Test that accessing /settings without admin login redirects."""
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 302


def test_settings_page_accessible_by_admin(client):
    """Test that authenticated admin can view /settings."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.get("/settings")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "System Configurations" in html
    assert "Network &amp; Hotspot" in html or "Network & Hotspot" in html
    assert "Rates &amp; Bandwidth" in html or "Rates & Bandwidth" in html


def test_update_network_settings(client, app):
    """Test updating network and WiFi settings via POST /settings/network."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    with patch("piso_wifi.web.routes.settings.save_env_file", return_value=True) as mock_save:
        response = client.post(
            "/settings/network",
            data={
                "wifi_interface": "wlan0",
                "internet_interface": "eth0",
                "ssid": "NewSSID",
                "password": "newpassword123",
                "ap_ip": "192.168.10.1",
                "network_mask": "255.255.255.0",
                "dhcp_start": "192.168.10.2",
                "dhcp_end": "192.168.10.50",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved["AP_SSID"] == "NewSSID"
        assert saved["AP_IP"] == "192.168.10.1"

        curr_cfg = app.config["CONFIG"]
        assert curr_cfg.network.ssid == "NewSSID"
        assert curr_cfg.network.ip == "192.168.10.1"


def test_update_rates_settings(client, app):
    """Test updating pricing and QoS bandwidth limits via POST /settings/rates."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    with patch("piso_wifi.web.routes.settings.save_env_file", return_value=True) as mock_save:
        response = client.post(
            "/settings/rates",
            data={
                "minutes_per_peso": "12.0",
                "default_download_kbps": "4096",
                "default_upload_kbps": "2048",
                "premium_download_kbps": "12000",
                "premium_upload_kbps": "12000",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        mock_save.assert_called_once()

        curr_cfg = app.config["CONFIG"]
        assert curr_cfg.minutes_per_peso == 12.0
        assert curr_cfg.network.bandwidth.default_download_kbps == 4096
        assert curr_cfg.network.bandwidth.premium_download_kbps == 12000


def test_update_admin_credentials(client, app):
    """Test updating admin username and password via POST /settings/admin."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    with patch("piso_wifi.web.routes.settings.save_env_file", return_value=True) as mock_save:
        response = client.post(
            "/settings/admin",
            data={
                "admin_username": "new_superadmin",
                "admin_password": "supersecurepass",
                "admin_password_confirm": "supersecurepass",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        mock_save.assert_called_once()
        curr_cfg = app.config["CONFIG"]
        assert curr_cfg.admin_username == "new_superadmin"
        assert curr_cfg.admin_password == "supersecurepass"


def test_apply_network_services(client, mock_network_controller):
    """Test triggering network service restart via POST /settings/apply-network."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.post("/settings/apply-network", follow_redirects=True)
    assert response.status_code == 200
    mock_network_controller.start_ap.assert_called_once()


def test_diagnostics_page_accessible_by_admin(client):
    """Test GET /diagnostics renders hardware and network status."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.get("/diagnostics")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Diagnostics &amp; Troubleshooting" in html or "Diagnostics & Troubleshooting" in html
    assert "Internet Uplink (WAN) Health" in html
    assert "wlan0" in html
    assert "pisowifi-box" in html


def test_diagnostics_interactive_ping(client):
    """Test POST /api/diagnostics/ping."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.post("/api/diagnostics/ping", json={"target": "1.1.1.1", "count": 3})
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["target"] == "1.1.1.1"


def test_diagnostics_interactive_dns(client):
    """Test POST /api/diagnostics/dns."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.post("/api/diagnostics/dns", json={"domain": "cloudflare.com"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert "104.16.132.229" in data["resolved_ips"]


def test_diagnostics_restart_daemon(client, mock_system_service):
    """Test POST /api/diagnostics/restart-daemon."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.post("/api/diagnostics/restart-daemon", json={"service": "hostapd"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    mock_system_service.restart_service.assert_called_with("hostapd")


def test_settings_backup_endpoints(client, app, tmp_path):
    """Test backup creation, download, list, prune, and delete endpoints in settings."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    from piso_wifi.services.backup_service import BackupService
    backup_svc = BackupService(backup_dir=str(tmp_path), db_path=str(tmp_path / "test.db"))
    app.config["BACKUP_SERVICE"] = backup_svc

    # 1. Create backup via POST
    res = client.post("/settings/backups/create", follow_redirects=True)
    assert res.status_code == 200

    # 2. List backups API
    res_list = client.get("/api/settings/backups")
    assert res_list.status_code == 200
    data = res_list.get_json()
    assert data["success"] is True
    assert len(data["backups"]) >= 1
    filename = data["backups"][0]["filename"]

    # 3. Download backup
    res_dl = client.get(f"/settings/backups/download/{filename}")
    assert res_dl.status_code == 200
    assert res_dl.mimetype == "application/zip"

    # 4. Prune backups
    res_prune = client.post("/settings/backups/prune", data={"max_keep": 5}, follow_redirects=True)
    assert res_prune.status_code == 200

    # 5. Delete backup
    res_del = client.post("/settings/backups/delete", data={"filename": filename}, follow_redirects=True)
    assert res_del.status_code == 200


def test_settings_voucher_endpoints(client, app):
    """Test promotional voucher generation, listing, and deletion via settings."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    mock_user_svc = MagicMock()
    mock_user_svc.create_vouchers.return_value = ["TESTCODE1", "TESTCODE2"]
    mock_user_svc.list_vouchers.return_value = [
        MagicMock(code="TESTCODE1", time_minutes=60, is_used=False, used_by_mac=None, created_at="2026-09-27", expires_at="2026-10-27", to_dict=lambda: {"code": "TESTCODE1", "time_minutes": 60})
    ]
    mock_user_svc.delete_voucher.return_value = True
    app.config["USER_SERVICE"] = mock_user_svc

    # 1. Generate vouchers
    res = client.post(
        "/settings/vouchers/generate",
        data={"count": 2, "time_minutes": 60, "expires_days": 30},
        follow_redirects=True,
    )
    assert res.status_code == 200
    mock_user_svc.create_vouchers.assert_called_once_with(count=2, time_minutes=60.0, expires_days=30)

    # 2. List vouchers API
    res_list = client.get("/api/settings/vouchers")
    assert res_list.status_code == 200
    data = res_list.get_json()
    assert data["success"] is True
    assert len(data["vouchers"]) == 1

    # 3. Delete voucher
    res_del = client.post("/settings/vouchers/delete", data={"code": "TESTCODE1"}, follow_redirects=True)
    assert res_del.status_code == 200
    mock_user_svc.delete_voucher.assert_called_once_with("TESTCODE1")


def test_settings_hardware_endpoints(client, app):
    """Test display and buzzer configuration updates and live test APIs."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    mock_disp = MagicMock()
    mock_buzz = MagicMock()
    app.config["DISPLAY_SERVICE"] = mock_disp
    app.config["BUZZER_SERVICE"] = mock_buzz

    with patch("piso_wifi.web.routes.settings.save_env_file", return_value=True):
        # 1. Update display
        res = client.post(
            "/settings/hardware/display",
            data={"display_enabled": "on", "display_type": "ssd1306_oled", "i2c_bus": "1", "i2c_address": "0x3c"},
            follow_redirects=True,
        )
        assert res.status_code == 200
        assert mock_disp.enabled is True
        assert mock_disp.display_type == "ssd1306_oled"

        # 2. Update buzzer
        res_buzz = client.post(
            "/settings/hardware/buzzer",
            data={"buzzer_enabled": "on", "buzzer_pin": "25", "buzzer_passive": "on"},
            follow_redirects=True,
        )
        assert res_buzz.status_code == 200
        assert mock_buzz.enabled is True
        assert mock_buzz.buzzer_pin == 25

    # 3. Test display API
    res_test_disp = client.post("/api/settings/hardware/test-display")
    assert res_test_disp.status_code == 200
    assert res_test_disp.get_json()["success"] is True
    mock_disp._render.assert_called_once()

    # 4. Test buzzer API
    res_test_buzz = client.post("/api/settings/hardware/test-buzzer")
    assert res_test_buzz.status_code == 200
    assert res_test_buzz.get_json()["success"] is True
    mock_buzz.beep_session_start.assert_called_once()
