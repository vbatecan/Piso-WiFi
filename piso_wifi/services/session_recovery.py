"""Device session token manager for resilient client identification across randomized MACs."""

import logging
import secrets
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SessionRecoveryService:
    """Tracks persistent browser tokens to protect user balances against MAC address rotation."""

    def __init__(self, user_service: Optional[Any] = None):
        self.user_service = user_service
        self._lock = threading.RLock()
        # token -> {"primary_mac": str, "created_at": float, "last_seen": float}
        self._tokens: Dict[str, Dict[str, Any]] = {}
        # mac -> token
        self._mac_to_token: Dict[str, str] = {}

    def get_or_create_token(self, mac: str) -> str:
        """Retrieve existing token for MAC or generate a new random token."""
        norm_mac = mac.strip().upper()
        with self._lock:
            if norm_mac in self._mac_to_token:
                tok = self._mac_to_token[norm_mac]
                if tok in self._tokens:
                    self._tokens[tok]["last_seen"] = time.time()
                    return tok

            token = secrets.token_urlsafe(24)
            now = time.time()
            self._tokens[token] = {
                "primary_mac": norm_mac,
                "created_at": now,
                "last_seen": now,
            }
            self._mac_to_token[norm_mac] = token
            return token

    def resolve_mac_with_token(
        self,
        token: Optional[str],
        current_mac: Optional[str],
    ) -> Optional[str]:
        """Resolve the effective user MAC address, migrating or restoring if MAC changed.

        Args:
            token: Value of the 'piso_device_token' cookie.
            current_mac: Currently observed hardware MAC address from ARP.

        Returns:
            Resolved MAC address (either current_mac or restored primary_mac).
        """
        norm_cur = current_mac.strip().upper() if current_mac else None
        if not token:
            return norm_cur

        with self._lock:
            record = self._tokens.get(token)
            if not record:
                # Token not recognized; register current_mac if present
                if norm_cur:
                    self._tokens[token] = {
                        "primary_mac": norm_cur,
                        "created_at": time.time(),
                        "last_seen": time.time(),
                    }
                    self._mac_to_token[norm_cur] = token
                return norm_cur

            primary_mac = record["primary_mac"]
            record["last_seen"] = time.time()

            # If current MAC is missing or matches primary MAC, return primary
            if not norm_cur or norm_cur == primary_mac:
                return primary_mac

            # Current MAC is different from token's primary MAC:
            # Check balances: if primary has time and current has 0, migrate time or alias!
            if self.user_service:
                primary_bal = self.user_service.check_balance(primary_mac)
                cur_bal = self.user_service.check_balance(norm_cur)

                if primary_bal > 0 and cur_bal <= 0:
                    logger.info(
                        f"Detected randomized MAC rotation from {primary_mac} to {norm_cur}. Migrating {primary_bal} mins."
                    )
                    # Transfer remaining balance to the new active MAC
                    if hasattr(self.user_service, "deduct_time") and hasattr(self.user_service, "add_time"):
                        self.user_service.deduct_time(primary_mac, int(primary_bal))
                        self.user_service.add_time(norm_cur, primary_bal)
                    # Update token's primary MAC
                    record["primary_mac"] = norm_cur
                    self._mac_to_token[norm_cur] = token
                    return norm_cur

            return norm_cur
