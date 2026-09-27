import os
import sqlite3
import pytest
from unittest.mock import MagicMock

from user_manager import UserManager
from piso_wifi.services.user_service import UserService
from piso_wifi.models.entities import User
from piso_wifi.models.enums import DeductionType, PlanType, UserStatus


@pytest.fixture
def test_db_path(tmp_path):
    """Provide a clean temporary database path for each test."""
    db_file = tmp_path / "test_piso_wifi.db"
    return str(db_file)


@pytest.fixture
def user_manager(test_db_path):
    """Provide a UserManager instance pointing to an isolated test database."""
    um = UserManager(db_path=test_db_path)
    return um


def test_add_time(user_manager):
    # Test adding time for a new user
    assert user_manager.add_time("00:11:22:33:44:55", 5, 25) is True
    assert user_manager.check_balance("00:11:22:33:44:55") == 25.0

    # Test adding time for existing user
    assert user_manager.add_time("00:11:22:33:44:55", 5, 25) is True
    assert user_manager.check_balance("00:11:22:33:44:55") == 50.0


def test_add_time_default_minutes(user_manager):
    # If minutes is None, default rate is 1 peso = 1 minute
    assert user_manager.add_time("AA:BB:CC:DD:EE:01", 10.0) is True
    assert user_manager.check_balance("AA:BB:CC:DD:EE:01") == 10.0


def test_check_balance_nonexistent_user(user_manager):
    assert user_manager.check_balance("11:22:33:44:55:66") == 0.0


def test_deduct_time(user_manager):
    mac = "AA:BB:CC:DD:EE:02"
    user_manager.add_time(mac, 20, 20)

    # Auto deduction
    assert user_manager.deduct_time(mac, 5, manual=False) is True
    assert user_manager.check_balance(mac) == 15.0

    # Manual deduction
    assert user_manager.deduct_time(mac, 10, manual=True) is True
    assert user_manager.check_balance(mac) == 5.0

    # Deduct to 0 and check status
    assert user_manager.deduct_time(mac, 10, manual=False) is True
    assert user_manager.check_balance(mac) == 0.0

    user_info = user_manager.get_user_info(mac)
    assert user_info is not None
    assert user_info.status == UserStatus.INACTIVE

    # Nonexistent user deduction returns False
    assert user_manager.deduct_time("FF:FF:FF:FF:FF:FF", 5) is False


def test_set_bandwidth(user_manager):
    mac = "AA:BB:CC:DD:EE:03"
    user_manager.add_time(mac, 10, 10)

    assert user_manager.set_bandwidth(mac, 4096, 2048) is True
    user_info = user_manager.get_user_info(mac)
    assert user_info.download_limit == 4096
    assert user_info.upload_limit == 2048


def test_request_upgrade(user_manager):
    mac = "AA:BB:CC:DD:EE:04"
    user_manager.add_time(mac, 10, 10)

    assert user_manager.request_upgrade(mac) is True
    user_info = user_manager.get_user_info(mac)
    assert user_info.upgrade_requested is True


def test_manage_plan(user_manager):
    mac = "AA:BB:CC:DD:EE:05"
    user_manager.add_time(mac, 10, 10)
    user_manager.request_upgrade(mac)

    assert user_manager.manage_plan(mac, "premium", 8192, 4096) is True
    user_info = user_manager.get_user_info(mac)
    assert user_info.plan == PlanType.PREMIUM
    assert user_info.download_limit == 8192
    assert user_info.upload_limit == 4096
    assert user_info.upgrade_requested is False


def test_get_user_info(user_manager):
    mac = "AA:BB:CC:DD:EE:06"
    assert user_manager.get_user_info(mac) is None

    user_manager.add_time(mac, 15, 30)
    user = user_manager.get_user_info(mac)
    assert isinstance(user, User)
    assert user.mac_address.upper() == mac.upper()
    assert user.time_balance == 30.0
    assert user.status == UserStatus.ACTIVE


def test_check_health(user_manager):
    assert user_manager.check_health() is True


def test_db_name_and_db_path_compatibility(tmp_path):
    db_file1 = str(tmp_path / "compat1.db")
    db_file2 = str(tmp_path / "compat2.db")

    um = UserManager(db_path=db_file1)
    assert um.db_path == db_file1
    assert um.db_name == db_file1

    # Setting db_name updates db_path
    um.db_name = db_file2
    assert um.db_path == db_file2
    assert um.db_name == db_file2


def test_repository_dependency_injection():
    mock_repo = MagicMock()
    mock_repo.check_balance.return_value = 42.0
    mock_repo.add_time.return_value = True
    mock_repo.deduct_time.return_value = True
    mock_repo.check_health.return_value = True

    svc = UserService(repository=mock_repo)
    assert svc.check_balance("00:11:22:33:44:55") == 42.0
    mock_repo.check_balance.assert_called_once_with("00:11:22:33:44:55")

    assert svc.add_time("00:11:22:33:44:55", 10.0, 10) is True
    mock_repo.add_time.assert_called_once_with("00:11:22:33:44:55", 10.0, 10)

    assert svc.deduct_time("00:11:22:33:44:55", 5.0, manual=True) is True
    mock_repo.deduct_time.assert_called_once_with("00:11:22:33:44:55", 5.0, manual=True)

    assert svc.check_health() is True
    mock_repo.check_health.assert_called_once()