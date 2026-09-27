"""Access point configuration and service lifecycle management.

Controls hostapd and dnsmasq lifecycle, wireless interface configuration,
system verification checks, and debugging diagnostics.
"""

import logging
import os
import re
from typing import Any, Dict, Optional

from piso_wifi.config import NetworkConfig
from piso_wifi.network.command_runner import CommandRunner, SystemCommandRunner

logger = logging.getLogger(__name__)


class AccessPointManager:
    """Manages hostapd, dnsmasq, and wireless interface state."""

    def __init__(self, runner: Optional[CommandRunner] = None):
        self.runner = runner or SystemCommandRunner()

    def verify_requirements(
        self, ap_iface: str, skip_root_check: bool = False
    ) -> None:
        """Verify operating system privileges, interface existence, and required CLI tools."""
        logger.info("Verifying system requirements for AP interface '%s'...", ap_iface)

        if not skip_root_check and hasattr(os, "geteuid") and os.geteuid() != 0:
            raise PermissionError("Must run as root to manage WiFi access point")

        # Check interface presence
        if not os.path.exists(f"/sys/class/net/{ap_iface}"):
            res = self.runner.run(f"ip link show {ap_iface}", ignore_errors=True)
            if not res.success:
                raise RuntimeError(f"Network interface '{ap_iface}' does not exist")

        # Verify essential binaries
        required_commands = ["hostapd", "dnsmasq", "iw", "ip", "iptables"]
        for cmd in required_commands:
            if not self.runner.command_exists(cmd):
                raise RuntimeError(
                    f"Required system command '{cmd}' not found on PATH"
                )

        # Verify AP mode capability via iw list
        try:
            iw_out = self.runner.execute("iw list", ignore_errors=True)
            if iw_out and "Supported interface modes" in iw_out:
                if "AP" not in iw_out:
                    raise RuntimeError(
                        f"Interface {ap_iface} does not support AP mode"
                    )
        except Exception as e:
            logger.warning("Could not verify AP mode support: %s", e)

        logger.info("System requirements verified successfully")

    def configure_ap(self, config: NetworkConfig) -> None:
        """Write hostapd.conf and dnsmasq.conf configuration files safely."""
        logger.info("Configuring hostapd and dnsmasq...")

        # 1. Generate hostapd configuration
        hostapd_dir = os.path.dirname(config.hostapd_conf)
        if hostapd_dir:
            os.makedirs(hostapd_dir, exist_ok=True)

        ap_isolate_opt = getattr(config, "ap_isolate", 1)
        if isinstance(ap_isolate_opt, bool):
            ap_isolate_val = 1 if ap_isolate_opt else 0
        else:
            try:
                ap_isolate_val = int(ap_isolate_opt)
            except (ValueError, TypeError):
                ap_isolate_val = 1

        hostapd_content = f"""# Interface configuration
interface={config.ap_interface}
driver=nl80211
ssid={config.ssid}

# Hardware configuration
hw_mode=g
channel=7
ieee80211n=1
wmm_enabled=0

# Open network configuration
auth_algs=1
ignore_broadcast_ssid=0
ap_isolate={ap_isolate_val}

# Debugging
logger_syslog=-1
logger_syslog_level=2
logger_stdout=-1
logger_stdout_level=2

# Stability settings
beacon_int=100
dtim_period=2
max_num_sta=10
rts_threshold=2347
fragm_threshold=2346
"""
        with open(config.hostapd_conf, "w", encoding="utf-8") as f:
            f.write(hostapd_content.strip() + "\n")

        self.runner.run(f"chown root:root {config.hostapd_conf}", ignore_errors=True)
        self.runner.run(f"chmod 644 {config.hostapd_conf}", ignore_errors=True)

        # 2. Generate dnsmasq configuration
        dnsmasq_dir = os.path.dirname(config.dnsmasq_conf)
        if dnsmasq_dir:
            os.makedirs(dnsmasq_dir, exist_ok=True)

        dnsmasq_content = f"""# Interface configuration
interface={config.ap_interface}
no-dhcp-interface=lo
bind-interfaces

# DHCP server configuration
dhcp-range={config.dhcp_start},{config.dhcp_end},{config.network_mask},24h
dhcp-option=option:router,{config.ip}
dhcp-option=option:dns-server,{config.ip}
dhcp-option=option:netmask,{config.network_mask}

# DNS configuration
no-resolv
no-poll
server=8.8.8.8
server=8.8.4.4

# Logging
log-queries
log-dhcp
"""
        with open(config.dnsmasq_conf, "w", encoding="utf-8") as f:
            f.write(dnsmasq_content.strip() + "\n")

        self.runner.run(f"chown root:root {config.dnsmasq_conf}", ignore_errors=True)
        self.runner.run(f"chmod 644 {config.dnsmasq_conf}", ignore_errors=True)

        logger.info("Hostapd and dnsmasq configurations generated")

    @staticmethod
    def is_wireless(iface: str) -> bool:
        """Check if a network interface is wireless via sysfs."""
        if not iface:
            return False
        return os.path.exists(f"/sys/class/net/{iface}/wireless") or os.path.exists(
            f"/sys/class/net/{iface}/phy80211"
        )

    def detect_default_uplink(self, ap_iface: str) -> str:
        """Detect active default internet uplink interface.

        Checks the system routing table for default gateway routes.
        Falls back to detecting physical presence of common uplink interfaces.
        """
        route_output = self.runner.execute("ip route show default", ignore_errors=True) or ""
        if not route_output.strip():
            route_output = self.runner.execute("ip route show 0.0.0.0/0", ignore_errors=True) or ""

        for line in route_output.splitlines():
            parts = line.strip().split()
            if "dev" in parts:
                idx = parts.index("dev")
                if idx + 1 < len(parts):
                    dev_name = parts[idx + 1]
                    if dev_name and dev_name != ap_iface:
                        logger.info("Detected default uplink interface via route: %s", dev_name)
                        return dev_name

        # Fallback to physical existence checks
        for candidate in ["eth0", "end0", "wlan1"]:
            if candidate != ap_iface and os.path.exists(f"/sys/class/net/{candidate}"):
                logger.info("Detected uplink interface via sysfs presence: %s", candidate)
                return candidate

        default_iface = "eth0" if os.path.exists("/sys/class/net/eth0") and ap_iface != "eth0" else "wlan1"
        logger.info("Defaulting uplink interface to: %s", default_iface)
        return default_iface

    def get_uplink_info(self, uplink_iface: str) -> Dict[str, Any]:
        """Collect network status and connectivity details for an uplink interface."""
        if not uplink_iface:
            return {
                "interface": "",
                "is_wireless": False,
                "carrier": False,
                "ip": "",
                "gateway": "",
            }

        is_wireless = self.is_wireless(uplink_iface)

        # Carrier detection
        carrier_bool = False
        carrier_path = f"/sys/class/net/{uplink_iface}/carrier"
        if os.path.exists(carrier_path):
            try:
                with open(carrier_path, "r", encoding="utf-8") as f:
                    carrier_bool = f.read().strip() == "1"
            except Exception:
                carrier_bool = False
        else:
            link_info = self.runner.execute(f"ip link show {uplink_iface}", ignore_errors=True) or ""
            carrier_bool = "LOWER_UP" in link_info or ("state UP" in link_info and "NO-CARRIER" not in link_info)

        # IP address extraction
        ip_str = ""
        addr_output = self.runner.execute(f"ip -4 addr show {uplink_iface}", ignore_errors=True) or ""
        if not addr_output:
            addr_output = self.runner.execute(f"ip addr show {uplink_iface}", ignore_errors=True) or ""
        match_ip = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", addr_output)
        if match_ip:
            ip_str = match_ip.group(1)

        # Gateway extraction
        gw_str = ""
        route_output = self.runner.execute(
            f"ip route show default dev {uplink_iface}", ignore_errors=True
        ) or ""
        if not route_output.strip():
            route_output = self.runner.execute("ip route show default", ignore_errors=True) or ""

        for line in route_output.splitlines():
            parts = line.strip().split()
            if "via" in parts:
                via_idx = parts.index("via")
                gw_candidate = parts[via_idx + 1] if via_idx + 1 < len(parts) else ""
                if "dev" in parts:
                    dev_idx = parts.index("dev")
                    dev_candidate = parts[dev_idx + 1] if dev_idx + 1 < len(parts) else ""
                    if dev_candidate == uplink_iface:
                        gw_str = gw_candidate
                        break
                elif route_output.strip().startswith("default via"):
                    gw_str = gw_candidate
                    break

        return {
            "interface": uplink_iface,
            "is_wireless": is_wireless,
            "carrier": carrier_bool,
            "ip": ip_str,
            "gateway": gw_str,
        }

    def start_ap(self, config: NetworkConfig) -> None:
        """Start WiFi Access Point services and configure the AP interface."""
        logger.info("Starting WiFi Access Point on %s...", config.ap_interface)

        # Kill any preexisting instances
        self.runner.run("killall hostapd", ignore_errors=True)
        self.runner.run("killall dnsmasq", ignore_errors=True)

        # Prevent NetworkManager from interfering with AP interface
        self.runner.run(
            f"nmcli device set {config.ap_interface} managed no",
            ignore_errors=True,
        )

        # Wireless-specific operations: performed only on ap_interface (which is wireless)
        is_ap_wireless = self.is_wireless(config.ap_interface)
        if not os.path.exists(f"/sys/class/net/{config.ap_interface}"):
            is_ap_wireless = True

        if is_ap_wireless:
            self.runner.run("rfkill unblock wifi", ignore_errors=True)

        self.runner.run(f"ip link set {config.ap_interface} down")

        if is_ap_wireless:
            self.runner.run(f"iw dev {config.ap_interface} set type __ap", ignore_errors=True)

        self.runner.run(f"ip addr flush dev {config.ap_interface}")
        self.runner.run(f"ip addr add {config.ip}/24 dev {config.ap_interface}")
        self.runner.run(f"ip link set {config.ap_interface} up")

        # Spawn hostapd daemon
        self.runner.run(f"hostapd -B -P /run/hostapd.pid {config.hostapd_conf}")

        # Start dnsmasq DHCP/DNS server
        self.runner.run("systemctl restart dnsmasq", ignore_errors=True)
        logger.info("Access Point started: SSID=%s", config.ssid)

    def stop_ap(self, ap_iface: str, internet_iface: Optional[str] = None) -> None:
        """Stop hostapd, dnsmasq, and restore NetworkManager management."""
        logger.info("Stopping WiFi Access Point...")
        self.runner.run("systemctl stop hostapd", ignore_errors=True)
        self.runner.run("systemctl stop dnsmasq", ignore_errors=True)
        if ap_iface:
            self.runner.run(f"ip link set {ap_iface} down", ignore_errors=True)

        # Re-enable NetworkManager control safely
        if ap_iface:
            self.runner.run(
                f"nmcli device set {ap_iface} managed yes", ignore_errors=True
            )
        if internet_iface and internet_iface != "auto":
            self.runner.run(
                f"nmcli device set {internet_iface} managed yes", ignore_errors=True
            )
        logger.info("WiFi Access Point stopped")

    def check_ap_status(self, ap_iface: str, ap_ip: str) -> bool:
        """Verify hostapd process, interface UP state, assigned IP, and dnsmasq."""
        try:
            # Check hostapd process
            ps_hostapd = self.runner.execute(
                "ps aux | grep '[h]ostapd'", ignore_errors=True
            )
            if not ps_hostapd:
                logger.error("No hostapd process found")
                return False

            # Check interface IP and status
            addr_info = self.runner.execute(
                f"ip addr show {ap_iface}", ignore_errors=True
            )
            if "UP" not in addr_info:
                logger.error("Interface %s is not UP", ap_iface)
                return False
            if ap_ip not in addr_info:
                logger.error(
                    "Interface %s missing required IP %s", ap_iface, ap_ip
                )
                return False

            # Check dnsmasq
            dns_status = self.runner.execute(
                "systemctl status dnsmasq", ignore_errors=True
            )
            if "running" not in dns_status:
                ps_dns = self.runner.execute(
                    "ps aux | grep '[d]nsmasq'", ignore_errors=True
                )
                if not ps_dns:
                    logger.error("dnsmasq service is not running")
                    return False

            return True

        except Exception as e:
            logger.error("Error checking AP status: %s", e)
            return False

    def dump_debug_info(self, ap_iface: str) -> str:
        """Collect diagnostic output across interfaces, services, and logs."""
        sections = [
            ("Interface Status", f"ip addr show {ap_iface}"),
            ("Hostapd Service", "systemctl status hostapd"),
            ("Hostapd Journal", "journalctl -u hostapd -n 50"),
            ("Dnsmasq Service", "systemctl status dnsmasq"),
            ("RFKill Status", "rfkill list"),
            ("Wireless Config", f"iw dev {ap_iface} info"),
        ]

        lines = ["=== Piso-WiFi Debug Information ==="]
        for title, cmd in sections:
            lines.append(f"--- {title} ({cmd}) ---")
            output = self.runner.execute(cmd, ignore_errors=True)
            lines.append(output or "[No output]")

        lines.append("===================================")
        dump = "\n".join(lines)
        logger.error("%s", dump)
        return dump
