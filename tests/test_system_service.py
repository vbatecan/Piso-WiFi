"""Unit tests for SystemService (hardware inspection, interfaces, and diagnostics)."""

import os
from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.network.command_runner import CommandResult
from piso_wifi.services.system_service import NetworkInterfaceInfo, SystemService


@pytest.fixture
def mock_runner():
    """Mock CommandRunner for deterministic system tests."""
    runner = MagicMock()
    # Default behavior for commands
    def execute_side_effect(cmd, ignore_errors=False, timeout=None):
        if "ip -br link show" in cmd:
            return "lo UNKNOWN 00:00:00:00:00:00\nwlan0 UP 60:ff:9e:3b:c7:3a\neth0 DOWN bc:fc:e7:01:e7:9e\n"
        if "ip route show default" in cmd:
            return "default via 192.168.1.1 dev eth0\n"
        if "iw list" in cmd:
            return "Supported interface modes:\n\t * IBSS\n\t * managed\n\t * AP\n\t * monitor\n"
        if "rfkill list wifi" in cmd:
            return "0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: no\n"
        if "systemctl is-active hostapd" in cmd:
            return "active\n"
        if "systemctl is-active dnsmasq" in cmd:
            return "active\n"
        if "iptables -t nat -L PREROUTING -n" in cmd:
            return "DNAT tcp -- 0.0.0.0/0 0.0.0.0/0 tcp dpt:80 to:192.168.4.1:5000\n"
        if "which" in cmd:
            bin_target = cmd.split()[-1]
            if bin_target == "hostapd_missing":
                return ""
            return "/usr/bin/" + bin_target
        return ""


    runner.execute.side_effect = execute_side_effect
    runner.run.return_value = CommandResult(
        command="test",
        returncode=0,
        stdout="3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = 12.1/15.4/18.2/2.1 ms",
        stderr="",
    )
    return runner


def test_interface_enumeration(mock_runner):
    """Test interface enumeration, wireless detection, and AP capability."""
    sys_svc = SystemService(runner=mock_runner)

    with patch("os.path.isdir", return_value=True), \
         patch("os.listdir", return_value=["lo", "wlan0", "eth0"]), \
         patch("piso_wifi.services.system_service.SystemService.is_wireless_interface", side_effect=lambda x: x == "wlan0"), \
         patch("os.path.exists", return_value=False):
        interfaces = sys_svc.list_interfaces()

    assert len(interfaces) >= 2
    iface_dict = {i.name: i for i in interfaces}

    assert "wlan0" in iface_dict
    assert iface_dict["wlan0"].is_wireless is True
    assert iface_dict["wlan0"].supports_ap is True

    assert "eth0" in iface_dict
    assert iface_dict["eth0"].is_wireless is False
    assert iface_dict["eth0"].supports_ap is False


def test_check_prerequisites_all_ok(mock_runner):
    """Test prerequisite check when all required dependencies are satisfied."""
    sys_svc = SystemService(runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/mocked"), \
         patch("os.geteuid", return_value=0), \
         patch.object(sys_svc, "list_interfaces", return_value=[
             NetworkInterfaceInfo(name="wlan0", is_wireless=True, supports_ap=True, operstate="up", carrier=True, mac_address="00:11:22:33:44:55")
         ]):
        res = sys_svc.check_prerequisites()

    assert res["ready"] is True
    assert res["is_root"] is True
    assert len(res["missing_binaries"]) == 0
    assert res["rfkill"]["blocked"] is False
    assert res["hardware"]["has_wireless"] is True
    assert res["hardware"]["has_ap_capable"] is True


def test_check_prerequisites_missing_binaries(mock_runner):
    """Test prerequisite check when binaries are missing."""
    sys_svc = SystemService(runner=mock_runner)

    def which_mock(bin_name):
        return None if bin_name == "hostapd" else f"/usr/bin/{bin_name}"

    orig_exec = mock_runner.execute.side_effect
    def custom_exec(cmd, ignore_errors=False, timeout=None):
        if "which hostapd" in cmd:
            return ""
        return orig_exec(cmd, ignore_errors=ignore_errors, timeout=timeout)

    mock_runner.execute.side_effect = custom_exec

    with patch("shutil.which", side_effect=which_mock), \
         patch("os.geteuid", return_value=0), \
         patch.object(sys_svc, "list_interfaces", return_value=[]):
        res = sys_svc.check_prerequisites()

    assert res["ready"] is False
    assert "hostapd" in res["missing_binaries"]
    assert any("hostapd" in issue for issue in res["issues"])



def test_run_ping_success(mock_runner):
    """Test successful ping execution and metrics extraction."""
    sys_svc = SystemService(runner=mock_runner)
    mock_runner.run.return_value = CommandResult(
        command="ping -c 3 -W 5 8.8.8.8",
        returncode=0,
        stdout="3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = 10.0/15.0/20.0/3.0 ms",
        stderr="",
    )

    result = sys_svc.run_ping(target="8.8.8.8", count=3)

    assert result["success"] is True
    assert result["packet_loss_pct"] == 0.0
    assert result["avg_latency_ms"] == 15.0
    assert result["target"] == "8.8.8.8"


def test_run_ping_invalid_target(mock_runner):
    """Test ping validation rejects shell injection attempts."""
    sys_svc = SystemService(runner=mock_runner)

    result = sys_svc.run_ping(target="8.8.8.8; rm -rf /")

    assert result["success"] is False
    assert "Invalid host" in result["error"]
    mock_runner.run.assert_not_called()


def test_run_dns_lookup(mock_runner):
    """Test DNS lookup diagnostic test."""
    sys_svc = SystemService(runner=mock_runner)

    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("142.250.190.46", 80))]):
        result = sys_svc.run_dns_lookup("google.com")

    assert result["success"] is True
    assert "142.250.190.46" in result["resolved_ips"]


def test_get_service_statuses(mock_runner):
    """Test service status query for hostapd, dnsmasq, and iptables."""
    sys_svc = SystemService(runner=mock_runner)

    statuses = sys_svc.get_service_statuses()

    assert statuses["hostapd"]["active"] is True
    assert statuses["dnsmasq"]["active"] is True
    assert statuses["iptables_nat"] is True


def test_restart_service(mock_runner):
    """Test restarting allowed daemon services."""
    sys_svc = SystemService(runner=mock_runner)
    mock_runner.run.return_value = CommandResult(command="systemctl restart hostapd", returncode=0, stdout="", stderr="")

    res = sys_svc.restart_service("hostapd")
    assert res["success"] is True
    assert res["service"] == "hostapd"

    # Disallowed service
    invalid_res = sys_svc.restart_service("evil_service")
    assert invalid_res["success"] is False
    assert "Invalid service" in invalid_res["error"]
