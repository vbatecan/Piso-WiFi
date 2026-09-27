"""Unit tests for Customer Captive Portal, Pause/Resume, and Voucher management."""

import json
from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.config import AppConfig
from piso_wifi.models.entities import User, Voucher
from piso_wifi.models.enums import UserStatus
from piso_wifi.services.time_service import TimeService
from piso_wifi.services.user_service import UserService
from piso_wifi.web import create_app


@pytest.fixture
def mock_user_service():
    """Mock UserService for portal testing."""
    service = MagicMock(spec=UserService)
    service.check_balance.return_value = 30.0
    service.get_user_info.return_value = User(
        mac_address="02:00:00:11:22:33",
        time_balance=30.0,
        status=UserStatus.ACTIVE,
    )
    service.pause_time.return_value = True
    service.resume_time.return_value = True
    service.redeem_voucher.return_value = {
        "success": True,
        "code": "TESTV123",
        "time_minutes": 60.0,
        "mac_address": "02:00:00:11:22:33",
    }
    service.create_vouchers.return_value = ["TESTV1", "TESTV2"]
    service.list_vouchers.return_value = [
        Voucher(code="TESTV1", time_minutes=60.0, is_used=False),
    ]
    return service


@pytest.fixture
def app_portal(mock_user_service):
    """Create Flask test client for captive portal."""
    cfg = AppConfig(setup_completed=True)
    app = create_app(
        config=cfg,
        user_service=mock_user_service,
        network_controller=MagicMock(),
    )
    app.config["TESTING"] = True
    return app


def test_index_renders_customer_portal_hero_card(app_portal):
    """Test that visiting / includes the mobile-first Customer Captive Portal Hero Card."""
    client = app_portal.test_client()
    response = client.get("/", headers={"X-Forwarded-For": "192.168.4.15", "X-Client-MAC": "02:00:00:11:22:33"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Remaining Internet Time Balance" in html
    assert "Insert Coin" in html
    assert "Redeem Voucher" in html
    assert "timeCountdown" in html


def test_api_portal_status(app_portal, mock_user_service):
    """Test GET /api/portal/status returns user status and time balance."""
    client = app_portal.test_client()
    res = client.get("/api/portal/status?mac_address=02:00:00:11:22:33")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["mac_address"] == "02:00:00:11:22:33"
    assert data["time_balance"] == 30.0
    assert data["status"] == "active"


def test_api_portal_pause_and_resume(app_portal, mock_user_service):
    """Test POST /api/portal/pause and POST /api/portal/resume."""
    client = app_portal.test_client()

    # Pause
    res = client.post("/api/portal/pause", json={"mac_address": "02:00:00:11:22:33"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["status"] == "paused"
    mock_user_service.pause_time.assert_called_once_with("02:00:00:11:22:33")

    # Resume
    res = client.post("/api/portal/resume", json={"mac_address": "02:00:00:11:22:33"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["status"] == "active"
    mock_user_service.resume_time.assert_called_once_with("02:00:00:11:22:33")


def test_api_portal_redeem_voucher(app_portal, mock_user_service):
    """Test POST /api/portal/redeem-voucher adds time balance."""
    client = app_portal.test_client()
    res = client.post(
        "/api/portal/redeem-voucher",
        json={"code": "TESTV123", "mac_address": "02:00:00:11:22:33"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["time_minutes"] == 60.0
    mock_user_service.redeem_voucher.assert_called_once_with(code="TESTV123", mac_address="02:00:00:11:22:33")


def test_api_vouchers_admin_generate_and_list(app_portal, mock_user_service):
    """Test admin voucher batch generation and listing."""
    client = app_portal.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    # Generate
    res = client.post("/api/vouchers/generate", json={"count": 2, "time_minutes": 120.0, "expires_days": 15})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert len(data["vouchers"]) == 2

    # List
    res = client.get("/api/vouchers/list")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert len(data["vouchers"]) == 1
    assert data["vouchers"][0]["code"] == "TESTV1"


def test_time_service_skips_paused_users():
    """Verify TimeService deduction skips paused users."""
    mock_user = MagicMock(spec=UserService)
    mock_user.check_balance.return_value = 50.0
    mock_user.get_user_info.return_value = User(
        mac_address="02:00:00:PA:US:ED",
        time_balance=50.0,
        status=UserStatus.PAUSED,
    )

    mock_net = MagicMock()
    mock_net.get_connected_devices.return_value = [{"mac_address": "02:00:00:PA:US:ED"}]

    time_svc = TimeService(user_service=mock_user, network_controller=mock_net, check_interval=1)
    time_svc._check_and_deduct_time()

    # Paused users should have network blocked and NO time deducted
    mock_net.block_mac.assert_called_with("02:00:00:PA:US:ED")
    mock_user.deduct_time.assert_not_called()
