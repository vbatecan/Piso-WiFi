import time
import pytest
from unittest.mock import MagicMock

from piso_wifi.services.time_service import TimeService, TimeManager
from piso_wifi.models.entities import DeviceInfo


@pytest.fixture
def mock_user_service():
    service = MagicMock()
    service.check_balance.return_value = 10.0
    service.deduct_time.return_value = True
    return service


@pytest.fixture
def mock_network_controller():
    controller = MagicMock()
    controller.get_connected_devices.return_value = []
    controller.block_mac.return_value = True
    return controller


@pytest.fixture
def time_service(mock_user_service, mock_network_controller):
    return TimeService(
        user_service=mock_user_service,
        network_controller=mock_network_controller,
        check_interval=1,
    )


def test_init_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "default_test.db"))
    ts = TimeService()
    assert ts.user_service is not None
    assert ts.user_manager is ts.user_service
    assert ts.network_controller is not None
    assert ts.check_interval == 5
    assert not ts.is_running
    assert not ts.running
    assert TimeManager is TimeService


def test_init_with_dependency_injection(mock_user_service, mock_network_controller):
    ts = TimeService(
        user_service=mock_user_service,
        network_controller=mock_network_controller,
        check_interval=3,
    )
    assert ts.user_service is mock_user_service
    assert ts.network_controller is mock_network_controller
    assert ts.check_interval == 3


def test_start_and_stop_lifecycle(time_service):
    assert not time_service.is_running

    time_service.start()
    assert time_service.is_running
    assert time_service.running

    # Second start is a no-op
    time_service.start()
    assert time_service.is_running

    time_service.stop(timeout=1.0)
    assert not time_service.is_running
    assert not time_service.running


def test_device_with_zero_or_negative_balance_blocked(
    time_service, mock_network_controller, mock_user_service
):
    mac = "00:11:22:33:44:55"
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": mac, "ip": "192.168.4.2"}
    ]
    mock_user_service.check_balance.return_value = 0.0

    time_service._check_and_deduct_time()

    mock_network_controller.block_mac.assert_called_once_with(mac)
    assert mac not in time_service.last_deduction


def test_device_with_positive_balance_first_seen(
    time_service, mock_network_controller, mock_user_service
):
    mac = "00:11:22:33:44:56"
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": mac, "ip": "192.168.4.3"}
    ]
    mock_user_service.check_balance.return_value = 15.0

    time_service._check_and_deduct_time()

    # Tracked in last_deduction
    assert mac in time_service.last_deduction
    # No deduction on first tick
    mock_user_service.deduct_time.assert_not_called()
    mock_network_controller.block_mac.assert_not_called()


def test_device_with_elapsed_time_deducted(
    time_service, mock_network_controller, mock_user_service
):
    mac = "00:11:22:33:44:57"
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": mac, "ip": "192.168.4.4"}
    ]
    # Set last deduction to 70 seconds ago (1.16 min)
    now = time.time()
    time_service.last_deduction[mac] = now - 70

    mock_user_service.check_balance.side_effect = [10.0, 9.0]
    mock_user_service.deduct_time.return_value = True

    time_service._check_and_deduct_time()

    mock_user_service.deduct_time.assert_called_once_with(mac, 1)
    mock_network_controller.block_mac.assert_not_called()
    assert time_service.last_deduction[mac] >= now


def test_device_multiple_minutes_elapsed(
    time_service, mock_network_controller, mock_user_service
):
    mac = "00:11:22:33:44:58"
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": mac}
    ]
    # 185 seconds ago = 3.08 minutes
    now = time.time()
    time_service.last_deduction[mac] = now - 185

    mock_user_service.check_balance.side_effect = [10.0, 7.0]
    mock_user_service.deduct_time.return_value = True

    time_service._check_and_deduct_time()

    mock_user_service.deduct_time.assert_called_once_with(mac, 3)
    mock_network_controller.block_mac.assert_not_called()


def test_device_depleted_after_deduction_blocked(
    time_service, mock_network_controller, mock_user_service
):
    mac = "00:11:22:33:44:59"
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": mac}
    ]
    now = time.time()
    time_service.last_deduction[mac] = now - 65

    # Balance 1.0 initially, drops to 0.0 after deducting 1 minute
    mock_user_service.check_balance.side_effect = [1.0, 0.0]
    mock_user_service.deduct_time.return_value = True

    time_service._check_and_deduct_time()

    mock_user_service.deduct_time.assert_called_once_with(mac, 1)
    mock_network_controller.block_mac.assert_called_once_with(mac)
    assert mac not in time_service.last_deduction


def test_disconnected_device_cleaned_up(time_service, mock_network_controller):
    mac1 = "AA:BB:CC:DD:EE:01"
    mac2 = "AA:BB:CC:DD:EE:02"

    time_service.last_deduction[mac1] = time.time()
    time_service.last_deduction[mac2] = time.time()

    # Only mac1 is currently connected
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": mac1}
    ]

    time_service._check_and_deduct_time()

    assert mac1 in time_service.last_deduction
    assert mac2 not in time_service.last_deduction


def test_device_info_dataclass_support(
    time_service, mock_network_controller, mock_user_service
):
    device = DeviceInfo(mac_address="11:22:33:44:55:66", ip="192.168.4.10")
    mock_network_controller.get_connected_devices.return_value = [device]
    mock_user_service.check_balance.return_value = 5.0

    time_service._check_and_deduct_time()

    assert "11:22:33:44:55:66" in time_service.last_deduction


def test_error_resilience(
    time_service, mock_network_controller, mock_user_service
):
    # If get_connected_devices raises, no crash
    mock_network_controller.get_connected_devices.side_effect = RuntimeError("Network error")
    time_service._check_and_deduct_time()

    # If check_balance raises for one device, other device proceeds
    mock_network_controller.get_connected_devices.side_effect = None
    mock_network_controller.get_connected_devices.return_value = [
        {"mac_address": "DEV:01"},
        {"mac_address": "DEV:02"},
    ]

    def balance_side_effect(mac):
        if mac == "DEV:01":
            raise ValueError("Corrupt balance")
        return 10.0

    mock_user_service.check_balance.side_effect = balance_side_effect
    time_service._check_and_deduct_time()

    assert "DEV:01" not in time_service.last_deduction
    assert "DEV:02" in time_service.last_deduction
