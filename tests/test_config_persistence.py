"""Unit tests for configuration persistence, .env handling, and AppConfig lifecycle."""

import os
import tempfile
import pytest

from piso_wifi.config import AppConfig, load_app_config, save_env_file


def test_app_config_to_dict():
    """Test AppConfig serialization to dictionary."""
    config = AppConfig(
        admin_username="testadmin",
        admin_password="testpassword",
        minutes_per_peso=6.0,
        setup_completed=True,
    )
    d = config.to_dict()

    assert d["admin_username"] == "testadmin"
    assert d["admin_password"] == "testpassword"
    assert d["minutes_per_peso"] == 6.0
    assert d["setup_completed"] is True
    assert "network" in d
    assert d["network"]["ap_interface"] == "wlan0"
    assert "bandwidth" in d["network"]


def test_save_env_file_creates_and_updates():
    """Test save_env_file writes key-value pairs atomically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, ".env")

        # 1. Initial write
        updates = {
            "WIFI_INTERFACE": "wlan1",
            "AP_SSID": "MyTestHotspot",
            "ADMIN_USERNAME": "superadmin",
            "SETUP_COMPLETED": "true",
        }
        success = save_env_file(updates, env_path=env_path)
        assert success is True
        assert os.path.exists(env_path)

        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "WIFI_INTERFACE=wlan1" in content
        assert "AP_SSID=MyTestHotspot" in content
        assert "ADMIN_USERNAME=superadmin" in content
        assert "SETUP_COMPLETED=true" in content

        # 2. Update existing key and add new key without losing prior content
        second_updates = {
            "AP_SSID": "UpdatedHotspot",
            "AP_IP": "10.0.0.1",
        }
        success2 = save_env_file(second_updates, env_path=env_path)
        assert success2 is True

        with open(env_path, "r", encoding="utf-8") as f:
            updated_content = f.read()

        assert "AP_SSID=UpdatedHotspot" in updated_content
        assert "WIFI_INTERFACE=wlan1" in updated_content
        assert "AP_IP=10.0.0.1" in updated_content
        assert "ADMIN_USERNAME=superadmin" in updated_content


def test_load_app_config_from_custom_env():
    """Test load_app_config loads persisted environment variables."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, ".env")
        save_env_file({
            "WIFI_INTERFACE": "wlan99",
            "AP_SSID": "CustomPisoWiFi",
            "SETUP_COMPLETED": "true",
            "ADMIN_USERNAME": "envadmin",
        }, env_path=env_path)

        config = load_app_config(env_path=env_path)

        assert config.network.ap_interface == "wlan99"
        assert config.network.ssid == "CustomPisoWiFi"
        assert config.setup_completed is True
        assert config.admin_username == "envadmin"
