"""Firewall and NAT management for Piso-WiFi routing and access control.

Handles iptables manipulation for packet forwarding, masquerading,
local portal traffic exemptions, and client MAC address filtering.
"""

import logging
from typing import Optional

from piso_wifi.network.command_runner import CommandRunner, SystemCommandRunner

logger = logging.getLogger(__name__)


class FirewallManager:
    """Manages iptables rules for NAT, packet forwarding, and MAC filtering."""

    def __init__(self, runner: Optional[CommandRunner] = None):
        self.runner = runner or SystemCommandRunner()

    def setup_nat_and_forwarding(
        self, ap_iface: str, internet_iface: str, ap_ip: str
    ) -> None:
        """Configure IP forwarding, NAT masquerading, and base captive portal firewall rules."""
        logger.info(
            "Setting up NAT and packet forwarding: %s -> %s (AP IP: %s)",
            ap_iface,
            internet_iface,
            ap_ip,
        )

        # Enable IPv4 forwarding in kernel
        self.runner.run("echo 1 > /proc/sys/net/ipv4/ip_forward", ignore_errors=True)

        # Flush previous filtering and NAT rules
        self.runner.run("iptables -t nat -F")
        self.runner.run("iptables -F")

        # Default policies: deny forward by default, accept input and output
        self.runner.run("iptables -P FORWARD DROP")
        self.runner.run("iptables -P INPUT ACCEPT")
        self.runner.run("iptables -P OUTPUT ACCEPT")

        # Allow established and related connections
        self.runner.run(
            "iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT"
        )

        # Outbound NAT masquerading
        # Interface-agnostic rule: masquerade all non-AP egress traffic
        self.runner.run(
            f"iptables -t nat -A POSTROUTING ! -o {ap_iface} -j MASQUERADE"
        )
        if internet_iface and internet_iface != "auto" and internet_iface != ap_iface:
            self.runner.run(
                f"iptables -t nat -A POSTROUTING -o {internet_iface} -j MASQUERADE"
            )

        # Allow DNS and DHCP traffic from AP clients
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -p udp --dport 53 -j ACCEPT"
        )
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -p udp --dport 67:68 -j ACCEPT"
        )

        # Allow captive portal traffic (port 5000) from AP interface
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -p tcp --dport 5000 -j ACCEPT"
        )

        # Allow access to local captive portal / web interface on AP IP
        self.runner.run(f"iptables -A FORWARD -i {ap_iface} -d {ap_ip} -j ACCEPT")

        # Explicitly drop all other forward traffic from AP interface
        self.runner.run(f"iptables -A FORWARD -i {ap_iface} -j DROP")
        logger.info("NAT and forwarding rules applied successfully")

    def setup_captive_portal_rules(
        self, ap_iface: str, ap_ip: str, portal_port: int = 5000
    ) -> None:
        """Configure DNAT rules to redirect HTTP traffic (port 80) to captive portal."""
        logger.info(
            "Setting up captive portal redirection: %s -> %s:%d",
            ap_iface,
            ap_ip,
            portal_port,
        )
        self.runner.run(
            f"iptables -t nat -A PREROUTING -i {ap_iface} -p tcp --dport 80 -j DNAT --to-destination {ap_ip}:{portal_port}",
            ignore_errors=True,
        )

    def block_mac(self, mac: str) -> bool:
        """Block forwarding traffic for a given MAC address."""
        try:
            logger.info("Blocking MAC address: %s", mac)
            # Remove any prior rules to prevent duplicate accumulation
            self.runner.run(
                f"iptables -D FORWARD -m mac --mac-source {mac} -j ACCEPT",
                ignore_errors=True,
            )
            self.runner.run(
                f"iptables -D FORWARD -m mac --mac-source {mac} -j DROP",
                ignore_errors=True,
            )

            # Insert drop rule at the top of the FORWARD chain
            self.runner.run(
                f"iptables -I FORWARD 1 -m mac --mac-source {mac} -j DROP"
            )
            return True
        except Exception as e:
            logger.error("Failed to block MAC %s: %s", mac, e)
            return False

    def unblock_mac(self, mac: str) -> bool:
        """Allow forwarding traffic for an authorized MAC address."""
        try:
            logger.info("Unblocking MAC address: %s", mac)
            # Remove existing rules for clean state
            self.runner.run(
                f"iptables -D FORWARD -m mac --mac-source {mac} -j ACCEPT",
                ignore_errors=True,
            )
            self.runner.run(
                f"iptables -D FORWARD -m mac --mac-source {mac} -j DROP",
                ignore_errors=True,
            )

            # Insert accept rule at the top of the FORWARD chain
            self.runner.run(
                f"iptables -I FORWARD 1 -m mac --mac-source {mac} -j ACCEPT"
            )
            return True
        except Exception as e:
            logger.error("Failed to unblock MAC %s: %s", mac, e)
            return False

    def allow_local_traffic(self, ap_iface: str, ap_ip: str) -> None:
        """Ensure local captive portal, DNS, and DHCP services are accessible."""
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -d {ap_ip} -j ACCEPT",
            ignore_errors=True,
        )
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -p udp --dport 53 -j ACCEPT",
            ignore_errors=True,
        )
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -p udp --dport 67:68 -j ACCEPT",
            ignore_errors=True,
        )
        self.runner.run(
            f"iptables -A FORWARD -i {ap_iface} -p tcp --dport 5000 -j ACCEPT",
            ignore_errors=True,
        )

    def dump_iptables(self) -> str:
        """Export current iptables configuration via iptables-save."""
        try:
            return self.runner.execute("iptables-save", ignore_errors=True)
        except Exception as e:
            logger.error("Failed to dump iptables: %s", e)
            return ""
