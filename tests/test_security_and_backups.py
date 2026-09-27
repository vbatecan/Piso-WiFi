"""Unit tests for Feature 5: Security & System Hardening.

Covers:
- LoginRateLimiter (sliding window, lockout, progressive delay, thread safety, IP/user tracking)
- BackupService (SQLite snapshots, .env sanitization, listing, restoration, credential preservation, pruning)
- Hostapd client isolation (ap_isolate=1 verification in AccessPointManager and NetworkController)
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import shutil
import sqlite3
import tempfile
import time
from unittest.mock import patch
import zipfile
import pytest

from piso_wifi.config import NetworkConfig
from piso_wifi.network.access_point import AccessPointManager
from piso_wifi.network.command_runner import MockCommandRunner
from piso_wifi.network.controller import NetworkController
from piso_wifi.security.rate_limiter import LoginRateLimiter
from piso_wifi.services.backup_service import BackupService


# =============================================================================
# 1. Rate Limiter Tests
# =============================================================================

def test_rate_limiter_defaults():
    limiter = LoginRateLimiter()
    assert limiter.max_attempts == 5
    assert limiter.window_seconds == 900
    assert limiter.progressive_delay is True
    assert limiter.base_delay == 1.0
    assert limiter.max_delay == 60.0

    is_lk, rem = limiter.is_locked("192.168.1.100")
    assert is_lk is False
    assert rem == 0
    assert limiter.get_attempts("192.168.1.100") == 0


def test_rate_limiter_successful_login_resets_history():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    key = "admin"

    limiter.record_attempt(key, success=False)
    limiter.record_attempt(key, success=False)
    assert limiter.get_attempts(key) == 2
    assert limiter.is_locked(key)[0] is False

    # Successful login resets the counter
    limiter.record_attempt(key, success=True)
    assert limiter.get_attempts(key) == 0
    assert limiter.is_locked(key)[0] is False


def test_rate_limiter_lockout_after_max_attempts():
    limiter = LoginRateLimiter(max_attempts=5, window_seconds=900)
    key = "192.168.4.50"

    for i in range(4):
        limiter.record_attempt(key, success=False)
        is_lk, rem = limiter.is_locked(key)
        assert is_lk is False
        assert rem == 0
        assert limiter.get_attempts(key) == i + 1

    # 5th failed attempt triggers lockout
    limiter.record_attempt(key, success=False)
    is_lk, rem = limiter.is_locked(key)
    assert is_lk is True
    assert 0 < rem <= 900


def test_rate_limiter_window_expiration():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=100)
    key = "10.0.0.1"
    start_time = 1000.0

    with patch("time.time", return_value=start_time):
        limiter.record_attempt(key, success=False)
        limiter.record_attempt(key, success=False)
        limiter.record_attempt(key, success=False)
        is_lk, rem = limiter.is_locked(key)
        assert is_lk is True
        assert rem == 100

    # Advance time beyond window
    with patch("time.time", return_value=start_time + 101.0):
        is_lk, rem = limiter.is_locked(key)
        assert is_lk is False
        assert rem == 0
        assert limiter.get_attempts(key) == 0


def test_rate_limiter_manual_reset():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=300)
    key = "bad_actor"

    limiter.record_attempt(key, success=False)
    limiter.record_attempt(key, success=False)
    assert limiter.is_locked(key)[0] is True

    limiter.reset(key)
    assert limiter.is_locked(key)[0] is False
    assert limiter.get_attempts(key) == 0


def test_rate_limiter_multi_key_isolation():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=300)
    key1 = "192.168.4.1"
    key2 = "192.168.4.2"

    limiter.record_attempt(key1, success=False)
    limiter.record_attempt(key1, success=False)

    assert limiter.is_locked(key1)[0] is True
    assert limiter.is_locked(key2)[0] is False


def test_rate_limiter_progressive_delay():
    limiter = LoginRateLimiter(
        max_attempts=5,
        progressive_delay=True,
        base_delay=1.0,
        max_delay=10.0,
    )
    key = "client_ip"

    assert limiter.get_delay(key) == 0.0

    limiter.record_attempt(key, success=False)
    assert limiter.get_delay(key) == 1.0  # 1.0 * 2^0

    limiter.record_attempt(key, success=False)
    assert limiter.get_delay(key) == 2.0  # 1.0 * 2^1

    limiter.record_attempt(key, success=False)
    assert limiter.get_delay(key) == 4.0  # 1.0 * 2^2

    limiter.record_attempt(key, success=False)
    assert limiter.get_delay(key) == 8.0  # 1.0 * 2^3

    limiter.record_attempt(key, success=False)
    assert limiter.get_delay(key) == 10.0  # capped at max_delay 10.0

    # Test apply_delay
    with patch("time.sleep") as mock_sleep:
        delay_applied = limiter.apply_delay(key)
        assert delay_applied == 10.0
        mock_sleep.assert_called_once_with(10.0)


def test_rate_limiter_progressive_delay_disabled():
    limiter = LoginRateLimiter(progressive_delay=False)
    key = "no_delay"
    limiter.record_attempt(key, success=False)
    limiter.record_attempt(key, success=False)
    assert limiter.get_delay(key) == 0.0


def test_rate_limiter_ip_and_username_helpers():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=500)
    ip = "192.168.4.77"
    user = "admin"

    # Brute forcing with multiple IPs on one user
    limiter.record_login_attempt("1.1.1.1", user, success=False)
    limiter.record_login_attempt("2.2.2.2", user, success=False)
    limiter.record_login_attempt("3.3.3.3", user, success=False)

    # User admin is locked
    assert limiter.is_login_locked("4.4.4.4", user)[0] is True
    # Unrelated user from clean IP is not locked
    assert limiter.is_login_locked("4.4.4.4", "operator")[0] is False

    # Reset admin
    limiter.reset_login("4.4.4.4", user)
    assert limiter.is_login_locked("4.4.4.4", user)[0] is False


def test_rate_limiter_thread_safety():
    limiter = LoginRateLimiter(max_attempts=50, window_seconds=600)
    key = "thread_target"

    def worker(success: bool):
        for _ in range(10):
            limiter.record_attempt(key, success)
            limiter.is_locked(key)
            limiter.get_delay(key)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, False) for _ in range(5)]
        for f in futures:
            f.result()

    assert limiter.get_attempts(key) == 50
    assert limiter.is_locked(key)[0] is True


def test_rate_limiter_edge_cases():
    limiter = LoginRateLimiter()
    # Empty strings and None handled safely
    limiter.record_attempt("", success=False)
    limiter.record_attempt(None, success=False)  # type: ignore
    assert limiter.is_locked("")[0] is False
    assert limiter.is_locked(None)[0] is False  # type: ignore
    assert limiter.get_attempts("") == 0
    limiter.reset("")

    # Clear all
    limiter.record_attempt("k1", success=False)
    limiter.record_attempt("k2", success=False)
    limiter.clear()
    assert limiter.get_attempts("k1") == 0
    assert limiter.get_attempts("k2") == 0


# =============================================================================
# 2. Backup Service Tests
# =============================================================================

@pytest.fixture
def temp_backup_env():
    """Create a temporary directory structure for database and environment backups."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "config", "piso_wifi.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # Seed sample SQLite DB
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, mac TEXT, time_remaining INTEGER);")
        cursor.execute("INSERT INTO users (mac, time_remaining) VALUES ('00:11:22:33:44:55', 3600);")
        conn.commit()
        conn.close()

        # Seed sample .env
        env_path = os.path.join(tmpdir, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(
                "# Network Settings\n"
                "AP_SSID=TestHotspot\n"
                "DHCP_RANGE_START=192.168.4.2\n"
                "# Secrets\n"
                "ADMIN_USERNAME=admin\n"
                "ADMIN_PASSWORD=supersecretpassword123\n"
                "AP_PASSWORD=wifisecretpass\n"
                "SECRET_KEY=super_jwt_secret_xyz\n"
            )

        backup_dir = os.path.join(tmpdir, "backups")

        service = BackupService(
            db_path=db_path,
            env_path=env_path,
            backup_dir=backup_dir,
        )

        yield {
            "tmpdir": tmpdir,
            "db_path": db_path,
            "env_path": env_path,
            "backup_dir": backup_dir,
            "service": service,
        }


def test_backup_env_sanitization(temp_backup_env):
    service = temp_backup_env["service"]

    raw_env = (
        "AP_SSID=PisoWiFi\n"
        "ADMIN_PASSWORD=my_admin_pass\n"
        "SECRET_KEY=secret_token_123\n"
        "DATABASE_URL=sqlite:///config/piso_wifi.db\n"
    )

    sanitized = service.sanitize_env(raw_env)
    assert "ADMIN_PASSWORD=********" in sanitized
    assert "SECRET_KEY=********" in sanitized
    assert "AP_SSID=PisoWiFi" in sanitized
    assert "DATABASE_URL=sqlite:///config/piso_wifi.db" in sanitized
    assert "my_admin_pass" not in sanitized
    assert "secret_token_123" not in sanitized


def test_backup_create_snapshot_zip(temp_backup_env):
    service = temp_backup_env["service"]
    backup_dir = temp_backup_env["backup_dir"]

    result = service.create_backup(dest_dir=backup_dir)

    assert result["status"] == "success"
    assert result["contains_db"] is True
    assert result["contains_env"] is True
    assert os.path.exists(result["filepath"])
    assert result["size_bytes"] > 0
    assert result["filename"].startswith("piso_wifi_backup_")

    # Inspect zip contents
    with zipfile.ZipFile(result["filepath"], "r") as zf:
        namelist = zf.namelist()
        assert "piso_wifi.db" in namelist
        assert ".env" in namelist

        # Verify .env in zip is sanitized
        env_content = zf.read(".env").decode("utf-8")
        assert "ADMIN_PASSWORD=********" in env_content
        assert "supersecretpassword123" not in env_content
        assert "AP_SSID=TestHotspot" in env_content


def test_backup_list_backups(temp_backup_env):
    service = temp_backup_env["service"]
    backup_dir = temp_backup_env["backup_dir"]

    # Initially empty
    assert service.list_backups() == []

    # Create 3 backups with slight timestamps
    b1 = service.create_backup()
    time.sleep(0.05)
    b2 = service.create_backup()

    backups = service.list_backups()
    assert len(backups) == 2
    assert backups[0]["filename"] == b2["filename"]
    assert backups[1]["filename"] == b1["filename"]
    assert backups[0]["contains_db"] is True
    assert backups[0]["contains_env"] is True
    assert backups[0]["size_bytes"] > 0


def test_backup_restore_snapshot(temp_backup_env):
    service = temp_backup_env["service"]
    db_path = temp_backup_env["db_path"]
    env_path = temp_backup_env["env_path"]

    # 1. Create a clean backup
    backup_res = service.create_backup()
    zip_path = backup_res["filepath"]

    # 2. Corrupt or alter the current database and .env
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET time_remaining = 0 WHERE mac = '00:11:22:33:44:55';")
    cursor.execute("INSERT INTO users (mac, time_remaining) VALUES ('corrupted_mac', 9999);")
    conn.commit()
    conn.close()

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("AP_SSID=CorruptedHotspot\nADMIN_PASSWORD=supersecretpassword123\n")

    # 3. Restore snapshot
    success = service.restore_backup(zip_path)
    assert success is True

    # 4. Verify database restored
    conn2 = sqlite3.connect(db_path)
    cursor2 = conn2.cursor()
    cursor2.execute("SELECT time_remaining FROM users WHERE mac = '00:11:22:33:44:55';")
    row = cursor2.fetchone()
    assert row[0] == 3600

    cursor2.execute("SELECT COUNT(*) FROM users WHERE mac = 'corrupted_mac';")
    count = cursor2.fetchone()[0]
    assert count == 0
    conn2.close()

    # 5. Verify .env restored and existing password was preserved
    with open(env_path, "r", encoding="utf-8") as f:
        restored_env = f.read()

    assert "AP_SSID=TestHotspot" in restored_env
    # The active secret was preserved rather than overwritten with asterisks
    assert "ADMIN_PASSWORD=supersecretpassword123" in restored_env


def test_backup_restore_invalid_path(temp_backup_env):
    service = temp_backup_env["service"]
    assert service.restore_backup("/nonexistent/path/to/archive.zip") is False


def test_backup_pruning(temp_backup_env):
    service = temp_backup_env["service"]
    backup_dir = temp_backup_env["backup_dir"]
    os.makedirs(backup_dir, exist_ok=True)

    # Create 10 mock zip archives with distinct modification times
    for i in range(10):
        fname = f"piso_wifi_backup_20260927_{100000 + i}.zip"
        fpath = os.path.join(backup_dir, fname)
        with zipfile.ZipFile(fpath, "w") as zf:
            zf.writestr("piso_wifi.db", "mock db")
            zf.writestr(".env", "mock env")
        os.utime(fpath, (1000 + i, 1000 + i))

    assert len(service.list_backups()) == 10

    # Prune to keep 7
    deleted = service.prune_old_backups(max_keep=7)
    assert deleted == 3

    remaining = service.list_backups()
    assert len(remaining) == 7

    # Prune again when within limit
    assert service.prune_old_backups(max_keep=7) == 0


def test_backup_automatic_snapshot_and_prune(temp_backup_env):
    service = temp_backup_env["service"]

    # Run automatic backup
    res = service.create_automatic_backup(max_keep=5)
    assert res["status"] == "success"
    assert "pruned_count" in res
    assert len(service.list_backups()) == 1


# =============================================================================
# 3. Hostapd Client Isolation Tests
# =============================================================================

def test_hostapd_configuration_has_ap_isolate():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    with tempfile.TemporaryDirectory() as tmpdir:
        hostapd_path = os.path.join(tmpdir, "hostapd.conf")
        dnsmasq_path = os.path.join(tmpdir, "dnsmasq.conf")

        config = NetworkConfig(
            ap_interface="wlan0",
            ssid="SecuredHotspot",
            hostapd_conf=hostapd_path,
            dnsmasq_conf=dnsmasq_path,
        )

        ap_mgr.configure_ap(config)

        assert os.path.exists(hostapd_path)
        with open(hostapd_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify client isolation setting is present
        assert "ap_isolate=1" in content
        assert "ssid=SecuredHotspot" in content


def test_network_controller_hostapd_helper_client_isolation():
    runner = MockCommandRunner()

    with tempfile.TemporaryDirectory() as tmpdir:
        hostapd_path = os.path.join(tmpdir, "hostapd.conf")
        dnsmasq_path = os.path.join(tmpdir, "dnsmasq.conf")

        config = NetworkConfig(
            ap_interface="wlan0",
            ssid="ControllerHotspot",
            hostapd_conf=hostapd_path,
            dnsmasq_conf=dnsmasq_path,
        )

        ctrl = NetworkController(config=config, runner=runner, auto_start=False)
        assert ctrl.ap_isolate == 1

        # Execute hostapd configuration helper
        ctrl.configure_ap()

        with open(hostapd_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "ap_isolate=1" in content


def test_hostapd_ap_isolate_configurable():
    runner = MockCommandRunner()
    ap_mgr = AccessPointManager(runner=runner)

    with tempfile.TemporaryDirectory() as tmpdir:
        hostapd_path = os.path.join(tmpdir, "hostapd.conf")
        dnsmasq_path = os.path.join(tmpdir, "dnsmasq.conf")

        config = NetworkConfig(
            ap_interface="wlan0",
            hostapd_conf=hostapd_path,
            dnsmasq_conf=dnsmasq_path,
        )
        # Custom override
        setattr(config, "ap_isolate", 0)

        ap_mgr.configure_ap(config)

        with open(hostapd_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "ap_isolate=0" in content
