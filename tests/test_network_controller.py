"""Tests for NetworkController facade and backward compatibility."""

from unittest.mock import MagicMock, patch
import pytest

from network_controller import NetworkController
from piso_wifi.config import NetworkConfig
from piso_wifi.network.command_runner import MockCommandRunner


@pytest.fixture
def network_controller():
    """Create a test NetworkController without triggering system commands or root checks."""
    return NetworkController(auto_start=False, skip_system_checks=True)


def test_controller_constants(network_controller):
    """Ensure speed plan constants exist and match expectations."""
    assert network_controller.DEFAULT_DOWNLOAD_SPEED == 2048
    assert network_controller.DEFAULT_UPLOAD_SPEED == 1024
    assert network_controller.PREMIUM_DOWNLOAD_SPEED == 8096
    assert network_controller.PREMIUM_UPLOAD_SPEED == 8096


def test_block_mac(network_controller):
    """Test blocking a MAC address using iptables."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        assert network_controller.block_mac("00:11:22:33:44:55") is True
        assert mock_run.called


def test_unblock_mac(network_controller):
    """Test unblocking a MAC address using iptables."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        assert network_controller.unblock_mac("00:11:22:33:44:55") is True
        assert mock_run.called


def test_get_connected_devices(network_controller):
    """Test connected devices parsing from ARP cache fallback."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "? (192.168.1.1) at 00:11:22:33:44:55 [ether]"
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        devices = network_controller.get_connected_devices()
        assert len(devices) == 1
        assert "00:11:22:33:44:55" in devices[0]
        assert devices[0]["mac_address"] == "00:11:22:33:44:55"
        assert devices[0]["ip"] == "192.168.1.1"


def test_execute_command_compatibility(network_controller):
    """Test _execute_command compatibility method."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "hello world"
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        out = network_controller._execute_command("echo hello world")
        assert out == "hello world"


def test_set_and_remove_bandwidth_limits():
    """Test setting and removing bandwidth limits with mock runner."""
    mock_runner = MockCommandRunner()
    mock_runner.register_response("arp -n", stdout="192.168.4.55 0x1 0x2 00:11:22:33:44:55 * wlan0\n")

    ctrl = NetworkController(runner=mock_runner, auto_start=False)
    success = ctrl.set_bandwidth_limit("00:11:22:33:44:55", download_kbps=4096, upload_kbps=2048)
    assert success is True
    assert any("tc class add" in cmd for cmd in mock_runner.executed_commands)
    assert any("4096kbit" in cmd for cmd in mock_runner.executed_commands)

    mock_runner.clear()
    mock_runner.register_response("arp -n", stdout="192.168.4.55 0x1 0x2 00:11:22:33:44:55 * wlan0\n")
    removed = ctrl.remove_bandwidth_limit("00:11:22:33:44:55")
    assert removed is True
    assert any("tc class del" in cmd for cmd in mock_runner.executed_commands)


def test_start_and_stop_ap():
    """Test starting and stopping access point via mock runner."""
    mock_runner = MockCommandRunner()
    ctrl = NetworkController(runner=mock_runner, auto_start=False)

    ctrl.start_ap()
    assert any("hostapd" in cmd for cmd in mock_runner.executed_commands)
    assert any("MASQUERADE" in cmd for cmd in mock_runner.executed_commands)
    assert any("tc qdisc add" in cmd for cmd in mock_runner.executed_commands)

    mock_runner.clear()
    ctrl.stop_ap()
    assert any("systemctl stop hostapd" in cmd for cmd in mock_runner.executed_commands)