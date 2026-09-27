"""User repository and database operations for Piso-WiFi."""

from datetime import datetime
import logging
import sqlite3
from typing import Any, List, Optional, Union

from piso_wifi.database.connection import DatabaseManager
from piso_wifi.models.entities import TimeLog, Transaction, User, Voucher
from piso_wifi.models.enums import DeductionType, PlanType, UserStatus

logger = logging.getLogger(__name__)


class UserRepository:
    """Repository handling all database operations for users, transactions, and time logs."""

    def __init__(
        self,
        db_manager: Optional[Union[DatabaseManager, str]] = None,
        db_path: Optional[str] = None,
    ):
        """Initialize UserRepository with dependency-injected DatabaseManager or db_path.

        Args:
            db_manager: DatabaseManager instance or db_path string.
            db_path: Optional path to SQLite database.
        """
        if db_path is not None:
            self.db = DatabaseManager(db_path=db_path)
        elif isinstance(db_manager, str):
            self.db = DatabaseManager(db_path=db_manager)
        elif isinstance(db_manager, DatabaseManager):
            self.db = db_manager
        else:
            self.db = DatabaseManager()

    @property
    def db_path(self) -> str:
        return self.db.db_path

    @db_path.setter
    def db_path(self, value: str) -> None:
        self.db = DatabaseManager(db_path=value)

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        """Normalize MAC address string to lower case and stripped."""
        return mac.strip().lower()

    @staticmethod
    def _parse_datetime(val: Any) -> Optional[datetime]:
        """Parse datetime object or ISO/SQLite timestamp string."""
        if val is None or isinstance(val, datetime):
            return val
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S.%f",
            ):
                try:
                    return datetime.strptime(val, fmt)
                except ValueError:
                    pass
            try:
                return datetime.fromisoformat(val)
            except Exception:
                return None
        return None

    def _row_to_user(self, row: sqlite3.Row) -> User:
        """Convert a sqlite3.Row to a typed User entity."""
        status_val = row["status"]
        if isinstance(status_val, UserStatus):
            status = status_val
        elif isinstance(status_val, str):
            status = UserStatus.from_str(status_val)
        else:
            status = UserStatus.INACTIVE

        plan_val = row["plan"]
        if isinstance(plan_val, PlanType):
            plan = plan_val
        elif isinstance(plan_val, str):
            plan = PlanType.from_str(plan_val)
        else:
            plan = PlanType.DEFAULT

        return User(
            id=row["id"],
            mac_address=row["mac_address"],
            time_balance=float(row["time_balance"]),
            status=status,
            created_at=self._parse_datetime(row["created_at"]),
            last_deduction=self._parse_datetime(row["last_deduction"]),
            download_limit=int(row["download_limit"]),
            upload_limit=int(row["upload_limit"]),
            plan=plan,
            upgrade_requested=bool(row["upgrade_requested"]),
        )

    def init_db(self) -> bool:
        """Initialize database schema including tables, columns, constraints, and indexes.

        Creates:
        - users: user accounts, statuses, bandwidth limits, and plan details.
        - transactions: log of balance additions and monetary amounts with foreign key to users.
        - time_logs: audit log of time deductions with deduction type and foreign key to users.

        Returns:
            bool: True on successful initialization.
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # Users table
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        mac_address TEXT UNIQUE NOT NULL,
                        time_balance REAL DEFAULT 0.0,
                        status TEXT DEFAULT 'inactive',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_deduction TIMESTAMP,
                        download_limit INTEGER DEFAULT 2048,
                        upload_limit INTEGER DEFAULT 1024,
                        plan TEXT DEFAULT 'default',
                        upgrade_requested BOOLEAN DEFAULT 0
                    );
                    """
                )

                # Transactions table
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        amount REAL NOT NULL,
                        minutes INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                    );
                    """
                )

                # Time logs table
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS time_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        mac_address TEXT NOT NULL,
                        minutes_deducted REAL NOT NULL,
                        balance_before REAL NOT NULL,
                        balance_after REAL NOT NULL,
                        deducted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        deduction_type TEXT DEFAULT 'auto',
                        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                    );
                    """
                )

                # Vouchers table
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vouchers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT UNIQUE NOT NULL,
                        time_minutes REAL NOT NULL,
                        is_used BOOLEAN DEFAULT 0,
                        used_by_mac TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
                    );
                    """
                )

                # Indexes
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_mac ON users (mac_address);"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions (user_id);"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_time_logs_user ON time_logs (user_id);"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vouchers_code ON vouchers (code);"
                )

                logger.info("Database schema initialized successfully.")
                return True
        except Exception as e:
            logger.error(f"Error initializing database schema: {e}")
            raise

    def get_user_by_mac(self, mac: str) -> Optional[User]:
        """Retrieve user by MAC address.

        Args:
            mac: Client MAC address.

        Returns:
            Optional[User]: Typed User entity if found, None otherwise.
        """
        norm_mac = self._normalize_mac(mac)
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM users WHERE LOWER(mac_address) = ? LIMIT 1",
                    (norm_mac,)
                )
                row = cursor.fetchone()
                if row:
                    return self._row_to_user(row)
                return None
        except Exception as e:
            logger.error(f"Error fetching user by MAC {mac}: {e}")
            return None

    def get_all_users(self) -> List[User]:
        """Retrieve all users in the system.

        Returns:
            List[User]: List of typed User entities.
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM users ORDER BY id ASC")
                rows = cursor.fetchall()
                return [self._row_to_user(row) for row in rows]
        except Exception as e:
            logger.error(f"Error fetching all users: {e}")
            return []

    def add_time(self, mac: str, amount: float, minutes: int) -> bool:
        """Add time balance to a user and record the monetary transaction.

        If the user does not exist, a new active user record is inserted.
        If the user exists, their time balance is incremented and status set to active.
        A transaction entry is always recorded linked to the user.

        Args:
            mac: Client MAC address.
            amount: Currency amount paid.
            minutes: Minutes to credit to user balance.

        Returns:
            bool: True on success, False on error.
        """
        norm_mac = self._normalize_mac(mac)
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, time_balance FROM users WHERE LOWER(mac_address) = ?",
                    (norm_mac,)
                )
                row = cursor.fetchone()

                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO users (
                            mac_address, time_balance, status, download_limit, upload_limit, plan, upgrade_requested
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (norm_mac, float(minutes), UserStatus.ACTIVE.value, 2048, 1024, PlanType.DEFAULT.value, 0)
                    )
                    user_id = cursor.lastrowid
                else:
                    user_id = row["id"]
                    cursor.execute(
                        """
                        UPDATE users 
                        SET time_balance = time_balance + ?, 
                            status = ? 
                        WHERE id = ?
                        """,
                        (float(minutes), UserStatus.ACTIVE.value, user_id)
                    )

                cursor.execute(
                    """
                    INSERT INTO transactions (user_id, amount, minutes, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (user_id, float(amount), int(minutes))
                )
                logger.info(f"Added {minutes} minutes (amount: {amount}) for MAC {norm_mac}")
                return True
        except Exception as e:
            logger.error(f"Error adding time for MAC {mac}: {e}")
            return False

    def check_balance(self, mac: str) -> float:
        """Check remaining time balance for a MAC address.

        Args:
            mac: Client MAC address.

        Returns:
            float: Remaining minutes balance, or 0.0 if not found.
        """
        norm_mac = self._normalize_mac(mac)
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT time_balance FROM users WHERE LOWER(mac_address) = ?",
                    (norm_mac,)
                )
                row = cursor.fetchone()
                if row:
                    return float(row["time_balance"])
                return 0.0
        except Exception as e:
            logger.error(f"Error checking balance for MAC {mac}: {e}")
            return 0.0

    def deduct_time(
        self,
        mac: str,
        minutes: float,
        manual: bool = False,
        deduction_type: DeductionType = DeductionType.AUTO,
    ) -> bool:
        """Deduct time from user's balance, update user status, and log the deduction.

        If the remaining balance becomes 0 or less, the user's status transitions
        to inactive. If remaining balance is greater than 0, status remains active.

        Args:
            mac: Client MAC address.
            minutes: Minutes to deduct.
            manual: Flag indicating if deduction was performed manually.
            deduction_type: DeductionType enum (AUTO or MANUAL).

        Returns:
            bool: True on successful deduction and log, False if user not found or error.
        """
        norm_mac = self._normalize_mac(mac)

        # Normalize deduction_type if manual is explicitly True or string provided
        if manual and deduction_type == DeductionType.AUTO:
            effective_deduction_type = DeductionType.MANUAL
        elif isinstance(deduction_type, str):
            effective_deduction_type = (
                DeductionType.MANUAL if deduction_type.lower() == "manual" else DeductionType.AUTO
            )
        else:
            effective_deduction_type = deduction_type

        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, time_balance FROM users WHERE LOWER(mac_address) = ?",
                    (norm_mac,)
                )
                row = cursor.fetchone()
                if row is None:
                    logger.warning(f"No user found for MAC {mac} during deduction")
                    return False

                user_id = row["id"]
                current_balance = float(row["time_balance"])
                new_balance = max(0.0, current_balance - float(minutes))
                new_status = (
                    UserStatus.INACTIVE.value if new_balance <= 0.0 else UserStatus.ACTIVE.value
                )

                cursor.execute(
                    """
                    UPDATE users
                    SET time_balance = ?,
                        status = ?,
                        last_deduction = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_balance, new_status, user_id)
                )

                cursor.execute(
                    """
                    INSERT INTO time_logs (
                        user_id, mac_address, minutes_deducted, balance_before, balance_after, deducted_at, deduction_type
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    """,
                    (
                        user_id,
                        norm_mac,
                        float(minutes),
                        current_balance,
                        new_balance,
                        effective_deduction_type.value,
                    )
                )
                logger.info(
                    f"Deducted {minutes} min from {norm_mac}. Balance: {current_balance} -> {new_balance} (status: {new_status})"
                )
                return True
        except Exception as e:
            logger.error(f"Error deducting time for MAC {mac}: {e}")
            return False

    def set_bandwidth(self, mac: str, download_kbps: int, upload_kbps: int) -> bool:
        """Set download and upload bandwidth limits for a user.

        Args:
            mac: Client MAC address.
            download_kbps: Download limit in kbps.
            upload_kbps: Upload limit in kbps.

        Returns:
            bool: True if user was found and updated, False otherwise.
        """
        norm_mac = self._normalize_mac(mac)
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE users
                    SET download_limit = ?,
                        upload_limit = ?
                    WHERE LOWER(mac_address) = ?
                    """,
                    (int(download_kbps), int(upload_kbps), norm_mac)
                )
                if cursor.rowcount > 0:
                    logger.info(
                        f"Updated bandwidth for {norm_mac}: down={download_kbps}, up={upload_kbps}"
                    )
                    return True
                logger.warning(f"User with MAC {mac} not found for bandwidth update")
                return False
        except Exception as e:
            logger.error(f"Error setting bandwidth for MAC {mac}: {e}")
            return False

    def request_upgrade(self, mac: str) -> bool:
        """Flag an upgrade request for a user.

        Args:
            mac: Client MAC address.

        Returns:
            bool: True if user was found and flagged, False otherwise.
        """
        norm_mac = self._normalize_mac(mac)
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE users
                    SET upgrade_requested = 1
                    WHERE LOWER(mac_address) = ?
                    """,
                    (norm_mac,)
                )
                if cursor.rowcount > 0:
                    logger.info(f"Upgrade requested for MAC {norm_mac}")
                    return True
                logger.warning(f"User with MAC {mac} not found to request upgrade")
                return False
        except Exception as e:
            logger.error(f"Error requesting upgrade for MAC {mac}: {e}")
            return False

    def update_plan(
        self,
        mac: str,
        plan: Union[PlanType, str],
        download_limit: int,
        upload_limit: int,
    ) -> bool:
        """Update a user's plan and bandwidth limits, resetting the upgrade request flag.

        Args:
            mac: Client MAC address.
            plan: PlanType enum or string representation.
            download_limit: Download limit in kbps.
            upload_limit: Upload limit in kbps.

        Returns:
            bool: True if user was found and updated, False otherwise.
        """
        norm_mac = self._normalize_mac(mac)
        if isinstance(plan, PlanType):
            plan_str = plan.value
        elif isinstance(plan, str):
            plan_str = PlanType.from_str(plan).value
        else:
            plan_str = PlanType.DEFAULT.value

        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE users
                    SET plan = ?,
                        download_limit = ?,
                        upload_limit = ?,
                        upgrade_requested = 0
                    WHERE LOWER(mac_address) = ?
                    """,
                    (plan_str, int(download_limit), int(upload_limit), norm_mac)
                )
                if cursor.rowcount > 0:
                    logger.info(
                        f"Updated plan for {norm_mac} to {plan_str} ({download_limit}/{upload_limit} kbps)"
                    )
                    return True
                logger.warning(f"User with MAC {mac} not found for plan update")
                return False
        except Exception as e:
            logger.error(f"Error updating plan for MAC {mac}: {e}")
            return False

    def update_user_status(self, mac: str, status: Union[UserStatus, str]) -> bool:
        """Update connection/authorization status for a user.

        Args:
            mac: Client MAC address.
            status: UserStatus enum or string value.

        Returns:
            bool: True if user was found and updated, False otherwise.
        """
        norm_mac = self._normalize_mac(mac)
        if isinstance(status, UserStatus):
            status_str = status.value
        elif isinstance(status, str):
            status_str = UserStatus.from_str(status).value
        else:
            status_str = UserStatus.INACTIVE.value

        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET status = ? WHERE LOWER(mac_address) = ?",
                    (status_str, norm_mac),
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating status for MAC {mac}: {e}")
            return False

    def check_health(self) -> bool:
        """Check if database is accessible and responsive.

        Returns:
            bool: True if database connection succeeds and executes SELECT 1, False otherwise.
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
                return row is not None and row[0] == 1
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False

    def get_transactions_by_user_id(self, user_id: int) -> List[Transaction]:
        """Retrieve transactions for a user ID.

        Args:
            user_id: User ID.

        Returns:
            List[Transaction]: List of Transaction entities.
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM transactions WHERE user_id = ? ORDER BY id ASC",
                    (user_id,)
                )
                rows = cursor.fetchall()
                return [
                    Transaction(
                        id=row["id"],
                        user_id=row["user_id"],
                        amount=float(row["amount"]),
                        minutes=int(row["minutes"]),
                        created_at=self._parse_datetime(row["created_at"]),
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error fetching transactions for user_id {user_id}: {e}")
            return []

    def get_time_logs_by_mac(self, mac: str) -> List[TimeLog]:
        """Retrieve time deduction logs for a MAC address.

        Args:
            mac: Client MAC address.

        Returns:
            List[TimeLog]: List of TimeLog entities.
        """
        norm_mac = self._normalize_mac(mac)
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM time_logs WHERE LOWER(mac_address) = ? ORDER BY id ASC",
                    (norm_mac,)
                )
                rows = cursor.fetchall()
                return [
                    TimeLog(
                        id=row["id"],
                        user_id=row["user_id"],
                        mac_address=row["mac_address"],
                        minutes_deducted=float(row["minutes_deducted"]),
                        balance_before=float(row["balance_before"]),
                        balance_after=float(row["balance_after"]),
                        deducted_at=self._parse_datetime(row["deducted_at"]),
                        deduction_type=(
                            DeductionType.MANUAL
                            if row["deduction_type"] == "manual"
                            else DeductionType.AUTO
                        ),
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error fetching time logs for MAC {mac}: {e}")
            return []


class VoucherRepository:
    """Repository handling database operations for promotional and time credit vouchers."""

    def __init__(
        self,
        db_manager: Optional[Union[DatabaseManager, str]] = None,
        db_path: Optional[str] = None,
    ):
        if db_path is not None:
            self.db = DatabaseManager(db_path=db_path)
        elif isinstance(db_manager, str):
            self.db = DatabaseManager(db_path=db_manager)
        elif isinstance(db_manager, DatabaseManager):
            self.db = db_manager
        else:
            self.db = DatabaseManager()

    @property
    def db_path(self) -> str:
        return self.db.db_path

    @db_path.setter
    def db_path(self, value: str) -> None:
        self.db = DatabaseManager(db_path=value)

    @staticmethod
    def _parse_datetime(val: Any) -> Optional[datetime]:
        return UserRepository._parse_datetime(val)

    def _row_to_voucher(self, row: sqlite3.Row) -> Voucher:
        keys = row.keys()
        return Voucher(
            code=str(row["code"]),
            time_minutes=float(row["time_minutes"]),
            is_used=bool(row["is_used"]),
            used_by_mac=row["used_by_mac"] if "used_by_mac" in keys else None,
            created_at=self._parse_datetime(row["created_at"]) if "created_at" in keys else None,
            expires_at=self._parse_datetime(row["expires_at"]) if "expires_at" in keys else None,
        )

    def init_db(self) -> bool:
        """Initialize vouchers table schema and index."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vouchers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT UNIQUE NOT NULL,
                        time_minutes REAL NOT NULL,
                        is_used BOOLEAN DEFAULT 0,
                        used_by_mac TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
                    );
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vouchers_code ON vouchers (code);"
                )
                return True
        except Exception as e:
            logger.error(f"Error initializing vouchers schema: {e}")
            raise

    def create_voucher(
        self,
        code: str,
        time_minutes: float,
        expires_at: Optional[datetime] = None,
    ) -> bool:
        """Insert a newly generated voucher code."""
        code_norm = code.strip().upper()
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO vouchers (code, time_minutes, is_used, expires_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (code_norm, float(time_minutes), expires_at),
                )
                return True
        except Exception as e:
            logger.error(f"Error creating voucher {code}: {e}")
            return False

    def get_voucher(self, code: str) -> Optional[Voucher]:
        """Fetch voucher details by code."""
        code_norm = code.strip().upper()
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM vouchers WHERE UPPER(code) = ? LIMIT 1",
                    (code_norm,),
                )
                row = cursor.fetchone()
                if row:
                    return self._row_to_voucher(row)
                return None
        except Exception as e:
            logger.error(f"Error fetching voucher {code}: {e}")
            return None

    def mark_voucher_used(self, code: str, mac_address: str) -> bool:
        """Mark a voucher as consumed by a specific client MAC."""
        code_norm = code.strip().upper()
        mac_norm = mac_address.strip().lower()
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE vouchers
                    SET is_used = 1, used_by_mac = ?
                    WHERE UPPER(code) = ? AND is_used = 0
                    """,
                    (mac_norm, code_norm),
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error marking voucher {code} used: {e}")
            return False

    def list_vouchers(self, active_only: bool = False) -> List[Voucher]:
        """List vouchers ordered by creation date."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                if active_only:
                    cursor.execute(
                        """
                        SELECT * FROM vouchers
                        WHERE is_used = 0
                          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                        ORDER BY id DESC
                        """
                    )
                else:
                    cursor.execute("SELECT * FROM vouchers ORDER BY id DESC")
                rows = cursor.fetchall()
                return [self._row_to_voucher(row) for row in rows]
        except Exception as e:
            logger.error(f"Error listing vouchers: {e}")
            return []

    def delete_voucher(self, code: str) -> bool:
        """Delete voucher by code."""
        code_norm = code.strip().upper()
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM vouchers WHERE UPPER(code) = ?", (code_norm,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting voucher {code}: {e}")
            return False
