"""Sliding-window rate limiter for admin authentication and brute-force prevention."""

from collections import defaultdict
import logging
import math
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LoginRateLimiter:
    """In-memory sliding window rate limiter for admin authentication attempts.

    Supports tracking failed attempts by IP address, username, or composite keys.
    Provides configurable lockout windows, progressive backoff delays, and thread-safe operations.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 900,  # 15 minutes lockout
        progressive_delay: bool = True,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
    ):
        """Initialize LoginRateLimiter.

        Args:
            max_attempts: Number of failed attempts before lockout triggers (default: 5).
            window_seconds: Lockout duration and sliding window span in seconds (default: 900 / 15 min).
            progressive_delay: Whether to compute progressive throttle delays between attempts (default: True).
            base_delay: Initial throttle delay in seconds for progressive backoff (default: 1.0).
            max_delay: Maximum ceiling in seconds for progressive throttle delay (default: 60.0).
        """
        self.max_attempts = int(max_attempts)
        self.window_seconds = int(window_seconds)
        self.progressive_delay = bool(progressive_delay)
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)

        self._lock = threading.RLock()
        # key -> list of failure timestamps
        self._attempts: Dict[str, List[float]] = defaultdict(list)
        # key -> absolute lockout expiration timestamp (epoch seconds)
        self._lockouts: Dict[str, float] = {}

    def _purge_expired_attempts(self, key: str, now: float) -> List[float]:
        """Prune attempts older than the window period for the given key."""
        cutoff = now - self.window_seconds
        valid = [t for t in self._attempts.get(key, []) if t > cutoff]
        if valid:
            self._attempts[key] = valid
        else:
            self._attempts.pop(key, None)
        return valid

    def record_attempt(self, key: str, success: bool) -> None:
        """Record an authentication attempt for the specified key.

        Args:
            key: Tracking key (e.g. IP address or username).
            success: True if authentication succeeded, False if it failed.
        """
        if not key:
            return

        with self._lock:
            now = time.time()
            if success:
                # Clear lockout and attempts on successful login
                self.reset(key)
                logger.debug("Rate limiter: Successful attempt recorded for '%s'. State reset.", key)
                return

            # Failed attempt: prune older attempts
            attempts = self._purge_expired_attempts(key, now)
            attempts.append(now)
            self._attempts[key] = attempts

            # Trigger lockout if threshold reached
            if len(attempts) >= self.max_attempts:
                self._lockouts[key] = now + self.window_seconds
                logger.warning(
                    "Rate limiter: Key '%s' exceeded max attempts (%d/%d). Locked out for %d seconds.",
                    key,
                    len(attempts),
                    self.max_attempts,
                    self.window_seconds,
                )
            else:
                logger.info(
                    "Rate limiter: Failed attempt %d/%d recorded for '%s'.",
                    len(attempts),
                    self.max_attempts,
                    key,
                )

    def is_locked(self, key: str) -> Tuple[bool, int]:
        """Check whether the specified key is currently locked out.

        Args:
            key: Tracking key (e.g. IP address or username).

        Returns:
            Tuple[bool, int]: (is_locked, remaining_lockout_seconds).
                If locked, returns (True, remaining_seconds >= 1).
                If not locked, returns (False, 0).
        """
        if not key:
            return False, 0

        with self._lock:
            now = time.time()

            # 1. Check active lockout
            if key in self._lockouts:
                lockout_end = self._lockouts[key]
                if now < lockout_end:
                    remaining = int(math.ceil(lockout_end - now))
                    return True, max(1, remaining)
                else:
                    # Lockout expired naturally
                    self._lockouts.pop(key, None)
                    self._attempts.pop(key, None)
                    logger.info("Rate limiter: Lockout expired for '%s'.", key)
                    return False, 0

            # 2. Check sliding window attempts
            attempts = self._purge_expired_attempts(key, now)
            if len(attempts) >= self.max_attempts:
                lockout_end = attempts[-1] + self.window_seconds
                if now < lockout_end:
                    self._lockouts[key] = lockout_end
                    remaining = int(math.ceil(lockout_end - now))
                    return True, max(1, remaining)
                else:
                    self._attempts.pop(key, None)
                    return False, 0

            return False, 0

    def reset(self, key: str) -> None:
        """Reset failed attempts and clear lockout for the specified key.

        Args:
            key: Tracking key to clear.
        """
        if not key:
            return
        with self._lock:
            self._attempts.pop(key, None)
            self._lockouts.pop(key, None)

    def get_attempts(self, key: str) -> int:
        """Return the count of active failed attempts in the sliding window for the key."""
        if not key:
            return 0
        with self._lock:
            now = time.time()
            attempts = self._purge_expired_attempts(key, now)
            return len(attempts)

    def get_delay(self, key: str) -> float:
        """Compute progressive throttle delay in seconds for the given key.

        Exponential backoff scales with consecutive failed attempts:
            - 0 attempts: 0.0s
            - 1 attempt: base_delay (e.g. 1.0s)
            - 2 attempts: base_delay * 2 (e.g. 2.0s)
            - 3 attempts: base_delay * 4 (e.g. 4.0s)
            - ... capped at max_delay.

        Args:
            key: Tracking key.

        Returns:
            float: Delay in seconds (0.0 if disabled or no attempts).
        """
        if not self.progressive_delay or not key:
            return 0.0

        with self._lock:
            now = time.time()
            attempts = self._purge_expired_attempts(key, now)
            count = len(attempts)

        if count <= 0:
            return 0.0

        delay = self.base_delay * (2 ** (count - 1))
        return min(self.max_delay, float(delay))

    def apply_delay(self, key: str) -> float:
        """Calculate and execute sleep delay for progressive throttling."""
        delay = self.get_delay(key)
        if delay > 0:
            time.sleep(delay)
        return delay

    def record_login_attempt(self, ip: str, username: str, success: bool) -> None:
        """Track login attempts across IP address, username, and composite IP:user key.

        Args:
            ip: Client IP address.
            username: Admin username.
            success: Whether the authentication attempt succeeded.
        """
        clean_ip = (ip or "").strip()
        clean_user = (username or "").strip().lower()

        if clean_ip:
            self.record_attempt(f"ip:{clean_ip}", success)
        if clean_user:
            self.record_attempt(f"user:{clean_user}", success)
        if clean_ip and clean_user:
            self.record_attempt(f"ip_user:{clean_ip}:{clean_user}", success)

    def is_login_locked(self, ip: str, username: str) -> Tuple[bool, int]:
        """Check if client IP, username, or combination is locked out.

        Args:
            ip: Client IP address.
            username: Admin username.

        Returns:
            Tuple[bool, int]: (is_locked, remaining_seconds) returning the maximum remaining lockout.
        """
        clean_ip = (ip or "").strip()
        clean_user = (username or "").strip().lower()

        locked = False
        max_remaining = 0

        keys_to_check = []
        if clean_ip:
            keys_to_check.append(f"ip:{clean_ip}")
        if clean_user:
            keys_to_check.append(f"user:{clean_user}")
        if clean_ip and clean_user:
            keys_to_check.append(f"ip_user:{clean_ip}:{clean_user}")

        for k in keys_to_check:
            is_lk, rem = self.is_locked(k)
            if is_lk:
                locked = True
                if rem > max_remaining:
                    max_remaining = rem

        return locked, max_remaining

    def reset_login(self, ip: str, username: str) -> None:
        """Reset attempts and lockouts across IP address, username, and composite key."""
        clean_ip = (ip or "").strip()
        clean_user = (username or "").strip().lower()

        if clean_ip:
            self.reset(f"ip:{clean_ip}")
        if clean_user:
            self.reset(f"user:{clean_user}")
        if clean_ip and clean_user:
            self.reset(f"ip_user:{clean_ip}:{clean_user}")

    def clear(self) -> None:
        """Clear all active lockouts and attempts across all keys."""
        with self._lock:
            self._attempts.clear()
            self._lockouts.clear()
