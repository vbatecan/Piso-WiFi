"""Comprehensive unit tests for the modular network subsystem.

Tests CommandRunner, FirewallManager, TrafficShaper, AccessPointManager,
DeviceDiscovery, and NetworkController using MockCommandRunner without root or real hardware.
"""

import os
import subprocess
import tempfile
import time
from unittest.mock import patch
import pytest

from piso_wifi.config import NetworkConfig
from piso_wifi.network.access_point import AccessPointManager
from piso_wifi.network.command_runner import (
    CommandResult,
    CommandRunner,
    MockCommandRunner,
    SystemCommandRunner,
)
from piso_wifi.network.controller import NetworkController
from piso_wifi.network.discovery import DeviceDiscovery, DeviceDict
from piso_wifi.network.firewall import FirewallManager
from piso_wifi.network.qos import QoSManager, TrafficShaper


# ==========================================
# 1. CommandRunner Tests
# ==========================================

def test_command_result_properties():
    res = CommandResult(
        command="test-cmd",
        returncode=0,
        stdout="hello world\n",
        stderr="",
    )
    assert res.success is True
    assert str(res) == "hello world\n"

    err_res = CommandResult(
        command="fail-cmd",
        returncode=1,
        stdout="",
        stderr="error occurred",
    )
    assert err_res.success is False


def test_mock_command_runner_responses():
    runner = MockCommandRunner(default_stdout="default-out")
    
    # 1. Default response
    res = runner.run("sample command")
    assert res.stdout == "default-out"
    assert "sample command" in runner.executed_commands

    # 2. Registered pattern response
    runner.register_response("special", stdout="custom-special", returncode=0)
    res2 = runner.run("run special operation")
    assert res2.stdout == "custom-special"

    # 3. Dynamic handler
    def custom_handler(cmd: str):
        if "dynamic" in cmd:
            return CommandResult(cmd, returncode=0, stdout="dynamic-result", stderr="")
        return None

    runner.register_handler(custom_handler)
    res3 = runner.run("dynamic command")
    assert res3.stdout == "dynamic-result"

    # 4. Error simulation
    runner.register_response("error-cmd", returncode=2, stderr="failed badly")
    with pytest.raises(subprocess.CalledProcessError):
        runner.run("error-cmd")

    # Error ignored with ignore_errors=True
    res_ignored = runner.run("error-cmd", ignore_errors=True)
    assert res_ignored.returncode == 2

    # Clear
    runner.clear()
    assert len(runner.executed_commands) == 0


def test_system_command_runner():
    runner = SystemCommandRunner()
    # Simple echo
    res = runner.run("echo 'hello pytest'")
    assert res.success is True
    assert "hello pytest" in res.stdout

    # Execute string return
    output = runner.execute("echo 'direct output'")
    assert "direct output" in output

    # Command exists
    assert runner.command_exists("sh") is True
    assert runner.command_exists("nonexistent_binary_xyz_123") is False

    # Ignore errors
    fail_res = runner.run("false", ignore_errors=True)
    assert fail_res.returncode != 0

    # Raise on error
    with pytest.raises(subprocess.CalledProcessError):
        runner.run("false", ignore_errors=False)


# ==========================================
# 2. FirewallManager Tests
# ==========================================

def test_firewall_setup_nat():
    runner = MockCommandRunner()
    fw = FirewallManager(runner=runner)

    fw.setup_nat_and_forwarding(
        ap_iface="wlan0", internet_iface="eth0", ap_ip="192.168.4.1"
    )

    cmds = runner.executed_commands
    assert any("ip_forward" in c for c in cmds)
    assert any("iptables -t nat -F" in c for c in cmds)
    assert any("iptables -P FORWARD DROP" in c for c in cmds)
    assert any("POSTROUTING -o eth0 -j MASQUERADE" in c for c in cmds)
    assert any("POSTROUTING ! -o wlan0 -j MASQUERADE" in c for c in cmds)
    assert any("-i wlan0 -p udp --dport 53 -j ACCEPT" in c for c in cmds)
    assert any("-i wlan0 -p udp --dport 67:68 -j ACCEPT" in c for c in cmds)
    assert any("-i wlan0 -p tcp --dport 5000 -j ACCEPT" in c for c in cmds)
    assert any("-i wlan0 -d 192.168.4.1 -j ACCEPT" in c for c in cmds)
    assert any("-i wlan0 -j DROP" in c for c in cmds)


def test_firewall_captive_portal_rules():
    runner = MockCommandRunner()
    fw = FirewallManager(runner=runner)

    fw.setup_captive_portal_rules("wlan0", "192.168.4.1", 5000)
    cmds = runner.executed_commands
    assert any("PREROUTING -i wlan0 -p tcp --dport 80 -j DNAT --to-destination 192.168.4.1:5000" in c for c in cmds)


def test_firewall_interface_agnostic_nat_with_auto():
    runner = MockCommandRunner()
    fw = FirewallManager(runner=runner)

    fw.setup_nat_and_forwarding(
        ap_iface="wlan0", internet_iface="auto", ap_ip="192.168.4.1"
    )

    cmds = runner.executed_commands
    # Interface-agnostic rule is present
    assert any("POSTROUTING ! -o wlan0 -j MASQUERADE" in c for c in cmds)
    # Does not create rule for 'auto'
    assert not any("POSTROUTING -o auto -j MASQUERADE" in c for c in cmds)
    assert any("-i wlan0 -p tcp --dport 5000 -j ACCEPT" in c for c in cmds)


def test_firewall_block_and_unblock_mac():
    runner = MockCommandRunner()
    fw = FirewallManager(runner=runner)

    # Block
    assert fw.block_mac("AA:BB:CC:DD:EE:FF") is True
    assert any("iptables -I FORWARD 1 -m mac --mac-source AA:BB:CC:DD:EE:FF -j DROP" in c for c in runner.executed_commands)

    # Unblock
    runner.clear()
    assert fw.unblock_mac("AA:BB:CC:DD:EE:FF") is True
    assert any("iptables -I FORWARD 1 -m mac --mac-source AA:BB:CC:DD:EE:FF -j ACCEPT" in c for c in runner.executed_commands)


def test_firewall_allow_local_traffic_and_dump():
    runner = MockCommandRunner()
    fw = FirewallManager(runner=runner)

    fw.allow_local_traffic("wlan0", "192.168.4.1")
    assert any("192.168.4.1" in c for c in runner.executed_commands)

    runner.register_response("iptables-save", stdout="# Generated by iptables-save")
    dump = fw.dump_iptables()
    assert "# Generated by iptables-save" in dump


# ==========================================
# 3. TrafficShaper Tests
# ==========================================

def test_traffic_shaper_qos_setup():
    runner = MockCommandRunner()
    shaper = TrafficShaper(runner=runner)

    shaper.setup_qos(ap_iface="wlan0", default_download=2048)

    cmds = runner.executed_commands
    assert any("tc qdisc add dev wlan0 root handle 1: htb default 10" in c for c in cmds)
    assert any("tc class add dev wlan0 parent 1: classid 1:1 htb rate 100mbit" in c for c in cmds)
    assert any("classid 1:10 htb rate 2048kbit" in c for c in cmds)
    assert any("tc qdisc add dev wlan0 ingress" in c for c in cmds)


def test_traffic_shaper_bandwidth_limits():
    runner = MockCommandRunner()
    shaper = TrafficShaper(runner=runner)
    assert QoSManager is TrafficShaper

    mac = "00:11:22:33:44:55"
    ip = "192.168.4.10"
    class_id = TrafficShaper.calculate_class_id(mac)
    assert 20 <= class_id <= 1019

    # Set bandwidth limit
    ok = shaper.set_bandwidth_limit("wlan0", mac, ip, 4096, 1024)
    assert ok is True

    cmds = runner.executed_commands
    assert any(f"classid 1:{class_id} htb rate 4096kbit" in c for c in cmds)
    assert any(f"handle {class_id}: sfq perturb 10" in c for c in cmds)
    assert any(f"match ip dst {ip} flowid 1:{class_id}" in c for c in cmds)
    assert any(f"match ip src {ip} police rate 1024kbit" in c for c in cmds)
    assert any(f"iptables -A FORWARD -s {ip} -j ACCEPT" in c for c in cmds)

    # Remove bandwidth limit
    runner.clear()
    rem_ok = shaper.remove_bandwidth_limit("wlan0", mac, ip)
    assert rem_ok is True
    assert any(f"tc class del dev wlan0 classid 1:{class_id}" in c for c in runner.executed_commands)
    assert any(f"iptables -D FORWARD -s {ip} -j ACCEPT" in c for c in runner.executed_commands)


# ==========================================
# 4. AccessPointManager Tests
# ==========================================

def test_access_point_configure_ap():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    with tempfile.TemporaryDirectory() as tmpdir:
        hostapd_path = os.path.join(tmpdir, "hostapd.conf")
        dnsmasq_path = os.path.join(tmpdir, "dnsmasq.conf")

        config = NetworkConfig(
            ap_interface="wlan0",
            ssid="TestPisoHotspot",
            ip="192.168.4.1",
            hostapd_conf=hostapd_path,
            dnsmasq_conf=dnsmasq_path,
        )

        ap_mgr.configure_ap(config)

        assert os.path.exists(hostapd_path)
        assert os.path.exists(dnsmasq_path)

        with open(hostapd_path, "r", encoding="utf-8") as f:
            h_content = f.read()
            assert "ssid=TestPisoHotspot" in h_content
            assert "interface=wlan0" in h_content

        with open(dnsmasq_path, "r", encoding="utf-8") as f:
            d_content = f.read()
            assert "dhcp-range=" in d_content
            assert "interface=wlan0" in d_content


def test_access_point_start_and_stop():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    config = NetworkConfig(
        ap_interface="wlan0",
        internet_interface="eth0",
        ip="192.168.4.1",
    )

    ap_mgr.start_ap(config)
    cmds = runner.executed_commands
    assert any("killall hostapd" in c for c in cmds)
    assert any("nmcli device set wlan0 managed no" in c for c in cmds)
    assert any("ip addr add 192.168.4.1/24 dev wlan0" in c for c in cmds)
    assert any("hostapd -B" in c for c in cmds)
    assert any("systemctl restart dnsmasq" in c for c in cmds)

    runner.clear()
    ap_mgr.stop_ap("wlan0", "eth0")
    cmds_stop = runner.executed_commands
    assert any("systemctl stop hostapd" in c for c in cmds_stop)
    assert any("systemctl stop dnsmasq" in c for c in cmds_stop)
    assert any("nmcli device set wlan0 managed yes" in c for c in cmds_stop)
    assert any("nmcli device set eth0 managed yes" in c for c in cmds_stop)


def test_access_point_check_status_and_debug():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    # Status succeeds when hostapd, IP, and dnsmasq match
    runner.register_response("ps aux | grep '[h]ostapd'", stdout="root 1234 /usr/sbin/hostapd\n")
    runner.register_response("ip addr show wlan0", stdout="3: wlan0: <BROADCAST,MULTICAST,UP> inet 192.168.4.1/24\n")
    runner.register_response("systemctl status dnsmasq", stdout="Active: active (running)\n")

    assert ap_mgr.check_ap_status("wlan0", "192.168.4.1") is True

    # Fails if IP not found
    runner.register_response("ip addr show wlan0", stdout="3: wlan0: <BROADCAST> inet 10.0.0.1/24\n")
    assert ap_mgr.check_ap_status("wlan0", "192.168.4.1") is False

    # Debug dump
    dump = ap_mgr.dump_debug_info("wlan0")
    assert "=== Piso-WiFi Debug Information ===" in dump


def test_access_point_is_wireless():
    with patch("os.path.exists") as mock_exists:
        # wireless sysfs exists
        mock_exists.side_effect = lambda p: p == "/sys/class/net/wlan0/wireless"
        assert AccessPointManager.is_wireless("wlan0") is True

        # phy80211 exists
        mock_exists.side_effect = lambda p: p == "/sys/class/net/wlan1/phy80211"
        assert AccessPointManager.is_wireless("wlan1") is True

        # neither exists (e.g. wired eth0)
        mock_exists.side_effect = lambda p: False
        assert AccessPointManager.is_wireless("eth0") is False

        # empty interface
        assert AccessPointManager.is_wireless("") is False


def test_access_point_detect_default_uplink():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    # 1. Routing table returns eth0
    runner.register_response(
        "ip route show default",
        stdout="default via 192.168.1.1 dev eth0 proto dhcp metric 100\n",
    )
    assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "eth0"

    # 2. Routing table returns enp2s0
    runner.clear()
    runner.register_response(
        "ip route show default",
        stdout="default via 10.0.0.1 dev enp2s0 proto dhcp metric 100\n",
    )
    assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "enp2s0"

    # 3. Routing table has primary ap_iface (wlan0) and secondary wlan1
    runner.clear()
    runner.register_response(
        "ip route show default",
        stdout="default via 192.168.4.1 dev wlan0 proto dhcp metric 50\ndefault via 192.168.8.1 dev wlan1 proto dhcp metric 600\n",
    )
    assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "wlan1"

    # 4. Routing table default is empty, but 0.0.0.0/0 route returns end0
    runner.clear()
    runner.register_response("ip route show default", stdout="")
    runner.register_response(
        "ip route show 0.0.0.0/0",
        stdout="0.0.0.0/0 via 192.168.2.1 dev end0\n",
    )
    assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "end0"

    # 5. Route table empty, physical fallback checks:
    runner.clear()
    runner.register_response("ip route show default", stdout="")
    runner.register_response("ip route show 0.0.0.0/0", stdout="")

    with patch("os.path.exists") as mock_exists:
        # eth0 physically exists
        mock_exists.side_effect = lambda p: p == "/sys/class/net/eth0"
        assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "eth0"

        # end0 physically exists
        mock_exists.side_effect = lambda p: p == "/sys/class/net/end0"
        assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "end0"

        # wlan1 physically exists
        mock_exists.side_effect = lambda p: p == "/sys/class/net/wlan1"
        assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "wlan1"

        # None exist -> fallback to wlan1
        mock_exists.side_effect = lambda p: False
        assert ap_mgr.detect_default_uplink(ap_iface="wlan0") == "wlan1"


def test_access_point_get_uplink_info():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    runner.register_response(
        "ip link show eth0",
        stdout="2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> state UP\n",
    )
    runner.register_response(
        "ip -4 addr show eth0",
        stdout="inet 192.168.1.55/24 brd 192.168.1.255 scope global eth0\n",
    )
    runner.register_response(
        "ip route show default dev eth0",
        stdout="default via 192.168.1.1 dev eth0 metric 100\n",
    )

    with patch("os.path.exists") as mock_exists:
        mock_exists.return_value = False
        info = ap_mgr.get_uplink_info("eth0")
        assert info["interface"] == "eth0"
        assert info["is_wireless"] is False
        assert info["carrier"] is True
        assert info["ip"] == "192.168.1.55"
        assert info["gateway"] == "192.168.1.1"

    # Empty interface test
    empty_info = ap_mgr.get_uplink_info("")
    assert empty_info["interface"] == ""
    assert empty_info["carrier"] is False


# ==========================================
# 5. DeviceDiscovery Tests
# ==========================================

def test_device_discovery_mac_validation():
    assert DeviceDiscovery.is_valid_mac("00:11:22:33:44:55") is True
    assert DeviceDiscovery.is_valid_mac("AA:BB:CC:DD:EE:FF") is True
    assert DeviceDiscovery.is_valid_mac("00-11-22-33-44-55") is True
    assert DeviceDiscovery.is_valid_mac("invalid_mac") is False
    assert DeviceDiscovery.is_valid_mac("00:11:22:33:44") is False
    assert DeviceDiscovery.is_valid_mac("") is False


def test_device_discovery_dhcp_leases():
    discovery = DeviceDiscovery()

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        future_time = int(time.time()) + 3600
        f.write(f"{future_time} 00:11:22:33:44:55 192.168.4.100 my-phone 01:00:11:22:33:44:55\n")
        f.write(f"{future_time} AA:BB:CC:DD:EE:FF 192.168.1.50 other-net *\n")
        # Expired lease
        f.write(f"100000 11:22:33:44:55:66 192.168.4.101 expired-device *\n")
        leases_path = f.name

    try:
        leases = discovery.get_dhcp_leases(leases_path, subnet_prefix="192.168.4.")
        assert "00:11:22:33:44:55" in leases
        assert leases["00:11:22:33:44:55"]["ip"] == "192.168.4.100"
        assert leases["00:11:22:33:44:55"]["hostname"] == "my-phone"

        # Not in subnet
        assert "AA:BB:CC:DD:EE:FF" not in leases
        # Expired
        assert "11:22:33:44:55:66" not in leases
    finally:
        if os.path.exists(leases_path):
            os.remove(leases_path)


def test_device_discovery_station_dump_and_info():
    runner = MockCommandRunner()
    discovery = DeviceDiscovery(runner=runner)

    dump_output = """Station 00:11:22:33:44:55 (on wlan0)
    inactive time: 10 ms
Station AA:BB:CC:DD:EE:FF (on wlan0)
    inactive time: 50 ms
"""
    runner.register_response("iw dev wlan0 station dump", stdout=dump_output)

    macs = discovery.get_station_dump("wlan0")
    assert macs == ["00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF"]

    station_stats = """Station 00:11:22:33:44:55 (on wlan0):
    signal: -58 dBm
    rx bytes: 1048576
    tx bytes: 2097152
    connected time: 120 seconds
"""
    runner.register_response("iw dev wlan0 station get 00:11:22:33:44:55", stdout=station_stats)

    info = discovery.get_station_info("wlan0", "00:11:22:33:44:55")
    assert info["signal"] == "-58 dBm"
    assert info["rx_bytes"] == 1048576
    assert info["tx_bytes"] == 2097152
    assert info["connected_time"] == 120


def test_device_discovery_connected_devices():
    runner = MockCommandRunner()
    discovery = DeviceDiscovery(runner=runner)

    runner.register_response("iw dev wlan0 station dump", stdout="Station 00:11:22:33:44:55 (on wlan0)\n")
    runner.register_response("iw dev wlan0 station get 00:11:22:33:44:55", stdout="signal: -62 dBm\nrx bytes: 500\ntx bytes: 600\nconnected time: 30 seconds\n")
    runner.register_response("arp -n", stdout="192.168.4.15 0x1 0x2 00:11:22:33:44:55 * wlan0\n")

    devices = discovery.get_connected_devices("wlan0", leases_file="/nonexistent/leases")
    assert len(devices) == 1
    dev = devices[0]
    assert dev["mac_address"] == "00:11:22:33:44:55"
    assert dev["ip"] == "192.168.4.15"
    assert dev["signal"] == "-62 dBm"
    assert "00:11:22:33:44:55" in dev  # DeviceDict compatibility


# ==========================================
# 6. NetworkController Integration Tests
# ==========================================

def test_network_controller_client_flow():
    runner = MockCommandRunner()
    config = NetworkConfig(ap_interface="wlan0", internet_interface="eth0")
    ctrl = NetworkController(config=config, runner=runner, auto_start=False)

    # 1. Connected devices triggers default block for new MACs
    runner.register_response("iw dev wlan0 station dump", stdout="Station 00:11:22:33:44:55 (on wlan0)\n")
    runner.register_response("arp -n", stdout="192.168.4.20 0x1 0x2 00:11:22:33:44:55 * wlan0\n")

    devices = ctrl.get_connected_devices()
    assert len(devices) == 1
    assert "00:11:22:33:44:55" in ctrl.connected_devices
    # Automatic block rule was executed
    assert any("iptables -I FORWARD 1 -m mac --mac-source 00:11:22:33:44:55 -j DROP" in c for c in runner.executed_commands)

    # 2. Authenticating user unblocks MAC
    runner.clear()
    assert ctrl.unblock_mac("00:11:22:33:44:55") is True
    assert any("iptables -I FORWARD 1 -m mac --mac-source 00:11:22:33:44:55 -j ACCEPT" in c for c in runner.executed_commands)

    # 3. Apply bandwidth limits
    runner.clear()
    runner.register_response("arp -n", stdout="192.168.4.20 0x1 0x2 00:11:22:33:44:55 * wlan0\n")
    assert ctrl.set_bandwidth_limit("00:11:22:33:44:55", download_kbps=ctrl.PREMIUM_DOWNLOAD_SPEED, upload_kbps=ctrl.PREMIUM_UPLOAD_SPEED) is True
    assert any(f"{ctrl.PREMIUM_DOWNLOAD_SPEED}kbit" in c for c in runner.executed_commands)

    # 4. Remove limits
    runner.clear()
    runner.register_response("arp -n", stdout="192.168.4.20 0x1 0x2 00:11:22:33:44:55 * wlan0\n")
    assert ctrl.remove_bandwidth_limit("00:11:22:33:44:55") is True


def test_network_controller_auto_uplink_detection():
    runner = MockCommandRunner()
    # Mock default route output indicating eth0 is default gateway
    runner.register_response("ip route show default", stdout="default via 192.168.1.1 dev eth0 proto dhcp metric 100\n")

    config = NetworkConfig(ap_interface="wlan0", internet_interface="auto")
    ctrl = NetworkController(config=config, runner=runner, auto_start=False)

    assert ctrl.internet_interface == "eth0"
    assert "ip route show default" in runner.executed_commands


def test_network_controller_get_uplink_status():
    runner = MockCommandRunner()
    runner.register_response(
        "ip route show default",
        stdout="default via 192.168.1.1 dev eth0 proto dhcp metric 100\n",
    )
    runner.register_response(
        "ip link show eth0",
        stdout="2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> state UP\n",
    )
    runner.register_response(
        "ip -4 addr show eth0",
        stdout="inet 192.168.1.80/24 scope global eth0\n",
    )
    runner.register_response(
        "ip route show default dev eth0",
        stdout="default via 192.168.1.1 dev eth0\n",
    )

    config = NetworkConfig(ap_interface="wlan0", internet_interface="auto")
    ctrl = NetworkController(config=config, runner=runner, auto_start=False)

    with patch("os.path.exists") as mock_exists:
        mock_exists.return_value = False
        status = ctrl.get_uplink_status()
        assert status["interface"] == "eth0"
        assert status["carrier"] is True
        assert status["ip"] == "192.168.1.80"
        assert status["gateway"] == "192.168.1.1"
