"""Unit tests for Piso-WiFi Flask web application and routes."""

from unittest.mock import MagicMock
import pytest
from flask import session

from piso_wifi.config import AppConfig
from piso_wifi.models.entities import User
from piso_wifi.models.enums import PlanType, UserStatus
from piso_wifi.web import create_app
from piso_wifi.web.auth import admin_required, is_admin


@pytest.fixture
def mock_user_service():
    """Mock UserService for testing web routes."""
    mock = MagicMock()
    mock.check_balance.return_value = 60.0
    mock.add_time.return_value = True
    mock.deduct_time.return_value = True
    mock.set_bandwidth.return_value = True
    mock.request_upgrade.return_value = True
    mock.manage_plan.return_value = True
    mock.get_user_info.return_value = User(
        id=1,
        mac_address="AA:BB:CC:DD:EE:01",
        time_balance=60.0,
        status=UserStatus.ACTIVE,
        download_limit=2048,
        upload_limit=1024,
        plan=PlanType.DEFAULT,
        upgrade_requested=False,
    )
    return mock


@pytest.fixture
def mock_network_controller():
    """Mock NetworkController for testing web routes."""
    mock = MagicMock()
    mock.DEFAULT_DOWNLOAD_SPEED = 2048
    mock.DEFAULT_UPLOAD_SPEED = 1024
    mock.PREMIUM_DOWNLOAD_SPEED = 8096
    mock.PREMIUM_UPLOAD_SPEED = 8096
    mock.ap_interface = "wlan0"
    mock.internet_interface = "wlan1"
    mock.get_connected_devices.return_value = [
        {
            "mac_address": "AA:BB:CC:DD:EE:01",
            "ip": "192.168.4.2",
            "hostname": "test-device",
            "signal": "-45 dBm",
            "connected": True,
        }
    ]
    mock.get_uplink_status.return_value = {
        "interface": "wlan1",
        "connection_type": "Wireless Uplink",
        "status": "Online",
        "carrier_status": "Active",
        "ip_address": "192.168.1.50",
        "gateway": "192.168.1.1",
    }
    mock.unblock_mac.return_value = True
    mock.block_mac.return_value = True
    mock.set_bandwidth_limit.return_value = True
    mock.remove_bandwidth_limit.return_value = True
    mock._execute_command.return_value = "mock_command_output"
    return mock


@pytest.fixture
def test_config():
    """Application config tailored for testing."""
    return AppConfig(
        secret_key="test-secret-key",
        admin_username="testadmin",
        admin_password="testpassword123",
    )


@pytest.fixture
def app(test_config, mock_user_service, mock_network_controller):
    """Flask application fixture with injected mocked dependencies."""
    application = create_app(
        config=test_config,
        user_service=mock_user_service,
        network_controller=mock_network_controller,
    )
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ============================================================================
# Dashboard Route Tests: /
# ============================================================================


def test_index_unauthenticated(client, mock_network_controller, mock_user_service):
    """Test index route renders device table and admin login option."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Connected Devices" in html
    assert "AA:BB:CC:DD:EE:01" in html
    assert "192.168.4.2" in html
    assert "Admin Login" in html
    assert "Logout" not in html
    mock_network_controller.get_connected_devices.assert_called_once()
    mock_user_service.get_user_info.assert_called_with("AA:BB:CC:DD:EE:01")


def test_index_as_admin(client):
    """Test index route shows admin controls and logout when authenticated."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Logout" in html
    assert "Update Plan" in html


def test_index_with_empty_devices(client, mock_network_controller):
    """Test index route renders properly when no devices are connected."""
    mock_network_controller.get_connected_devices.return_value = []
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Connected Devices" in html


def test_index_renders_uplink_info(client, mock_network_controller):
    """Test index route renders WAN uplink status card with diagnostics."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "WAN Uplink Diagnostics" in html
    assert "wlan1" in html
    assert "Wireless Uplink" in html
    assert "192.168.1.50" in html
    assert "192.168.1.1" in html
    assert "Online" in html
    mock_network_controller.get_uplink_status.assert_called_once()


def test_index_uplink_fallback_on_error(client, mock_network_controller):
    """Test index route gracefully renders fallback when get_uplink_status fails."""
    mock_network_controller.get_uplink_status.side_effect = RuntimeError("Failed to detect uplink")
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "WAN Uplink Diagnostics" in html
    assert "Connected Devices" in html



# ============================================================================
# Authentication Route Tests: /login and /logout
# ============================================================================


def test_login_page_get(client):
    """Test GET /login renders login template."""
    response = client.get("/login")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Admin Login" in html
    assert 'name="username"' in html
    assert 'name="password"' in html


def test_login_post_success(client, test_config):
    """Test POST /login with correct credentials establishes admin session."""
    response = client.post(
        "/login",
        data={
            "username": test_config.admin_username,
            "password": test_config.admin_password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    with client.session_transaction() as sess:
        assert sess.get("is_admin") is True


def test_login_post_failure(client):
    """Test POST /login with invalid credentials flashes error and does not set session."""
    response = client.post(
        "/login",
        data={
            "username": "wronguser",
            "password": "wrongpassword",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Invalid credentials" in html

    with client.session_transaction() as sess:
        assert sess.get("is_admin") is not True


def test_logout(client):
    """Test GET /logout clears admin session and redirects."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    with client.session_transaction() as sess:
        assert sess.get("is_admin") is None


# ============================================================================
# Time Management Route Tests: /add_time and /deduct_time
# ============================================================================


def test_add_time_success(client, mock_user_service, mock_network_controller):
    """Test adding time successfully unblocks device and redirects."""
    mac = "AA:BB:CC:DD:EE:01"
    response = client.post(
        "/add_time",
        data={"mac_address": mac, "amount": "10"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    mock_user_service.add_time.assert_called_once_with(mac, 10.0)
    mock_network_controller.unblock_mac.assert_called_once_with(mac)


def test_add_time_invalid_amount(client, mock_user_service, mock_network_controller):
    """Test add_time with non-positive or invalid amount."""
    response = client.post(
        "/add_time",
        data={"mac_address": "AA:BB:CC:DD:EE:01", "amount": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.add_time.assert_not_called()
    mock_network_controller.unblock_mac.assert_not_called()


def test_add_time_missing_mac(client, mock_user_service):
    """Test add_time with missing MAC address."""
    response = client.post(
        "/add_time",
        data={"amount": "10"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.add_time.assert_not_called()


def test_deduct_time_success_with_remaining_balance(
    client, mock_user_service, mock_network_controller
):
    """Test deduct_time when balance remains positive does not block device."""
    mac = "AA:BB:CC:DD:EE:01"
    mock_user_service.check_balance.return_value = 25.0

    response = client.post(
        "/deduct_time",
        data={"mac_address": mac, "minutes": "5"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.deduct_time.assert_called_once_with(mac, 5.0, manual=True)
    mock_network_controller.block_mac.assert_not_called()


def test_deduct_time_depletes_balance_blocks_device(
    client, mock_user_service, mock_network_controller
):
    """Test deduct_time when balance reaches zero blocks the MAC address."""
    mac = "AA:BB:CC:DD:EE:01"
    mock_user_service.check_balance.return_value = 0.0

    response = client.post(
        "/deduct_time",
        data={"mac_address": mac, "minutes": "30"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.deduct_time.assert_called_once_with(mac, 30.0, manual=True)
    mock_network_controller.block_mac.assert_called_once_with(mac)


def test_deduct_time_invalid_minutes(client, mock_user_service, mock_network_controller):
    """Test deduct_time with invalid minutes is rejected."""
    response = client.post(
        "/deduct_time",
        data={"mac_address": "AA:BB:CC:DD:EE:01", "minutes": "-5"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.deduct_time.assert_not_called()
    mock_network_controller.block_mac.assert_not_called()


# ============================================================================
# Bandwidth and Plan Management Route Tests
# ============================================================================


def test_set_bandwidth_success(client, mock_user_service, mock_network_controller):
    """Test set_bandwidth validates bounds and updates service and controller."""
    mac = "AA:BB:CC:DD:EE:01"
    response = client.post(
        "/set_bandwidth",
        data={"mac_address": mac, "download": "3000", "upload": "1500"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.set_bandwidth.assert_called_once_with(mac, 3000, 1500)
    mock_network_controller.set_bandwidth_limit.assert_called_once_with(mac, 3000, 1500)


def test_set_bandwidth_out_of_bounds(client, mock_user_service, mock_network_controller):
    """Test set_bandwidth rejects values below 32 kbps or above 100000 kbps."""
    # Below minimum
    response = client.post(
        "/set_bandwidth",
        data={"mac_address": "AA:BB:CC:DD:EE:01", "download": "16", "upload": "1024"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.set_bandwidth.assert_not_called()

    # Above maximum
    response2 = client.post(
        "/set_bandwidth",
        data={"mac_address": "AA:BB:CC:DD:EE:01", "download": "200000", "upload": "1024"},
        follow_redirects=False,
    )
    assert response2.status_code == 302
    mock_user_service.set_bandwidth.assert_not_called()


def test_request_upgrade(client, mock_user_service):
    """Test request_upgrade updates user record."""
    mac = "AA:BB:CC:DD:EE:01"
    response = client.post(
        "/request_upgrade",
        data={"mac_address": mac},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.request_upgrade.assert_called_once_with(mac)


def test_manage_plan_unauthorized(client, mock_user_service, mock_network_controller):
    """Test manage_plan requires admin privileges."""
    response = client.post(
        "/manage_plan",
        data={"mac_address": "AA:BB:CC:DD:EE:01", "plan": "premium"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.manage_plan.assert_not_called()
    mock_network_controller.set_bandwidth_limit.assert_not_called()


def test_manage_plan_authorized_to_premium(
    client, mock_user_service, mock_network_controller
):
    """Test manage_plan by admin upgrades to premium speeds."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    mac = "AA:BB:CC:DD:EE:01"
    response = client.post(
        "/manage_plan",
        data={"mac_address": mac, "plan": "premium"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_network_controller.remove_bandwidth_limit.assert_called_once_with(mac)
    mock_user_service.manage_plan.assert_called_once_with(
        mac, "premium", mock_network_controller.PREMIUM_DOWNLOAD_SPEED, mock_network_controller.PREMIUM_UPLOAD_SPEED
    )
    mock_network_controller.set_bandwidth_limit.assert_called_once_with(
        mac, mock_network_controller.PREMIUM_DOWNLOAD_SPEED, mock_network_controller.PREMIUM_UPLOAD_SPEED
    )


def test_manage_plan_already_on_plan(client, mock_user_service, mock_network_controller):
    """Test manage_plan when device is already on requested plan."""
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    mac = "AA:BB:CC:DD:EE:01"
    # user_info already has plan=DEFAULT in fixture
    response = client.post(
        "/manage_plan",
        data={"mac_address": mac, "plan": "default"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    mock_user_service.manage_plan.assert_not_called()


# ============================================================================
# Debug Route Tests: /debug/connections
# ============================================================================


def test_debug_connections(client, mock_network_controller):
    """Test debug connections endpoint returns JSON status of network components."""
    response = client.get("/debug/connections")
    assert response.status_code == 200
    assert response.is_json
    data = response.get_json()

    assert "connected_devices" in data
    assert "ap_interface_status" in data
    assert "internet_interface_status" in data
    assert "hostapd_status" in data
    assert "iptables_rules" in data
    assert "uplink_status" in data
    assert data["uplink_status"]["interface"] == "wlan1"
    assert data["uplink_status"]["status"] == "Online"
    assert len(data["connected_devices"]) == 1
    assert data["connected_devices"][0]["mac_address"] == "AA:BB:CC:DD:EE:01"


# ============================================================================
# Captive Portal Detection Route Tests
# ============================================================================


def test_captive_portal_generate_204_redirect(client):
    """Test Android /generate_204 endpoint redirects to dashboard index."""
    response = client.get("/generate_204", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_captive_portal_gen_204_redirect(client):
    """Test Android /gen_204 alias endpoint redirects to dashboard index."""
    response = client.get("/gen_204", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_captive_portal_hotspot_detect_redirect(client):
    """Test Apple CNA /hotspot-detect.html endpoint redirects to dashboard index."""
    response = client.get("/hotspot-detect.html", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_captive_portal_connecttest_redirect(client):
    """Test Windows NCSI /connecttest.txt endpoint redirects to dashboard index."""
    response = client.get("/connecttest.txt", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_captive_portal_ncsi_redirect(client):
    """Test Windows NCSI /ncsi.txt endpoint redirects to dashboard index."""
    response = client.get("/ncsi.txt", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


# ============================================================================
# Decorator and Helper Unit Tests
# ============================================================================


def test_admin_required_and_is_admin_helpers(app):
    """Directly test is_admin helper and admin_required decorator behavior."""
    @admin_required
    def sample_admin_view():
        return "admin_ok"

    with app.test_request_context():
        # Non-admin
        assert is_admin() is False
        res = sample_admin_view()
        assert res.status_code == 302

        # Admin
        session["is_admin"] = True
        assert is_admin() is True
        assert sample_admin_view() == "admin_ok"


def test_login_rate_limiting_and_lockout(client):
    """Test that failed login attempts are rate limited and trigger 429 lockout."""
    # 4 failed attempts should return 200 with invalid credentials
    for _ in range(4):
        res = client.post("/login", data={"username": "admin", "password": "wrongpassword"})
        assert res.status_code == 200
        assert "Invalid credentials" in res.get_data(as_text=True)

    # 5th failed attempt triggers lockout
    res5 = client.post("/login", data={"username": "admin", "password": "wrongpassword"})
    assert res5.status_code == 200

    # Next attempt should be blocked with 429
    res6 = client.post("/login", data={"username": "admin", "password": "wrongpassword"})
    assert res6.status_code == 429
    assert "Locked out" in res6.get_data(as_text=True)
