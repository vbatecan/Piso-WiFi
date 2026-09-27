"""Unit tests for the DatabaseManager and UserRepository layer."""

import os
import sqlite3
import tempfile
import threading
from typing import Generator
import pytest

from piso_wifi.database.connection import DatabaseManager
from piso_wifi.database.repository import UserRepository
from piso_wifi.models.entities import User
from piso_wifi.models.enums import DeductionType, PlanType, UserStatus


@pytest.fixture
def in_memory_db() -> DatabaseManager:
    """Fixture providing an in-memory DatabaseManager."""
    db = DatabaseManager(":memory:")
    yield db
    db.close()


@pytest.fixture
def repo(in_memory_db: DatabaseManager) -> UserRepository:
    """Fixture providing a UserRepository with an initialized in-memory database."""
    repository = UserRepository(in_memory_db)
    repository.init_db()
    return repository


class TestDatabaseManager:
    """Tests for DatabaseManager connection, pragma, context manager, and directory creation."""

    def test_in_memory_connection_retains_data(self, in_memory_db: DatabaseManager):
        """Verify that multiple context manager invocations share the same in-memory DB."""
        with in_memory_db.get_connection() as conn:
            conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, val TEXT);")
            conn.execute("INSERT INTO test_table (val) VALUES ('antigravity');")

        with in_memory_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT val FROM test_table WHERE id = 1;")
            row = cursor.fetchone()
            assert row is not None
            assert row["val"] == "antigravity"

    def test_pragmas_and_row_factory(self, in_memory_db: DatabaseManager):
        """Verify row_factory is sqlite3.Row and PRAGMA foreign_keys is ON."""
        with in_memory_db.get_connection() as conn:
            assert conn.row_factory == sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys;")
            fk_status = cursor.fetchone()[0]
            assert fk_status == 1

    def test_direct_context_manager_syntax(self, in_memory_db: DatabaseManager):
        """Verify with db as conn works seamlessly."""
        with in_memory_db as conn:
            conn.execute("CREATE TABLE direct_syntax (id INTEGER PRIMARY KEY);")
            conn.execute("INSERT INTO direct_syntax (id) VALUES (42);")

        with in_memory_db as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM direct_syntax;")
            assert cursor.fetchone()["id"] == 42

    def test_transaction_rollback_on_exception(self, in_memory_db: DatabaseManager):
        """Verify that an exception causes a rollback in the context manager."""
        with in_memory_db.get_connection() as conn:
            conn.execute("CREATE TABLE rollback_test (id INTEGER PRIMARY KEY, name TEXT);")

        with pytest.raises(ValueError):
            with in_memory_db.get_connection() as conn:
                conn.execute("INSERT INTO rollback_test (id, name) VALUES (1, 'retained');")
                raise ValueError("Simulated failure")

        with in_memory_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM rollback_test;")
            assert cursor.fetchone()["cnt"] == 0

    def test_file_based_directory_creation_and_cleanup(self):
        """Verify parent directory is automatically created for file-based DBs."""
        temp_dir = tempfile.mkdtemp()
        nested_db_path = os.path.join(temp_dir, "nested", "sub", "piso.db")
        try:
            db = DatabaseManager(nested_db_path)
            assert os.path.exists(os.path.dirname(nested_db_path))

            with db.get_connection() as conn:
                conn.execute("CREATE TABLE sample (id INT);")

            assert os.path.exists(nested_db_path)
        finally:
            if os.path.exists(nested_db_path):
                os.remove(nested_db_path)
            # Cleanup temp directory
            for root, dirs, files in os.walk(temp_dir, topdown=False):
                for f in files:
                    os.remove(os.path.join(root, f))
                for d in dirs:
                    os.rmdir(os.path.join(root, d))
            os.rmdir(temp_dir)

    def test_concurrent_access_thread_safety(self, in_memory_db: DatabaseManager):
        """Verify concurrent multi-threaded execution behaves safely."""
        with in_memory_db.get_connection() as conn:
            conn.execute("CREATE TABLE thread_safe_counts (id INTEGER PRIMARY KEY AUTOINCREMENT);")

        def insert_records():
            for _ in range(50):
                with in_memory_db.get_connection() as conn:
                    conn.execute("INSERT INTO thread_safe_counts DEFAULT VALUES;")

        threads = [threading.Thread(target=insert_records) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with in_memory_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as total FROM thread_safe_counts;")
            assert cursor.fetchone()["total"] == 200


class TestUserRepository:
    """Tests for UserRepository schema initialization and CRUD operations."""

    def test_init_db_creates_expected_schema(self, repo: UserRepository):
        """Verify users, transactions, and time_logs tables and indexes exist."""
        with repo.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'transactions', 'time_logs');"
            )
            tables = {row["name"] for row in cursor.fetchall()}
            assert {"users", "transactions", "time_logs"}.issubset(tables)

            # Check users columns
            cursor.execute("PRAGMA table_info(users);")
            columns = {row["name"] for row in cursor.fetchall()}
            expected_cols = {
                "id",
                "mac_address",
                "time_balance",
                "status",
                "created_at",
                "last_deduction",
                "download_limit",
                "upload_limit",
                "plan",
                "upgrade_requested",
            }
            assert expected_cols.issubset(columns)

    def test_init_db_idempotency(self, repo: UserRepository):
        """Verify running init_db multiple times does not raise or destroy data."""
        repo.add_time("00:11:22:33:44:55", amount=10.0, minutes=10)
        assert repo.init_db() is True
        user = repo.get_user_by_mac("00:11:22:33:44:55")
        assert user is not None
        assert user.time_balance == 10.0

    def test_add_time_for_new_user(self, repo: UserRepository):
        """Verify adding time for a new user inserts user and transaction correctly."""
        success = repo.add_time("00:aa:bb:cc:dd:ee", amount=5.0, minutes=30)
        assert success is True

        user = repo.get_user_by_mac("00:aa:bb:cc:dd:ee")
        assert user is not None
        assert user.mac_address == "00:aa:bb:cc:dd:ee"
        assert user.time_balance == 30.0
        assert user.status == UserStatus.ACTIVE
        assert user.download_limit == 2048
        assert user.upload_limit == 1024
        assert user.plan == PlanType.DEFAULT
        assert user.upgrade_requested is False
        assert user.has_time_remaining is True
        assert user.bandwidth.download_kbps == 2048
        assert user.bandwidth.upload_kbps == 1024

        transactions = repo.get_transactions_by_user_id(user.id)
        assert len(transactions) == 1
        assert transactions[0].amount == 5.0
        assert transactions[0].minutes == 30
        assert transactions[0].user_id == user.id

    def test_add_time_for_existing_user(self, repo: UserRepository):
        """Verify adding time to an existing user accumulates balance and activates user."""
        repo.add_time("00:aa:bb:cc:dd:ee", amount=5.0, minutes=30)
        # Deduct all time so user is inactive
        repo.deduct_time("00:aa:bb:cc:dd:ee", minutes=30)
        user_before = repo.get_user_by_mac("00:aa:bb:cc:dd:ee")
        assert user_before.status == UserStatus.INACTIVE
        assert user_before.time_balance == 0.0

        # Add more time
        success = repo.add_time("00:aa:bb:cc:dd:ee", amount=10.0, minutes=60)
        assert success is True

        user_after = repo.get_user_by_mac("00:aa:bb:cc:dd:ee")
        assert user_after.time_balance == 60.0
        assert user_after.status == UserStatus.ACTIVE

        txs = repo.get_transactions_by_user_id(user_after.id)
        assert len(txs) == 2
        assert txs[0].amount == 5.0
        assert txs[1].amount == 10.0

    def test_check_balance(self, repo: UserRepository):
        """Verify checking balance returns accurate float or 0.0 for unknown MAC."""
        assert repo.check_balance("non:existent:mac") == 0.0

        repo.add_time("11:22:33:44:55:66", amount=2.0, minutes=15)
        assert repo.check_balance("11:22:33:44:55:66") == 15.0

    def test_deduct_time_partial_and_full(self, repo: UserRepository):
        """Verify deduct_time updates balance, status, and creates time_logs."""
        mac = "22:33:44:55:66:77"
        repo.add_time(mac, amount=10.0, minutes=45)

        # 1. Partial auto deduction
        deduct_res = repo.deduct_time(mac, minutes=15.0)
        assert deduct_res is True

        user = repo.get_user_by_mac(mac)
        assert user.time_balance == 30.0
        assert user.status == UserStatus.ACTIVE
        assert user.last_deduction is not None

        logs = repo.get_time_logs_by_mac(mac)
        assert len(logs) == 1
        assert logs[0].minutes_deducted == 15.0
        assert logs[0].balance_before == 45.0
        assert logs[0].balance_after == 30.0
        assert logs[0].deduction_type == DeductionType.AUTO

        # 2. Manual deduction depleting balance
        deduct_res2 = repo.deduct_time(mac, minutes=35.0, manual=True)
        assert deduct_res2 is True

        user2 = repo.get_user_by_mac(mac)
        assert user2.time_balance == 0.0
        assert user2.status == UserStatus.INACTIVE

        logs = repo.get_time_logs_by_mac(mac)
        assert len(logs) == 2
        assert logs[1].minutes_deducted == 35.0
        assert logs[1].balance_before == 30.0
        assert logs[1].balance_after == 0.0
        assert logs[1].deduction_type == DeductionType.MANUAL

    def test_deduct_time_unknown_user(self, repo: UserRepository):
        """Verify deducting time from non-existent user returns False."""
        assert repo.deduct_time("99:99:99:99:99:99", minutes=5.0) is False

    def test_get_user_by_mac_case_insensitivity(self, repo: UserRepository):
        """Verify MAC lookup is case-insensitive."""
        repo.add_time("aa:bb:cc:11:22:33", amount=1.0, minutes=10)

        user_upper = repo.get_user_by_mac("AA:BB:CC:11:22:33")
        assert user_upper is not None
        assert user_upper.mac_address == "aa:bb:cc:11:22:33"

        user_lower = repo.get_user_by_mac("aa:bb:cc:11:22:33")
        assert user_lower is not None
        assert user_lower.id == user_upper.id

    def test_get_all_users(self, repo: UserRepository):
        """Verify get_all_users returns all registered devices."""
        assert repo.get_all_users() == []

        repo.add_time("10:00:00:00:00:01", 1.0, 10)
        repo.add_time("10:00:00:00:00:02", 2.0, 20)
        repo.add_time("10:00:00:00:00:03", 3.0, 30)

        users = repo.get_all_users()
        assert len(users) == 3
        macs = [u.mac_address for u in users]
        assert macs == [
            "10:00:00:00:00:01",
            "10:00:00:00:00:02",
            "10:00:00:00:00:03",
        ]

    def test_set_bandwidth(self, repo: UserRepository):
        """Verify updating download and upload bandwidth limits."""
        mac = "33:44:55:66:77:88"
        repo.add_time(mac, 5.0, 20)

        assert repo.set_bandwidth(mac, download_kbps=4096, upload_kbps=2048) is True
        user = repo.get_user_by_mac(mac)
        assert user.download_limit == 4096
        assert user.upload_limit == 2048

        # Non-existent user
        assert repo.set_bandwidth("00:00:00:00:00:00", 1024, 512) is False

    def test_request_upgrade(self, repo: UserRepository):
        """Verify requesting an upgrade updates upgrade_requested flag."""
        mac = "44:55:66:77:88:99"
        repo.add_time(mac, 5.0, 20)

        user_initial = repo.get_user_by_mac(mac)
        assert user_initial.upgrade_requested is False

        assert repo.request_upgrade(mac) is True
        user_updated = repo.get_user_by_mac(mac)
        assert user_updated.upgrade_requested is True

        # Non-existent user
        assert repo.request_upgrade("00:00:00:00:00:00") is False

    def test_update_plan(self, repo: UserRepository):
        """Verify updating plan resets upgrade_requested and adjusts limits."""
        mac = "55:66:77:88:99:aa"
        repo.add_time(mac, 10.0, 60)
        repo.request_upgrade(mac)

        user_before = repo.get_user_by_mac(mac)
        assert user_before.upgrade_requested is True
        assert user_before.plan == PlanType.DEFAULT

        # Update to premium plan
        success = repo.update_plan(
            mac=mac,
            plan=PlanType.PREMIUM,
            download_limit=8096,
            upload_limit=8096,
        )
        assert success is True

        user_after = repo.get_user_by_mac(mac)
        assert user_after.plan == PlanType.PREMIUM
        assert user_after.download_limit == 8096
        assert user_after.upload_limit == 8096
        assert user_after.upgrade_requested is False

        # Non-existent user
        assert repo.update_plan("00:00:00:00:00:00", PlanType.PREMIUM, 8096, 8096) is False

    def test_check_health(self, repo: UserRepository):
        """Verify check_health reports True on active database."""
        assert repo.check_health() is True

    def test_foreign_key_cascade(self, repo: UserRepository):
        """Verify deleting a user cascades to transactions and time_logs."""
        mac = "66:77:88:99:aa:bb"
        repo.add_time(mac, amount=10.0, minutes=50)
        repo.deduct_time(mac, minutes=10.0)

        user = repo.get_user_by_mac(mac)
        assert user is not None
        user_id = user.id

        assert len(repo.get_transactions_by_user_id(user_id)) == 1
        assert len(repo.get_time_logs_by_mac(mac)) == 1

        with repo.db.get_connection() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

        assert repo.get_user_by_mac(mac) is None
        assert len(repo.get_transactions_by_user_id(user_id)) == 0
        assert len(repo.get_time_logs_by_mac(mac)) == 0

    def test_update_plan_with_string_plan(self, repo: UserRepository):
        """Verify updating plan with a string instead of PlanType enum."""
        mac = "77:88:99:aa:bb:cc"
        repo.add_time(mac, 5.0, 30)

        assert repo.update_plan(mac, plan="premium", download_limit=5000, upload_limit=5000) is True
        user = repo.get_user_by_mac(mac)
        assert user.plan == PlanType.PREMIUM
        assert user.download_limit == 5000

        # Invalid string defaults to PlanType.DEFAULT
        assert repo.update_plan(mac, plan="unknown_plan", download_limit=2048, upload_limit=1024) is True
        user2 = repo.get_user_by_mac(mac)
        assert user2.plan == PlanType.DEFAULT

    def test_deduct_time_with_string_deduction_type(self, repo: UserRepository):
        """Verify deduct_time accepts string deduction_type."""
        mac = "88:99:aa:bb:cc:dd"
        repo.add_time(mac, 5.0, 30)

        assert repo.deduct_time(mac, minutes=5.0, deduction_type="manual") is True
        logs = repo.get_time_logs_by_mac(mac)
        assert len(logs) == 1
        assert logs[0].deduction_type == DeductionType.MANUAL

    def test_check_health_failure(self):
        """Verify check_health returns False when connection cannot be established."""
        # Use an invalid path / unwritable directory or closed connection mock
        db = DatabaseManager("/dev/null/unreachable.db")
        repo = UserRepository(db)
        assert repo.check_health() is False

    def test_default_repo_init(self):
        """Verify UserRepository can be instantiated with default DatabaseManager."""
        repo = UserRepository()
        assert repo.db is not None
        assert isinstance(repo.db, DatabaseManager)

