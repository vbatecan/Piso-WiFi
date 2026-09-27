"""User service and management module for Piso-WiFi."""

from datetime import datetime, timedelta
import logging
import os
import secrets
import sqlite3
from typing import Any, Dict, List, Optional

from piso_wifi.config import AppConfig
from piso_wifi.models.entities import User, Voucher
from piso_wifi.models.enums import DeductionType, PlanType, UserStatus


def _parse_datetime(val: Any) -> Optional[datetime]:
    """Parse sqlite datetime string or object safely."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace(" ", "T"))
        except ValueError:
            return None
    return None


class UserService:
    """Service handling user account management, balances, and service plans."""

    def __init__(
        self,
        repository: Optional[Any] = None,
        db_path: Optional[str] = None,
        config: Optional[AppConfig] = None,
    ):
        self.logger = logging.getLogger(__name__)
        self.config = config or AppConfig()

        # Allow db_path to be passed as the first positional argument
        if isinstance(repository, str):
            db_path = repository
            repository = None

        if db_path is not None:
            self._db_path = db_path
        elif repository is not None and hasattr(repository, "db_path"):
            self._db_path = getattr(repository, "db_path")
        elif (
            repository is not None
            and hasattr(repository, "db")
            and hasattr(repository.db, "db_path")
        ):
            self._db_path = getattr(repository.db, "db_path")
        else:
            self._db_path = self.config.db_path

        # Handle dependency injection of DatabaseManager instance
        try:
            from piso_wifi.database.connection import DatabaseManager
            from piso_wifi.database.repository import UserRepository

            if isinstance(repository, DatabaseManager):
                repository = UserRepository(db_manager=repository)
        except (ImportError, ModuleNotFoundError):
            pass

        # Handle repository class or instance
        if isinstance(repository, type):
            try:
                from piso_wifi.database.connection import DatabaseManager
                from piso_wifi.database.repository import UserRepository

                if issubclass(repository, UserRepository):
                    db_mgr = DatabaseManager(db_path=self._db_path)
                    self._repository = repository(db_manager=db_mgr)
                elif issubclass(repository, DatabaseManager):
                    db_mgr = repository(db_path=self._db_path)
                    self._repository = UserRepository(db_manager=db_mgr)
                else:
                    self._repository = repository()
            except Exception:
                self._repository = repository()
        elif repository is not None:
            self._repository = repository
        else:
            # Try to discover and initialize UserRepository from piso_wifi.database
            try:
                from piso_wifi.database.connection import DatabaseManager
                from piso_wifi.database.repository import UserRepository

                db_mgr = DatabaseManager(db_path=self._db_path)
                self._repository = UserRepository(db_manager=db_mgr)
                if hasattr(self._repository, "init_db"):
                    self._repository.init_db()
            except (ImportError, ModuleNotFoundError, AttributeError):
                self._repository = None

        # If running in direct SQLite mode, ensure database tables exist
        if self._repository is None and self._db_path:
            self._init_db()

    @property
    def repository(self) -> Optional[Any]:
        """Return the underlying repository if configured."""
        return self._repository

    @property
    def db_path(self) -> str:
        """Database path for backward compatibility."""
        if self._repository is not None:
            if hasattr(self._repository, "db") and hasattr(self._repository.db, "db_path"):
                return self._repository.db.db_path
            if hasattr(self._repository, "db_path"):
                return self._repository.db_path
        return self._db_path

    @db_path.setter
    def db_path(self, value: str) -> None:
        self._db_path = value
        if self._repository is not None:
            if hasattr(self._repository, "db") and hasattr(self._repository.db, "db_path"):
                self._repository.db.db_path = value
                if hasattr(self._repository, "init_db"):
                    self._repository.init_db()
            elif hasattr(self._repository, "db_path"):
                self._repository.db_path = value
        else:
            self._init_db()

    @property
    def db_name(self) -> str:
        """Alias for db_path for backward compatibility."""
        return self.db_path

    @db_name.setter
    def db_name(self, value: str) -> None:
        """Alias for db_path for backward compatibility."""
        self.db_path = value

    def _init_db(self) -> None:
        """Initialize database tables for direct SQLite mode."""
        if not self._db_path:
            return

        dir_name = os.path.dirname(self._db_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac_address TEXT UNIQUE,
                    time_balance REAL DEFAULT 0,
                    status TEXT DEFAULT 'inactive',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_deduction TIMESTAMP,
                    download_limit INTEGER DEFAULT 1024,
                    upload_limit INTEGER DEFAULT 512,
                    plan TEXT DEFAULT 'default',
                    upgrade_requested BOOLEAN DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    minutes INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS time_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    mac_address TEXT,
                    minutes_deducted REAL,
                    balance_before REAL,
                    balance_after REAL,
                    deducted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deduction_type TEXT DEFAULT 'auto',
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """)
            conn.commit()
        except Exception as e:
            self.logger.error(f"Error initializing database: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_time(self, mac_address: str, amount: float, minutes: Optional[int] = None) -> bool:
        """Add time for a user.

        If minutes is None, calculates minutes based on config rate (default: 1 peso = 1 min).
        """
        mac_address = mac_address.upper()
        if minutes is None:
            rate = getattr(self.config, "minutes_per_peso", 1.0)
            minutes = int(amount * rate)

        if self._repository is not None and hasattr(self._repository, "add_time"):
            return bool(self._repository.add_time(mac_address, amount, minutes))

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("SELECT id, time_balance FROM users WHERE mac_address = ?", (mac_address,))
            user = c.fetchone()

            if user is None:
                c.execute(
                    "INSERT INTO users (mac_address, time_balance, status) VALUES (?, ?, ?)",
                    (mac_address, minutes, UserStatus.ACTIVE.value),
                )
                user_id = c.lastrowid
            else:
                user_id, current_balance = user
                c.execute(
                    "UPDATE users SET time_balance = time_balance + ?, status = ? WHERE id = ?",
                    (minutes, UserStatus.ACTIVE.value, user_id),
                )

            c.execute(
                "INSERT INTO transactions (user_id, amount, minutes) VALUES (?, ?, ?)",
                (user_id, amount, minutes),
            )
            conn.commit()
            self.logger.info(f"Added {minutes} minutes for MAC {mac_address}")
            return True
        except Exception as e:
            self.logger.error(f"Error adding time: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def check_balance(self, mac_address: str) -> float:
        """Check time balance for a MAC address in minutes."""
        mac_address = mac_address.upper()

        if self._repository is not None:
            if hasattr(self._repository, "check_balance"):
                return float(self._repository.check_balance(mac_address))
            if hasattr(self._repository, "get_user_info"):
                u = self._repository.get_user_info(mac_address)
                return float(u.time_balance) if u else 0.0
            if hasattr(self._repository, "get_user_by_mac"):
                u = self._repository.get_user_by_mac(mac_address)
                return float(u.time_balance) if u else 0.0
            if hasattr(self._repository, "get_by_mac"):
                u = self._repository.get_by_mac(mac_address)
                return float(u.time_balance) if u else 0.0

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("SELECT time_balance FROM users WHERE mac_address = ?", (mac_address,))
            result = c.fetchone()
            return float(result[0]) if result and result[0] is not None else 0.0
        except Exception as e:
            self.logger.error(f"Error checking balance: {e}")
            return 0.0
        finally:
            conn.close()

    def deduct_time(self, mac_address: str, minutes: float, manual: bool = False) -> bool:
        """Deduct time from user's balance and update status if depleted."""
        mac_address = mac_address.upper()

        if self._repository is not None and hasattr(self._repository, "deduct_time"):
            return bool(self._repository.deduct_time(mac_address, minutes, manual=manual))

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("SELECT id, time_balance FROM users WHERE mac_address = ?", (mac_address,))
            result = c.fetchone()

            if not result:
                self.logger.warning(f"No user found for MAC {mac_address}")
                return False

            user_id, current_balance = result
            new_balance = max(0.0, current_balance - minutes)

            c.execute("""
                UPDATE users 
                SET time_balance = ?,
                    status = CASE 
                        WHEN ? <= 0 THEN 'inactive'
                        ELSE 'active'
                    END,
                    last_deduction = CURRENT_TIMESTAMP
                WHERE mac_address = ?
            """, (new_balance, new_balance, mac_address))

            deduction_type = DeductionType.MANUAL.value if manual else DeductionType.AUTO.value
            c.execute("""
                INSERT INTO time_logs (
                    user_id,
                    mac_address, 
                    minutes_deducted, 
                    balance_before,
                    balance_after,
                    deducted_at,
                    deduction_type
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """, (user_id, mac_address, minutes, current_balance, new_balance, deduction_type))

            conn.commit()
            self.logger.info(
                f"Deducted {minutes} minutes from {mac_address}. Balance: {current_balance} -> {new_balance}"
            )
            return True
        except Exception as e:
            self.logger.error(f"Error deducting time: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def set_bandwidth(self, mac_address: str, download_kbps: int, upload_kbps: int) -> bool:
        """Set bandwidth limits for a user."""
        mac_address = mac_address.upper()

        if self._repository is not None and hasattr(self._repository, "set_bandwidth"):
            return bool(self._repository.set_bandwidth(mac_address, download_kbps, upload_kbps))

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("""
                UPDATE users 
                SET download_limit = ?,
                    upload_limit = ?
                WHERE mac_address = ?
            """, (download_kbps, upload_kbps, mac_address))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            self.logger.error(f"Error setting bandwidth: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def request_upgrade(self, mac_address: str) -> bool:
        """Flag that a user requested a plan upgrade."""
        mac_address = mac_address.upper()

        if self._repository is not None and hasattr(self._repository, "request_upgrade"):
            return bool(self._repository.request_upgrade(mac_address))

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("UPDATE users SET upgrade_requested = 1 WHERE mac_address = ?", (mac_address,))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            self.logger.error(f"Error requesting upgrade for {mac_address}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def manage_plan(
        self,
        mac_address: str,
        new_plan: str,
        download_speed: int,
        upload_speed: int,
    ) -> bool:
        """Update plan tier and bandwidth speeds for a device."""
        mac_address = mac_address.upper()

        if self._repository is not None:
            if hasattr(self._repository, "manage_plan"):
                return bool(self._repository.manage_plan(mac_address, new_plan, download_speed, upload_speed))
            if hasattr(self._repository, "update_plan"):
                return bool(self._repository.update_plan(mac_address, new_plan, download_speed, upload_speed))

        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        try:
            c.execute("""
                UPDATE users 
                SET plan = ?,
                    download_limit = ?,
                    upload_limit = ?,
                    upgrade_requested = 0
                WHERE mac_address = ?
            """, (new_plan, download_speed, upload_speed, mac_address))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            self.logger.error(f"Error managing plan for {mac_address}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def get_user_info(self, mac_address: str) -> Optional[User]:
        """Fetch user entity details by MAC address."""
        mac_address = mac_address.upper()

        if self._repository is not None:
            user = None
            if hasattr(self._repository, "get_user_info"):
                user = self._repository.get_user_info(mac_address)
            elif hasattr(self._repository, "get_user_by_mac"):
                user = self._repository.get_user_by_mac(mac_address)
            elif hasattr(self._repository, "get_by_mac"):
                user = self._repository.get_by_mac(mac_address)

            if user is not None:
                if user.mac_address.lower() == mac_address.lower():
                    user.mac_address = mac_address
                return user

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            c.execute("SELECT * FROM users WHERE mac_address = ?", (mac_address,))
            row = c.fetchone()
            if not row:
                return None

            keys = row.keys()
            return User(
                id=row["id"] if "id" in keys else None,
                mac_address=row["mac_address"] if "mac_address" in keys else mac_address,
                time_balance=float(row["time_balance"]) if "time_balance" in keys and row["time_balance"] is not None else 0.0,
                status=UserStatus.from_str(row["status"]) if "status" in keys and row["status"] else UserStatus.INACTIVE,
                created_at=_parse_datetime(row["created_at"]) if "created_at" in keys else None,
                last_deduction=_parse_datetime(row["last_deduction"]) if "last_deduction" in keys else None,
                download_limit=row["download_limit"] if "download_limit" in keys and row["download_limit"] is not None else 1024,
                upload_limit=row["upload_limit"] if "upload_limit" in keys and row["upload_limit"] is not None else 512,
                plan=PlanType.from_str(row["plan"]) if "plan" in keys and row["plan"] else PlanType.DEFAULT,
                upgrade_requested=bool(row["upgrade_requested"]) if "upgrade_requested" in keys and row["upgrade_requested"] is not None else False,
            )
        except Exception as e:
            self.logger.error(f"Error fetching user info for {mac_address}: {e}")
            return None
        finally:
            conn.close()

    def check_health(self) -> bool:
        """Check if database / storage is accessible."""
        if self._repository is not None and hasattr(self._repository, "check_health"):
            return bool(self._repository.check_health())

        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute("SELECT 1")
            return True
        except Exception as e:
            self.logger.error(f"Database health check failed: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def pause_time(self, mac_address: str) -> bool:
        """Pause a user's time balance, changing status to PAUSED."""
        user = self.get_user_info(mac_address)
        if not user or user.time_balance <= 0:
            return False

        if self._repository is not None and hasattr(self._repository, "update_user_status"):
            return self._repository.update_user_status(mac_address, UserStatus.PAUSED)

        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute(
                "UPDATE users SET status = ? WHERE LOWER(mac_address) = ?",
                (UserStatus.PAUSED.value, mac_address.lower()),
            )
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            self.logger.error(f"Error pausing time for {mac_address}: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def resume_time(self, mac_address: str) -> bool:
        """Resume a user's paused time balance, changing status to ACTIVE."""
        user = self.get_user_info(mac_address)
        if not user or user.time_balance <= 0:
            return False

        if self._repository is not None and hasattr(self._repository, "update_user_status"):
            return self._repository.update_user_status(mac_address, UserStatus.ACTIVE)

        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute(
                "UPDATE users SET status = ? WHERE LOWER(mac_address) = ?",
                (UserStatus.ACTIVE.value, mac_address.lower()),
            )
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            self.logger.error(f"Error resuming time for {mac_address}: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def create_voucher(
        self,
        time_minutes: float,
        code: Optional[str] = None,
        expires_days: int = 30,
    ) -> Optional[Voucher]:
        """Generate and save a single promo/time voucher code."""
        if not code:
            code = secrets.token_hex(4).upper()
        expires_at = datetime.now() + timedelta(days=expires_days)
        repo = self._get_voucher_repo()
        if repo and repo.create_voucher(code, time_minutes, expires_at):
            return repo.get_voucher(code)
        return None

    def create_vouchers(
        self,
        count: int,
        time_minutes: float,
        expires_days: int = 30,
    ) -> List[str]:
        """Generate multiple vouchers in batch."""
        codes = []
        for _ in range(count):
            v = self.create_voucher(time_minutes=time_minutes, expires_days=expires_days)
            if v:
                codes.append(v.code)
        return codes

    def redeem_voucher(self, code: str, mac_address: str) -> Dict[str, Any]:
        """Validate and redeem a voucher for a client MAC address."""
        repo = self._get_voucher_repo()
        if not repo:
            return {"success": False, "error": "Voucher service unavailable"}

        voucher = repo.get_voucher(code)
        if not voucher:
            return {"success": False, "error": "Invalid voucher code"}

        if voucher.is_used:
            return {"success": False, "error": "Voucher has already been used"}

        if voucher.is_expired:
            return {"success": False, "error": "Voucher has expired"}

        added = self.add_time(mac_address, voucher.time_minutes)
        if not added:
            return {"success": False, "error": "Failed to add time balance"}

        repo.mark_voucher_used(code, mac_address)

        return {
            "success": True,
            "code": voucher.code,
            "time_minutes": voucher.time_minutes,
            "mac_address": mac_address,
        }

    def list_vouchers(self, active_only: bool = False) -> List[Voucher]:
        """List vouchers."""
        repo = self._get_voucher_repo()
        if repo:
            return repo.list_vouchers(active_only=active_only)
        return []

    def delete_voucher(self, code: str) -> bool:
        """Delete a voucher by code."""
        repo = self._get_voucher_repo()
        if repo and hasattr(repo, "delete_voucher"):
            return repo.delete_voucher(code)
        return False

    def _get_voucher_repo(self):
        if not hasattr(self, "_voucher_repo") or self._voucher_repo is None:
            try:
                from piso_wifi.database.repository import VoucherRepository
                self._voucher_repo = VoucherRepository(db_path=self._db_path)
                self._voucher_repo.init_db()
            except Exception as e:
                self.logger.debug(f"Could not initialize VoucherRepository: {e}")
                self._voucher_repo = None
        return self._voucher_repo


# Backward-compatibility alias
UserManager = UserService
